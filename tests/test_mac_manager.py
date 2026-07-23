import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = REPO_ROOT / "tools" / "hls-gallery-manager.py"


def load_manager():
    spec = importlib.util.spec_from_file_location("hls_gallery_manager", MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = load_manager()

    def test_photos_rows_and_video_filter(self):
        encoded = json.dumps([
            {"id": "photo-1", "name": "IMG_1000.HEIC"},
            {"id": "video-1", "name": "IMG_1001.MOV"},
        ])
        rows = self.manager.parse_photos_rows(encoded)
        self.assertEqual(
            [
                {"id": "photo-1", "name": "IMG_1000.HEIC"},
                {"id": "video-1", "name": "IMG_1001.MOV"},
            ],
            rows,
        )
        videos = [
            row for row in rows
            if Path(row["name"]).suffix.lower() in self.manager.VIDEO_EXTENSIONS
        ]
        self.assertEqual(["IMG_1001.MOV"], [item["name"] for item in videos])

    def test_config_validation_and_private_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_path = self.manager.CONFIG_PATH
            self.manager.CONFIG_PATH = Path(temporary) / "manager.json"
            try:
                saved = self.manager.save_config({
                    "host": "videos.example.com",
                    "ssh_user": "gallery-owner",
                    "remote_root": "/var/www/html/videos",
                    "identity_file": "",
                })
                self.assertEqual("gallery-owner", saved["ssh_user"])
                self.assertEqual(0o600, self.manager.CONFIG_PATH.stat().st_mode & 0o777)
                loaded = json.loads(self.manager.CONFIG_PATH.read_text(encoding="utf-8"))
                self.assertNotIn("password", loaded)
            finally:
                self.manager.CONFIG_PATH = old_path

    def test_config_rejects_shell_metacharacters_and_relative_roots(self):
        invalid = [
            {
                "host": "example.com;touch-bad",
                "ssh_user": "owner",
                "remote_root": "/var/www/videos",
            },
            {
                "host": "example.com",
                "ssh_user": "owner;bad",
                "remote_root": "/var/www/videos",
            },
            {
                "host": "example.com",
                "ssh_user": "owner",
                "remote_root": "relative/videos",
            },
            {
                "host": "example.com",
                "ssh_user": "owner",
                "remote_root": "/var/www/videos;touch-bad",
            },
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.manager.validate_config(value)

    def test_upload_sorting_and_selection(self):
        items = [
            {"source_relative": "first.mov", "upload_sequence": 1, "state": "ready"},
            {"source_relative": "third.mov", "upload_sequence": 3, "state": "queued"},
            {"source_relative": "second.mov", "upload_sequence": 2, "state": "ready"},
        ]
        newest = self.manager.sort_inventory(items, "upload-newest")
        self.assertEqual(
            ["third.mov", "second.mov", "first.mov"],
            [item["source_relative"] for item in newest],
        )
        self.assertEqual(newest[0], self.manager.select_item(newest, "1"))
        self.assertEqual(items[1], self.manager.select_item(items, "third"))

    def test_ssh_control_path_never_uses_spaced_config_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_config_path = self.manager.CONFIG_PATH
            old_control_directory = self.manager.CONTROL_DIRECTORY
            self.manager.CONFIG_PATH = (
                Path(temporary) / "Library" / "Application Support" / "manager.json"
            )
            self.manager.CONTROL_DIRECTORY = Path(temporary) / ".cache" / "manager"
            try:
                config = {
                    "host": "videos.example.com",
                    "ssh_user": "gallery-owner",
                    "remote_root": "/var/www/html/videos",
                    "identity_file": "",
                }
                for command in (
                    self.manager.ssh_base(config),
                    self.manager.scp_base(config),
                ):
                    option = next(
                        value for value in command
                        if value.startswith("ControlPath=")
                    )
                    self.assertNotIn(" ", option)
                    self.assertNotIn("Application Support", option)
            finally:
                self.manager.CONFIG_PATH = old_config_path
                self.manager.CONTROL_DIRECTORY = old_control_directory


if __name__ == "__main__":
    unittest.main()
