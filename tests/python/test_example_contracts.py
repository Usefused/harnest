"""Keep checked-in agent examples on the current project contracts."""

from pathlib import Path
import unittest

import yaml


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _ROOT / "examples"


class ExampleContractTests(unittest.TestCase):
    def test_managed_examples_pin_their_selected_framework(self):
        """Prevent examples from silently resolving a different framework release."""
        configs = sorted(_EXAMPLES.rglob("config.yaml"))
        self.assertGreater(len(configs), 0)
        for config_path in configs:
            with self.subTest(example=config_path.parent.relative_to(_EXAMPLES)):
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                lock_path = config_path.with_name("harnest.lock")
                self.assertTrue(lock_path.is_file())
                lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(lock["projectSchema"], 3)
                self.assertEqual(
                    lock["framework"]["name"], config["spec"]["framework"]["name"]
                )
                self.assertRegex(lock["framework"]["version"], r"^\d+\.\d+\.\d+$")

    def test_examples_author_server_overrides_in_project_config(self):
        """Keep examples on the single-file server configuration introduced in 0.12."""
        self.assertEqual(list(_EXAMPLES.rglob("server.yaml")), [])


if __name__ == "__main__":
    unittest.main()
