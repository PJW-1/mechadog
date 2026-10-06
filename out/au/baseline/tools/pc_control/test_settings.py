import json
import tempfile
import unittest
from pathlib import Path

from settings import REPO, Settings


class SettingsTests(unittest.TestCase):
    def test_no_settings_cannot_operate(self):
        with self.assertRaises(ValueError):
            Settings().validate()

    def test_relative_paths_and_matching_client(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {
                "data_dir": "runtime",
                "port": "COM99",
                "mac": "001122334455",
                "ota_client": "client.json",
                "packages": ["package.json"],
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            (root / "client.json").write_text(
                json.dumps({"mac": "001122334455", "certificate": "ca.pem"}), encoding="utf-8"
            )
            config = Settings(root / "settings.json").validate()
            self.assertEqual(config.packages, [(root / "package.json").resolve()])
            self.assertEqual(config.client()["certificate"], str((root / "ca.pem").resolve()))

    def test_rejects_missing_identity_or_packages_and_in_repo_data(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "settings.json"
            base = {
                "data_dir": "runtime",
                "port": "COM99",
                "mac": "001122334455",
                "packages": ["package.json"],
            }
            for changes in ({"mac": ""}, {"port": ""}, {"packages": []}, {"data_dir": str(REPO)}):
                with self.subTest(changes=changes):
                    file.write_text(json.dumps(base | changes), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        Settings(file).validate()

    def test_wrong_client_identity_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "data_dir": "runtime",
                        "port": "COM99",
                        "mac": "001122334455",
                        "ota_client": "client.json",
                        "packages": ["package.json"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "client.json").write_text(json.dumps({"mac": "aabbccddeeff"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Settings(root / "settings.json").client()
