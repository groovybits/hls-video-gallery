import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = REPO_ROOT / "site" / "_tools" / "quality_analyzer.py"
MONITOR_PATH = REPO_ROOT / "site" / "_tools" / "encode_monitor.py"
CONFIGURE_PATH = REPO_ROOT / "scripts" / "configure.py"
EXAMPLE_CONFIG = REPO_ROOT / "config" / "gallery.example.json"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_key(prefix):
    character = format(prefix, "x")[-1]
    return character * 18 + "--" + character * 14


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def catalog_item(item_id, key, sequence, duration=10):
    return {
        "id": item_id,
        "cache_key": key,
        "source_relative": item_id + ".mp4",
        "title": item_id.title(),
        "upload_sequence": sequence,
        "duration_seconds": duration,
    }


def quality_record(worker, key, signature, elapsed=10, duration=10):
    return {
        "cache_key": key,
        "settings_signature": signature,
        "worker_version": worker.WORKER_VERSION,
        "analysis_seconds": elapsed,
        "duration_seconds": duration,
    }


def create_report_artifacts(root, key):
    report_root = root / "data" / "quality" / key
    report_root.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for filename in ("report.json", "frames.csv", "report.html"):
        path = report_root / filename
        path.write_text(filename + "\n", encoding="utf-8")
        stat_result = path.stat()
        artifacts[filename] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
        }
    return artifacts


class QualityWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_module("hls_gallery_quality_worker", WORKER_PATH)
        cls.monitor = load_module("hls_gallery_encode_monitor", MONITOR_PATH)

    def test_quality_ffmpeg_is_not_misreported_as_an_encoder(self):
        root = Path("/srv/example-gallery")
        quality_arguments = [
            "ffmpeg",
            "-i", "/srv/example-gallery/media/source.mov",
            "-i", "/srv/example-gallery/cache/current/hls/master.m3u8",
            "-filter_complex",
            "log_path=/srv/example-gallery/data/quality/.building-current/report.json",
        ]
        encoding_arguments = [
            "ffmpeg",
            "-i", "/srv/example-gallery/media/source.mov",
            "-hls_segment_filename",
            "/srv/example-gallery/cache/.building-current/hls/seg-%06d.ts",
        ]

        self.assertFalse(self.monitor.is_encoding_ffmpeg(quality_arguments, root))
        self.assertTrue(self.monitor.is_encoding_ffmpeg(encoding_arguments, root))

    def test_queue_reuses_current_cache_and_gates_content_and_cooldowns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            current_key = cache_key(1)
            waiting_key = cache_key(2)
            changed_key = cache_key(3)
            cooldown_key = cache_key(4)
            orphan_key = cache_key(5)
            old_changed_key = cache_key(6)
            items = [
                catalog_item("cooldown", cooldown_key, 40),
                catalog_item("changed", changed_key, 30),
                catalog_item("cached", current_key, 10),
                catalog_item("waiting", waiting_key, 20),
            ]
            write_json(data / "catalog.json", {"items": items})

            signature = "settings-current"
            configuration = {
                "signature": signature,
                "require_content_analysis": True,
                "expected_content_analyzer_version": "content-v2",
            }
            cached_record = quality_record(
                self.worker, current_key, signature, elapsed=5, duration=10
            )
            cached_record["artifacts"] = create_report_artifacts(root, current_key)
            write_json(data / "quality-index.json", {
                "items": {
                    "cached": cached_record,
                    "changed": quality_record(
                        self.worker, old_changed_key, signature
                    ),
                    "orphan": quality_record(
                        self.worker, orphan_key, signature
                    ),
                },
            })
            write_json(data / "content-index.json", {
                "analyzer_version": "content-v2",
                "items": {
                    "waiting": {
                        "cache_key": old_changed_key,
                        "analyzer_version": "content-v2",
                    },
                    "changed": {
                        "cache_key": changed_key,
                        "analyzer_version": "content-v2",
                    },
                    "cooldown": {
                        "cache_key": cooldown_key,
                        "analyzer_version": "content-v2",
                    },
                },
            })
            write_json(data / "quality-failures.json", {
                "items": {
                    cooldown_key: {
                        "retry_after_epoch": time.time() + 600,
                        "settings_signature": signature,
                        "error": "temporary failure",
                    },
                    orphan_key: {
                        "retry_after_epoch": time.time() + 600,
                        "error": "orphan",
                    },
                },
            })

            (
                _catalog,
                ordered,
                records,
                failures,
                pending,
                waiting_content,
                cooling_down,
            ) = self.worker.queue_state(root, configuration)

            self.assertEqual(
                ["cached", "waiting", "changed", "cooldown"],
                [item["id"] for item in ordered],
            )
            self.assertEqual(["cached"], list(records))
            self.assertEqual([cooldown_key], list(failures))
            self.assertEqual(["changed"], [item["id"] for item in pending])
            self.assertEqual(["waiting"], [item["id"] for item in waiting_content])
            self.assertEqual(["cooldown"], [item["id"] for item in cooling_down])

    def test_stale_content_index_cannot_unlock_quality_after_analyzer_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(6)
            item = catalog_item("video", key, 1)
            write_json(root / "data" / "catalog.json", {"items": [item]})
            write_json(root / "data" / "content-index.json", {
                "analyzer_version": "content-v1",
                "items": {
                    "video": {
                        "cache_key": key,
                        "analyzer_version": "content-v1",
                    },
                },
            })

            _, _, _, _, pending, waiting, cooling = self.worker.queue_state(
                root,
                {
                    "signature": "current",
                    "require_content_analysis": True,
                    "expected_content_analyzer_version": "content-v2",
                },
            )

            self.assertEqual([], pending)
            self.assertEqual(["video"], [value["id"] for value in waiting])
            self.assertEqual([], cooling)

    def test_settings_or_source_identity_change_invalidates_cached_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(7)
            item = catalog_item("video", key, 1)
            write_json(root / "data" / "catalog.json", {"items": [item]})
            write_json(root / "data" / "quality-index.json", {
                "items": {
                    "video": quality_record(
                        self.worker, key, "old-settings"
                    ),
                },
            })
            configuration = {
                "signature": "new-settings",
                "require_content_analysis": False,
            }

            _, _, records, _, pending, waiting, cooling = self.worker.queue_state(
                root, configuration
            )

            self.assertEqual({}, records)
            self.assertEqual(["video"], [value["id"] for value in pending])
            self.assertEqual([], waiting)
            self.assertEqual([], cooling)

    def test_binary_change_updates_settings_signature_and_clears_old_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "hls-quality-analyzer"
            binary.write_bytes(b"analyzer-v1")
            with mock.patch.dict(
                os.environ, {"VIDEO_QUALITY_BINARY": str(binary)}, clear=False
            ):
                first = self.worker.settings()
                binary.write_bytes(b"analyzer-v2")
                second = self.worker.settings()

            self.assertEqual(
                hashlib.sha256(b"analyzer-v1").hexdigest(),
                first["binary_sha256"],
            )
            self.assertNotEqual(first["signature"], second["signature"])

            key = cache_key(14)
            write_json(
                root / "data" / "catalog.json",
                {"items": [catalog_item("video", key, 1)]},
            )
            write_json(root / "data" / "quality-failures.json", {
                "items": {
                    key: {
                        "retry_after_epoch": time.time() + 600,
                        "settings_signature": first["signature"],
                    },
                },
            })
            _, _, _, failures, pending, _, cooling = self.worker.queue_state(
                root, {
                    "signature": second["signature"],
                    "require_content_analysis": False,
                },
            )
            self.assertEqual({}, failures)
            self.assertEqual(["video"], [item["id"] for item in pending])
            self.assertEqual([], cooling)

    def test_orchestration_settings_do_not_invalidate_metric_reports(self):
        with mock.patch.dict(os.environ, {
            "VIDEO_QUALITY_FAILURE_RETRY_SECONDS": "600",
            "VIDEO_QUALITY_REQUIRE_CONTENT": "false",
        }, clear=False):
            first = self.worker.settings()
        with mock.patch.dict(os.environ, {
            "VIDEO_QUALITY_FAILURE_RETRY_SECONDS": "7200",
            "VIDEO_QUALITY_REQUIRE_CONTENT": "true",
        }, clear=False):
            second = self.worker.settings()

        self.assertEqual(first["signature"], second["signature"])

    def test_missing_or_changed_artifact_invalidates_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(17)
            item = catalog_item("video", key, 1)
            write_json(root / "data" / "catalog.json", {"items": [item]})
            record = quality_record(self.worker, key, "current")
            record["artifacts"] = create_report_artifacts(root, key)
            write_json(root / "data" / "quality-index.json", {
                "items": {"video": record},
            })
            (root / "data" / "quality" / key / "frames.csv").write_text(
                "changed\n", encoding="utf-8"
            )

            _, _, records, _, pending, _, _ = self.worker.queue_state(
                root,
                {"signature": "current", "require_content_analysis": False},
            )

            self.assertEqual({}, records)
            self.assertEqual(["video"], [value["id"] for value in pending])

    def test_forecast_uses_observed_runtime_and_has_conservative_default(self):
        records = {
            "one": {"analysis_seconds": 5, "duration_seconds": 10},
            "two": {"analysis_seconds": 30, "duration_seconds": 20},
        }
        pending = [
            {"duration_seconds": 10},
            {"duration_seconds": 20},
        ]

        result = self.worker.forecast(records, pending)

        self.assertEqual(1.0, result["average_realtime_factor"])
        self.assertEqual(30, result["eta_seconds"])
        cold_start = self.worker.forecast({}, [{"duration_seconds": 30}])
        self.assertEqual(2.0, cold_start["average_realtime_factor"])
        self.assertEqual(60, cold_start["eta_seconds"])

    def test_resource_checks_detect_post_processing_encoding_and_load(self):
        root = Path("/srv/example-gallery")
        with mock.patch.object(
            self.worker,
            "process_cmdlines",
            return_value=[
                "python3 /srv/example-gallery/_tools/content_analyzer.py"
            ],
        ):
            self.assertEqual(
                "category analysis is active",
                self.worker.active_resource_reason(root),
            )
        with mock.patch.object(
            self.worker,
            "process_cmdlines",
            return_value=[
                "ffmpeg -i /srv/example-gallery/media/video.mp4"
            ],
        ):
            self.assertIn(
                "video encoding",
                self.worker.active_resource_reason(root),
            )
        with mock.patch.object(
            self.worker, "process_cmdlines", return_value=[]
        ), mock.patch.object(
            self.worker.os, "getloadavg", return_value=(3.0, 2.0, 1.0)
        ), mock.patch.dict(
            os.environ, {"VIDEO_QUALITY_MAX_LOAD": "1.5"}, clear=False
        ):
            self.assertIn(
                "3.00 exceeds 1.50",
                self.worker.active_resource_reason(root),
            )

    def test_nonblocking_lock_excludes_a_second_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quality.lock"
            first = self.worker.acquire_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(self.worker.acquire_lock(path))
            finally:
                first.close()
            second = self.worker.acquire_lock(path)
            self.assertIsNotNone(second)
            second.close()

    def test_source_symlink_is_rejected_before_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            media.mkdir()
            target = media / "target.mp4"
            target.write_bytes(b"video")
            source = media / "linked.mp4"
            source.symlink_to(target)
            key = cache_key(15)
            hls = root / "cache" / key / "hls"
            hls.mkdir(parents=True)
            (hls / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                self.worker.safe_item_paths(
                    root,
                    {
                        "source_relative": source.name,
                        "cache_key": key,
                    },
                )

    def test_library_percent_includes_items_waiting_for_content(self):
        item = catalog_item("waiting", cache_key(16), 1)
        payload = self.worker.progress_payload(
            "waiting", [item], {}, [], [item], [],
        )

        self.assertEqual(0.0, payload["percent"])
        self.assertEqual(1, payload["waiting_content_count"])

    def test_pruning_removes_only_recognized_stale_report_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live_key = cache_key(8)
            stale_key = cache_key(9)
            symlink_key = cache_key(11)
            live = root / live_key
            stale = root / stale_key
            unknown = root / "manual-report"
            building = root / (".building-" + stale_key)
            abandoned = root / (".building-" + cache_key(12) + "-abandoned")
            old_swap = root / (".old-" + cache_key(13) + "-12345")
            recent_old_swap = root / (".old-" + cache_key(14) + "-67890")
            malformed_old_swap = root / (".old-" + cache_key(15) + "-manual")
            symlink_target = root / "manual-target"
            for path in (
                live, stale, unknown, building, abandoned,
                old_swap, recent_old_swap, malformed_old_swap,
            ):
                path.mkdir()
                (path / "keep.txt").write_text(path.name, encoding="utf-8")
            old = time.time() - self.worker.ABANDONED_BUILD_SECONDS - 60
            os.utime(abandoned, (old, old))
            os.utime(old_swap, (old, old))
            os.utime(malformed_old_swap, (old, old))
            symlink_target.mkdir()
            symlink = root / symlink_key
            symlink.symlink_to(symlink_target, target_is_directory=True)
            regular_file = root / cache_key(10)
            regular_file.write_text("not a directory", encoding="utf-8")

            removed = self.worker.prune_reports(root, {live_key})

            self.assertEqual(3, removed)
            self.assertTrue(live.is_dir())
            self.assertFalse(stale.exists())
            self.assertTrue(unknown.is_dir())
            self.assertTrue(building.is_dir())
            self.assertFalse(abandoned.exists())
            self.assertFalse(old_swap.exists())
            self.assertTrue(recent_old_swap.is_dir())
            self.assertTrue(malformed_old_swap.is_dir())
            self.assertTrue(symlink.is_symlink())
            self.assertTrue(symlink_target.is_dir())
            self.assertTrue(regular_file.is_file())

    def test_in_progress_catalog_defers_before_index_or_report_pruning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            stale_key = cache_key(12)
            report = data / "quality" / stale_key
            report.mkdir(parents=True)
            (report / "report.json").write_text("{}", encoding="utf-8")
            index = data / "quality-index.json"
            original_index = '{"sentinel":"must remain unchanged"}\n'
            index.write_text(original_index, encoding="utf-8")
            write_json(data / "quality-analysis-progress.json", {
                "catalog_count": 20,
                "analyzed_count": 7,
                "pending_count": 11,
                "waiting_content_count": 2,
                "cooling_down_count": 1,
                "percent": 35.0,
                "upcoming": ["next", "later"],
                "eta_seconds": 900,
            })
            write_json(data / "catalog.json", {
                "scan": {"in_progress": True},
                "items": [catalog_item("partial", cache_key(13), 1)],
            })
            arguments = SimpleNamespace(
                root=str(root),
                status=False,
                watch=False,
                json=False,
                all=False,
                command=False,
                force=False,
                ignore_busy=False,
                prune_only=False,
                items=1,
                video_id=None,
            )

            with mock.patch.object(
                self.worker, "parse_arguments", return_value=arguments
            ), redirect_stdout(io.StringIO()):
                result = self.worker.main()

            self.assertEqual(0, result)
            self.assertEqual(original_index, index.read_text(encoding="utf-8"))
            self.assertTrue(report.is_dir())
            progress = json.loads(
                (data / "quality-analysis-progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("waiting_for_catalog", progress["phase"])
            self.assertEqual(20, progress["catalog_count"])
            self.assertEqual(7, progress["analyzed_count"])
            self.assertEqual(11, progress["pending_count"])
            self.assertEqual(2, progress["waiting_content_count"])
            self.assertEqual(["next", "later"], progress["upcoming"])
            self.assertEqual(900, progress["eta_seconds"])

    def test_scanner_lock_prevents_catalog_mutation_and_pruning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            stale_key = cache_key(18)
            report = data / "quality" / stale_key
            report.mkdir(parents=True)
            (report / "report.json").write_text("keep", encoding="utf-8")
            index = data / "quality-index.json"
            original_index = '{"sentinel":"unchanged"}\n'
            index.write_text(original_index, encoding="utf-8")
            write_json(data / "quality-analysis-progress.json", {
                "catalog_count": 20,
                "analyzed_count": 7,
                "pending_count": 11,
                "waiting_content_count": 2,
                "cooling_down_count": 1,
                "percent": 35.0,
                "upcoming": ["next", "later"],
                "eta_seconds": 900,
            })
            write_json(data / "catalog.json", {
                "scan": {"in_progress": False},
                "items": [catalog_item("partial", cache_key(19), 1)],
            })
            arguments = SimpleNamespace(
                root=str(root), status=False, watch=False, json=False,
                all=False, command=False, force=False, ignore_busy=False,
                prune_only=False, items=1, video_id=None,
            )
            scanner_lock = self.worker.acquire_lock(data / "scan.lock")
            self.assertIsNotNone(scanner_lock)
            try:
                with mock.patch.object(
                    self.worker, "parse_arguments", return_value=arguments
                ), redirect_stdout(io.StringIO()):
                    result = self.worker.main()
            finally:
                scanner_lock.close()

            self.assertEqual(0, result)
            self.assertEqual(original_index, index.read_text(encoding="utf-8"))
            self.assertTrue(report.is_dir())
            progress = json.loads(
                (data / "quality-analysis-progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(20, progress["catalog_count"])
            self.assertEqual(7, progress["analyzed_count"])
            self.assertEqual(11, progress["pending_count"])
            self.assertEqual(2, progress["waiting_content_count"])
            self.assertEqual(["next", "later"], progress["upcoming"])
            self.assertEqual(900, progress["eta_seconds"])

    def test_targeted_force_keeps_the_global_queue_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            items = [
                catalog_item("one", cache_key(20), 1),
                catalog_item("two", cache_key(21), 2),
                catalog_item("three", cache_key(22), 3),
            ]
            write_json(data / "catalog.json", {
                "scan": {"in_progress": False},
                "items": items,
            })
            arguments = SimpleNamespace(
                root=str(root), status=False, watch=False, json=False,
                all=False, command=False, force=True, ignore_busy=True,
                prune_only=False, items=1, video_id="two",
            )
            configuration = {
                "signature": "current",
                "require_content_analysis": False,
                "failure_retry_seconds": 60,
            }

            def fake_run_one(*_arguments, **_keywords):
                item = _arguments[1]
                create_report_artifacts(root, item["cache_key"])
                return {
                    "analyzer_version": "test",
                    "summary": {"score": 88.0, "band": "Very good"},
                }, 1.0

            with mock.patch.object(
                self.worker, "parse_arguments", return_value=arguments
            ), mock.patch.object(
                self.worker, "settings", return_value=configuration
            ), mock.patch.object(
                self.worker, "run_one", side_effect=fake_run_one
            ), redirect_stdout(io.StringIO()):
                result = self.worker.main()

            self.assertEqual(0, result)
            index = json.loads((data / "quality-index.json").read_text(encoding="utf-8"))
            progress = json.loads(
                (data / "quality-analysis-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, index["analyzed_count"])
            self.assertEqual(2, index["pending_count"])
            self.assertEqual(2, progress["pending_count"])
            self.assertEqual(3, progress["catalog_count"])
            self.assertEqual(["One", "Three"], progress["upcoming"])


class QualityConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configure = load_module("hls_gallery_configure_quality", CONFIGURE_PATH)

    def example(self):
        return json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    def test_quality_defaults_validate_with_two_thread_cap(self):
        values = self.configure.validate(
            REPO_ROOT, EXAMPLE_CONFIG, self.example()
        )

        self.assertEqual({
            "enabled": False,
            "items_per_run": 1,
            "interval_seconds": 300,
            "max_load": 1.5,
            "threads": 2,
            "frame_rate": 30,
            "scene_threshold": 10.0,
            "min_scene_seconds": 2.0,
            "failure_retry_seconds": 3600,
        }, values["quality_analysis"])

    def test_quality_threads_above_cap_are_rejected(self):
        config = self.example()
        config["quality_analysis"]["threads"] = 3

        with self.assertRaisesRegex(
            self.configure.ConfigError,
            r"quality_analysis\.threads must be between 1 and 2",
        ):
            self.configure.validate(REPO_ROOT, EXAMPLE_CONFIG, config)

    def test_render_publishes_feature_flag_and_service_settings(self):
        config = copy.deepcopy(self.example())
        config["quality_analysis"]["enabled"] = True
        config["content_analysis"]["enabled"] = True
        config["gallery"]["show_quality_analysis"] = True
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            config_path = temporary_root / "gallery.json"
            output = temporary_root / "rendered"
            write_json(config_path, config)

            payload = self.configure.render(
                REPO_ROOT, config_path, output
            )

            self.assertTrue(payload["quality_analysis_enabled"])
            public_config = json.loads(
                (output / "site" / "data" / "site-config.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(public_config["features"]["quality_analysis"])
            service = (
                output / "systemd" / "hls-gallery-quality.service"
            ).read_text(encoding="utf-8")
            self.assertIn("VIDEO_QUALITY_THREADS=2", service)
            self.assertIn("VIDEO_QUALITY_FRAME_RATE=30", service)
            self.assertIn("VIDEO_QUALITY_SCENE_THRESHOLD=10.0", service)
            self.assertIn("VIDEO_QUALITY_REQUIRE_CONTENT=true", service)
            expected_content_version = self.configure.validate(
                REPO_ROOT, config_path, config
            )["content_analysis"]["analyzer_version"]
            self.assertIn(
                "VIDEO_QUALITY_EXPECTED_CONTENT_VERSION={}".format(
                    expected_content_version
                ),
                service,
            )
            self.assertNotIn("@@", service)


if __name__ == "__main__":
    unittest.main()
