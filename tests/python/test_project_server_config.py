"""Exercise single-file server authoring and legacy compatibility boundaries."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from harnest.bundle import compile_artifact
from harnest.server_config import (
    DEFAULT_SERVER_YAML,
    ServerConfigError,
    load_server_config,
    project_server_config_yaml,
)
from harnest.upgrade import _plan_server


class ProjectServerConfigTests(unittest.TestCase):
    def test_omitted_and_empty_settings_use_defaults(self):
        """A default agent needs no standalone file or explicit server fields."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(project_server_config_yaml(root), DEFAULT_SERVER_YAML)
            for source in ("kind: Agent\n", "server: {}\n"):
                (root / "config.yaml").write_text(source, encoding="utf-8")
                self.assertEqual(
                    yaml.safe_load(project_server_config_yaml(root)),
                    yaml.safe_load(DEFAULT_SERVER_YAML),
                )
                blockers = []
                _plan_server(root, blockers)
                self.assertEqual(blockers, [])
                self.assertFalse((root / "server.yaml").exists())

    def test_partial_settings_preserve_startup_references_and_defaults(self):
        """Merge only supplied fields and resolve references on the runtime host."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                "server:\n  http:\n    port: ${PORT}\n"
                "  limits:\n    maxRequestBytes: 3MiB\n"
                "  playground:\n    enabled: false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PORT": "do-not-resolve-at-compile"}):
                contents = project_server_config_yaml(root)
            self.assertIn("${PORT}", contents)
            artifact = root / "compiled-server.yaml"
            artifact.write_text(contents, encoding="utf-8")
            config = load_server_config(artifact, environment={"PORT": "9090"})
            self.assertEqual(config.http.port, 9090)
            self.assertEqual(config.http.host, "127.0.0.1")
            self.assertEqual(config.http.max_concurrent_requests, 8)
            self.assertEqual(config.http.request_timeout_seconds, 300)
            self.assertFalse(config.http.allow_remote)
            self.assertEqual(config.limits.max_request_bytes, 3 * 1024**2)
            self.assertFalse(config.playground.enabled)

    def test_rejects_invalid_and_ambiguous_inline_settings(self):
        """Unknown fields, nulls, duplicates, and invalid scalars never become defaults."""
        cases = [
            "server: null\n", "server: []\n", "server: {http: null}\n",
            "server: {unknown: true}\n", "server: {http: {prot: 8080}}\n",
            "server: {http: {port: 0}}\n", "server: {http: {port: true}}\n",
            "server: {http: {host: 'prefix-${HOST}'}}\n",
            "server: {limits: {maxRequestBytes: 2GiB}}\n",
            "server: {playground: {enabled: null}}\n",
            "server: {}\nserver: {}\n", "server: {http: {port: 1, port: 2}}\n",
            "server: {}\n---\nserver: {}\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in cases:
                (root / "config.yaml").write_text(source, encoding="utf-8")
                with self.subTest(source=source), self.assertRaises(ServerConfigError):
                    project_server_config_yaml(root)

    def test_legacy_file_is_preserved_and_conflicts_require_a_choice(self):
        """Existing projects keep their policy, but no source silently takes priority."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = DEFAULT_SERVER_YAML.replace("1MiB", "${MAX_BYTES}")
            (root / "server.yaml").write_text(legacy, encoding="utf-8")
            (root / "config.yaml").write_text("kind: Agent\n", encoding="utf-8")
            self.assertEqual(project_server_config_yaml(root), legacy)
            (root / "config.yaml").write_text("server: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ServerConfigError, "not both"):
                project_server_config_yaml(root)
            blockers = []
            _plan_server(root, blockers)
            self.assertIn("not both", blockers[0])

    def test_rejects_non_regular_and_oversized_server_settings(self):
        """Bound runtime policy without imposing a new limit on deployment config."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.yaml"
            target.write_text("server: {}\n", encoding="utf-8")
            config = root / "config.yaml"
            config.symlink_to(target)
            with self.assertRaisesRegex(ServerConfigError, "regular file"):
                project_server_config_yaml(root)
            config.unlink()
            config.write_text("#" * (64 * 1024 + 1), encoding="utf-8")
            # Unrelated comments must not make a previously valid config too large.
            with config.open("a", encoding="utf-8") as stream:
                stream.write("\nserver: {}\n")
            self.assertEqual(
                yaml.safe_load(project_server_config_yaml(root)),
                yaml.safe_load(DEFAULT_SERVER_YAML),
            )
            config.write_text("server:\n  http:\n    host: " + "a" * (64 * 1024))
            with self.assertRaisesRegex(ServerConfigError, "64KiB"):
                project_server_config_yaml(root)

    def test_invalid_policy_fails_before_authored_code_loads(self):
        """Compiler rejection cannot execute the agent or replace a prior artifact."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            root.mkdir()
            (root / "agent.py").write_text("raise RuntimeError('must not run')\n")
            (root / "config.yaml").write_text("server: {http: {port: false}}\n")
            output = Path(directory) / "compiled"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("prior artifact")
            with patch("harnest.bundle.compile_application") as compile_application:
                with self.assertRaises(ServerConfigError):
                    compile_artifact(root, output)
                compile_application.assert_not_called()
            self.assertEqual(sentinel.read_text(), "prior artifact")

    def test_schema_shares_partial_settings_with_strict_runtime_document(self):
        """Editor validation accepts partial overrides but keeps runtime files strict."""
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        schemas = Path(__file__).parents[2] / "schemas"
        server = json.loads((schemas / "server.schema.json").read_text())
        config = json.loads((schemas / "config.schema.json").read_text())
        registry = Registry().with_resource(server["$id"], Resource.from_contents(server))
        validator = Draft202012Validator(config, registry=registry)
        project = yaml.safe_load(
            (Path(__file__).parents[2] / "examples/self-serve/agents/helpdesk/config.yaml").read_text()
        )
        project["server"] = {"http": {"port": "${PORT}"}, "live": True}
        validator.validate(project)
        project["server"]["http"]["port"] = False
        self.assertFalse(validator.is_valid(project))
        project["server"]["http"]["port"] = "${PORT}"
        project["server"]["live"] = "true"
        self.assertFalse(validator.is_valid(project))
        self.assertFalse(Draft202012Validator(server).is_valid({
            "apiVersion": "harnest.dev/v1alpha1", "kind": "Server", "http": {},
            "limits": {}, "playground": {},
        }))
