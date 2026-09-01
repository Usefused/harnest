from contextlib import redirect_stderr
from io import StringIO
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.runtime import main as runtime_main
from harnest.server_config import (
    DEFAULT_SERVER_YAML,
    ServerConfigError,
    load_server_config,
    materialize_server_config,
    validate_server_config_template,
)


class ServerConfigTests(unittest.TestCase):
    def test_loads_strict_versioned_configuration_and_binary_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.yaml"
            path.write_text(
                DEFAULT_SERVER_YAML.replace("1MiB", "10MiB")
                .replace("port: 8080", "port: 9090")
                .replace("enabled: true", "enabled: false"),
                encoding="utf-8",
            )

            config = load_server_config(path)

            self.assertEqual(config.http.port, 9090)
            self.assertEqual(config.limits.max_request_bytes, 10 * 1024 * 1024)
            self.assertFalse(config.playground.enabled)

    def test_rejects_unknown_duplicate_unsafe_and_excessive_values(self):
        cases = {
            "unknown": DEFAULT_SERVER_YAML + "unexpected: true\n",
            "duplicate": DEFAULT_SERVER_YAML + "playground:\n  enabled: true\n",
            "oversized": DEFAULT_SERVER_YAML.replace("1MiB", "2GiB"),
            "wrong_kind": DEFAULT_SERVER_YAML.replace("kind: Server", "kind: Agent"),
            "not_finite": DEFAULT_SERVER_YAML.replace(
                "requestTimeoutSeconds: 300", "requestTimeoutSeconds: .nan"
            ),
            "non_string_key": DEFAULT_SERVER_YAML.replace(
                "  host: 127.0.0.1", "  ? [host]\n  : 127.0.0.1"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, contents in cases.items():
                path = root / f"{name}.yaml"
                path.write_text(contents, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ServerConfigError):
                    load_server_config(path)
            target = root / "target.yaml"
            target.write_text(DEFAULT_SERVER_YAML, encoding="utf-8")
            link = root / "link.yaml"
            link.symlink_to(target)
            with self.assertRaises(ServerConfigError):
                load_server_config(link)

    def test_materializes_default_or_preserves_valid_authored_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.yaml"
            generated = root / "generated.yaml"
            materialize_server_config(missing, generated)
            self.assertEqual(generated.read_text(encoding="utf-8"), DEFAULT_SERVER_YAML)

            authored = root / "authored.yaml"
            contents = DEFAULT_SERVER_YAML.replace("1MiB", "3MiB")
            authored.write_text(contents, encoding="utf-8")
            copied = root / "copied.yaml"
            materialize_server_config(authored, copied)
            self.assertEqual(copied.read_text(encoding="utf-8"), contents)

            templated = root / "templated.yaml"
            template_contents = DEFAULT_SERVER_YAML.replace(
                "port: 8080", "port: ${PORT}"
            )
            templated.write_text(template_contents, encoding="utf-8")
            templated_copy = root / "templated-copy.yaml"
            materialize_server_config(templated, templated_copy)
            self.assertEqual(
                templated_copy.read_text(encoding="utf-8"), template_contents
            )

    def test_resolves_exact_environment_references_for_all_scalar_types(self):
        contents = (
            DEFAULT_SERVER_YAML.replace("host: 127.0.0.1", "host: ${HOST}")
            .replace("port: 8080", "port: ${PORT}")
            .replace("allowRemote: false", "allowRemote: ${ALLOW_REMOTE}")
            .replace(
                "requestTimeoutSeconds: 300", "requestTimeoutSeconds: ${TIMEOUT}"
            )
            .replace(
                "maxConcurrentRequests: 8", "maxConcurrentRequests: ${CONCURRENCY}"
            )
            .replace("maxRequestBytes: 1MiB", "maxRequestBytes: ${MAX_BYTES}")
            .replace("enabled: true", "enabled: ${PLAYGROUND}")
        )
        environment = {
            "HOST": "localhost",
            "PORT": "9092",
            "ALLOW_REMOTE": "false",
            "TIMEOUT": "12.5",
            "CONCURRENCY": "17",
            "MAX_BYTES": "3MiB",
            "PLAYGROUND": "true",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.yaml"
            path.write_text(contents, encoding="utf-8")

            config = load_server_config(path, environment=environment)

        self.assertEqual(config.http.host, "localhost")
        self.assertEqual(config.http.port, 9092)
        self.assertFalse(config.http.allow_remote)
        self.assertEqual(config.http.request_timeout_seconds, 12.5)
        self.assertEqual(config.http.max_concurrent_requests, 17)
        self.assertEqual(config.limits.max_request_bytes, 3 * 1024 * 1024)
        self.assertTrue(config.playground.enabled)

    def test_environment_failures_name_reference_and_field_without_value(self):
        contents = DEFAULT_SERVER_YAML.replace("port: 8080", "port: ${PORT}")
        cases = {
            "missing": ({}, "unset"),
            "empty": ({"PORT": "  "}, "empty"),
            "invalid": ({"PORT": "private-value-do-not-report"}, "invalid"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.yaml"
            path.write_text(contents, encoding="utf-8")
            for name, (environment, expected) in cases.items():
                with self.subTest(name=name), self.assertRaises(
                    ServerConfigError
                ) as caught:
                    load_server_config(path, environment=environment)
                message = str(caught.exception)
                self.assertIn("PORT", message)
                self.assertIn("http.port", message)
                self.assertIn(expected, message)
                self.assertNotIn("private-value-do-not-report", message)

    def test_rejects_short_and_partial_environment_syntax_during_compilation(self):
        cases = {
            "short": "$HOST",
            "prefix": "server-${HOST}",
            "suffix": "${HOST}.internal",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, host in cases.items():
                path = root / f"{name}.yaml"
                path.write_text(
                    DEFAULT_SERVER_YAML.replace("127.0.0.1", host),
                    encoding="utf-8",
                )
                with self.subTest(name=name), self.assertRaisesRegex(
                    ServerConfigError, r"http\.host.*exact \$\{NAME\}"
                ):
                    validate_server_config_template(path)

    def test_serve_command_reads_adjacent_configuration_without_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            (artifact / "server.yaml").write_text(
                DEFAULT_SERVER_YAML.replace("port: 8080", "port: ${SERVER_PORT}")
                .replace("1MiB", "${SERVER_MAX_BYTES}")
                .replace("enabled: true", "enabled: ${SERVER_PLAYGROUND}"),
                encoding="utf-8",
            )
            captured = {}

            def create_app(path, **options):
                captured["artifact"] = path
                captured["options"] = options
                return object()

            def run(app, **options):
                captured["app"] = app
                captured["uvicorn"] = options

            uvicorn = SimpleNamespace(run=run)
            environment = {
                "SERVER_PORT": "9091",
                "SERVER_MAX_BYTES": "2MiB",
                "SERVER_PLAYGROUND": "false",
            }
            with patch("harnest.runtime.create_fastapi_app", side_effect=create_app):
                with patch.dict(os.environ, environment, clear=False), patch.dict(
                    sys.modules, {"uvicorn": uvicorn}
                ):
                    result = runtime_main(["--artifact", str(artifact), "serve"])

            self.assertEqual(result, 0)
            self.assertEqual(captured["uvicorn"]["port"], 9091)
            self.assertEqual(captured["options"]["max_request_bytes"], 2 * 1024 * 1024)
            self.assertFalse(captured["options"]["playground_enabled"])

    def test_compiled_launcher_does_not_print_invalid_environment_value(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            (artifact / "server.yaml").write_text(
                DEFAULT_SERVER_YAML.replace("port: 8080", "port: ${SERVER_PORT}"),
                encoding="utf-8",
            )
            stderr = StringIO()
            environment = {"SERVER_PORT": "sensitive-value-do-not-report"}

            with patch.dict(os.environ, environment, clear=False), redirect_stderr(
                stderr
            ):
                result = runtime_main(["--artifact", str(artifact), "serve"])

        output = stderr.getvalue()
        self.assertEqual(result, 2)
        self.assertIn("SERVER_PORT", output)
        self.assertIn("http.port", output)
        self.assertNotIn("sensitive-value-do-not-report", output)


if __name__ == "__main__":
    unittest.main()
