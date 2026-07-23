import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_PATH = REPO_ROOT / "site" / "_tools" / "scan.py"
PERMISSIONS_PATH = REPO_ROOT / "scripts" / "prepare-media-permissions.py"


def load_scan_module():
    spec = importlib.util.spec_from_file_location("hls_gallery_scan", SCAN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IngestOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scan = load_scan_module()

    def test_upload_order_is_persistent_and_new_files_join_the_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            media.mkdir()
            order_path = root / "data" / "ingest-order.json"

            older = media / "older.mp4"
            newer = media / "newer.mov"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            os.utime(older, (1_700_000_000, 1_700_000_000))
            os.utime(newer, (1_710_000_000, 1_710_000_000))

            videos = self.scan.discover_videos(media, self.scan.DEFAULT_EXTENSIONS)
            records = self.scan.update_ingest_order(order_path, videos, media)
            older_id = self.scan.source_id("older.mp4")
            newer_id = self.scan.source_id("newer.mov")
            self.assertEqual(1, records[older_id]["sequence"])
            self.assertEqual(2, records[newer_id]["sequence"])

            late = media / "late.webm"
            late.write_bytes(b"late")
            os.utime(late, (1_600_000_000, 1_600_000_000))
            before_observed = time.time()
            videos = self.scan.discover_videos(media, self.scan.DEFAULT_EXTENSIONS)
            records = self.scan.update_ingest_order(order_path, videos, media)
            late_id = self.scan.source_id("late.webm")
            self.assertEqual(3, records[late_id]["sequence"])
            uploaded_epoch = time.mktime(time.strptime(
                records[late_id]["uploaded_at"][:19], "%Y-%m-%dT%H:%M:%S"
            ))
            self.assertGreaterEqual(uploaded_epoch, before_observed - 24 * 3600)

            os.utime(older, None)
            records = self.scan.update_ingest_order(order_path, videos, media)
            self.assertEqual(1, records[older_id]["sequence"])

            newer.unlink()
            videos = self.scan.discover_videos(media, self.scan.DEFAULT_EXTENSIONS)
            records = self.scan.update_ingest_order(order_path, videos, media)
            self.assertNotIn(newer_id, records)
            saved = json.loads(order_path.read_text(encoding="utf-8"))
            self.assertNotIn(newer_id, saved["items"])

    def test_permission_helper_repairs_only_supported_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary)
            video = media / "upload.MOV"
            note = media / "notes.txt"
            video.write_bytes(b"video")
            note.write_text("keep private", encoding="utf-8")
            os.chmod(video, 0o600)
            os.chmod(note, 0o600)

            owner = pwd.getpwuid(os.getuid()).pw_name
            subprocess.run(
                [
                    sys.executable,
                    str(PERMISSIONS_PATH),
                    "--media-dir", str(media),
                    "--owner", owner,
                    "--quiet",
                ],
                check=True,
            )
            self.assertEqual(0o644, video.stat().st_mode & 0o777)
            self.assertEqual(0o600, note.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
