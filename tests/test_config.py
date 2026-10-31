"""Configuration regressions use isolated files and never the real APPDATA directory."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from photo_culler.config import Config, DEFAULT_CONFIG

class ConfigTests(unittest.TestCase):
    def load_value(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with patch("photo_culler.config.config_path", return_value=path):
                return Config()

    def test_non_object_json_uses_defaults(self):
        for value in [[], None, 3, "invalid"]:
            with self.subTest(value=value):
                self.assertEqual(self.load_value(value)._data, DEFAULT_CONFIG)

    def test_known_setting_is_loaded(self):
        self.assertEqual(self.load_value({"cache_mb": 128}).get("cache_mb"), 128)

    def test_unknown_setting_is_ignored(self):
        self.assertIsNone(self.load_value({"unknown_setting": 123}).get("unknown_setting"))

    def test_malformed_json_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            with patch("photo_culler.config.config_path", return_value=path):
                self.assertEqual(Config()._data, DEFAULT_CONFIG)

if __name__ == "__main__":
    unittest.main()
