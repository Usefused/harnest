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

    def test_compiled_launcher_reads_adjacent_configuration_without_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            (artifact / "server.yaml").write_text(
                DEFAULT_SERVER_YAML.replace("port: 8080", "port: 9091")
                .replace("1MiB", "2MiB")
                .replace("enabled: true", "enabled: false"),
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
            with patch("harnest.runtime.create_fastapi_app", side_effect=create_app):
                with patch.dict(sys.modules, {"uvicorn": uvicorn}):
                    result = runtime_main(["--artifact", str(artifact)])

            self.assertEqual(result, 0)
            self.assertEqual(captured["uvicorn"]["port"], 9091)
            self.assertEqual(captured["options"]["max_request_bytes"], 2 * 1024 * 1024)
            self.assertFalse(captured["options"]["playground_enabled"])


if __name__ == "__main__":
    unittest.main()
