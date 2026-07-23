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
SCANNER_PATH = REPO_ROOT / "site" / "_tools" / "scan.py"
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


def create_report_artifacts(root, key, report=None):
    report_root = root / "data" / "quality" / key
    report_root.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for filename in ("report.json", "frames.csv", "report.html"):
        path = report_root / filename
        if filename == "report.json" and report is not None:
            path.write_text(json.dumps(report), encoding="utf-8")
        else:
            path.write_text(filename + "\n", encoding="utf-8")
        stat_result = path.stat()
        artifacts[filename] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
        }
    return artifacts


def synthetic_quality_report(values, scene_split=None):
    split = scene_split if scene_split is not None else max(1, len(values) // 2)
    frames = []
    for index, value in enumerate(values):
        frames.append({
            "frame": index,
            "time_seconds": float(index),
            "scene": 1 if index < split else 2,
            "composite": float(value),
            "vmaf_standard": float(value) + 1,
            "vmaf_phone": float(value) + 2,
            "ssim": float(value) / 100,
            "ssim_normalized": float(value),
            "psnr_y": 20 + float(value) * 0.3,
            "psnr_normalized": float(value),
            "phash_similarity": float(value) + 3,
            "temporal_consistency": float(value) + 4,
        })
    scenes = [{
        "index": 1,
        "start_frame": 0,
        "end_frame": split,
        "start_seconds": 0,
        "end_seconds": float(split),
        "duration_seconds": float(split),
        "scene_change_strength": 0,
    }]
    if split < len(values):
        scenes.append({
            "index": 2,
            "start_frame": split,
            "end_frame": len(values),
            "start_seconds": float(split),
            "end_seconds": float(len(values)),
            "duration_seconds": float(len(values) - split),
            "scene_change_strength": 22.5,
        })
    return {
        "schema_version": 1,
        "analyzer_version": "1.1.0",
        "generated_at": "2026-07-23T12:00:00Z",
        "hdr_normalized": True,
        "summary": {
            "score": 72.5,
            "band": "Good",
            "vmaf_standard": 75.0,
            "vmaf_phone": 80.0,
            "ssim": 0.95,
            "ssim_normalized": 95.0,
            "psnr_y": 38.0,
            "psnr_normalized": 60.0,
            "phash_similarity": 96.0,
            "temporal_consistency": 97.0,
        },
        "video": {
            "width": 1920,
            "height": 1080,
            "duration_seconds": float(len(values)),
            "frames_analyzed": len(values),
            "reference_source_fps": 1,
            "distorted_source_fps": 1,
        },
        "frames": frames,
        "scenes": scenes,
    }


def create_hls_media_cache(root, item, durations, media_sequence=0):
    variant = {
        "name": "1080p",
        "playlist": "1080p/index.m3u8",
        "width": 1920,
        "height": 1080,
        "frame_rate": 30,
        "video_bitrate": 6_500_000,
        "audio_bitrate": 160_000,
        "bandwidth": 7_180_000,
    }
    item["hls_variants"] = [variant]
    media_root = root / "cache" / item["cache_key"] / "hls"
    playlist = media_root / variant["playlist"]
    playlist.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:{}".format(int(max(durations) + 0.999)),
        "#EXT-X-MEDIA-SEQUENCE:{}".format(media_sequence),
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index, duration in enumerate(durations):
        filename = "seg-{:06d}.ts".format(index)
        (playlist.parent / filename).write_bytes(
            bytes([index + 1]) * (index + 2)
        )
        lines.extend([
            "#EXTINF:{:.6f},".format(duration),
            filename,
        ])
    lines.append("#EXT-X-ENDLIST")
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist


class QualityWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_module("hls_gallery_quality_worker", WORKER_PATH)
        cls.monitor = load_module("hls_gallery_encode_monitor", MONITOR_PATH)
        cls.scanner = load_module("hls_gallery_scanner", SCANNER_PATH)

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
        with mock.patch.object(
            self.worker, "process_cmdlines", return_value=[]
        ), mock.patch.object(
            self.worker.os, "getloadavg", return_value=(99.0, 98.0, 97.0)
        ), mock.patch.dict(
            os.environ, {"VIDEO_QUALITY_MAX_LOAD": "0"}, clear=False
        ):
            self.assertEqual("", self.worker.active_resource_reason(root))

    def test_highest_hls_variant_is_selected_and_cannot_escape_hls_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(23)
            source = root / "media" / "source.mp4"
            source.parent.mkdir()
            source.write_bytes(b"source")
            hls_root = root / "cache" / key / "hls"
            low = hls_root / "360p" / "index.m3u8"
            high = hls_root / "1080p" / "index.m3u8"
            low.parent.mkdir(parents=True)
            high.parent.mkdir(parents=True)
            low.write_text("#EXTM3U\n", encoding="utf-8")
            high.write_text("#EXTM3U\n", encoding="utf-8")
            item = {
                "source_relative": source.name,
                "cache_key": key,
                "hls_variants": [
                    {
                        "width": 640,
                        "height": 360,
                        "video_bitrate": 800_000,
                        "playlist": "360p/index.m3u8",
                    },
                    {
                        "width": 1920,
                        "height": 1080,
                        "video_bitrate": 6_500_000,
                        "playlist": "1080p/index.m3u8",
                    },
                ],
            }

            selected_source, selected_playlist = self.worker.safe_item_paths(
                root, item
            )

            self.assertEqual(source.resolve(), selected_source)
            self.assertEqual(high.resolve(), selected_playlist)

            unsafe = copy.deepcopy(item)
            unsafe["hls_variants"][1]["playlist"] = "../../outside.m3u8"
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                self.worker.safe_item_paths(root, unsafe)

            escaped_root = root / "escaped-cache"
            escaped_root.mkdir()
            escaped_item = copy.deepcopy(item)
            escaped_item["cache_key"] = cache_key(28)
            cache_directory = root / "cache" / escaped_item["cache_key"]
            cache_directory.symlink_to(escaped_root, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                self.worker.safe_item_paths(root, escaped_item)

            hls_escape_item = copy.deepcopy(item)
            hls_escape_item["cache_key"] = cache_key(29)
            hls_cache = root / "cache" / hls_escape_item["cache_key"]
            hls_cache.mkdir()
            (hls_cache / "hls").symlink_to(
                escaped_root, target_is_directory=True
            )
            with self.assertRaisesRegex(RuntimeError, "HLS directory"):
                self.worker.safe_item_paths(root, hls_escape_item)

    def test_terminal_state_poll_is_throttled_without_delaying_active_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = Path(temporary) / "progress.json"
            with mock.patch.object(self.worker.time, "time", return_value=100.0):
                write_json(progress, {
                    "state": "complete",
                    "updated_at": "1970-01-01T00:01:20Z",
                })
                with mock.patch.object(self.worker.time, "sleep") as sleep:
                    delay = self.worker.throttle_idle_poll(progress)
                self.assertEqual(10.0, delay)
                sleep.assert_called_once_with(10.0)

                write_json(progress, {
                    "state": "idle",
                    "updated_at": "1970-01-01T00:01:39Z",
                })
                with mock.patch.object(self.worker.time, "sleep") as sleep:
                    self.assertEqual(
                        0.0, self.worker.throttle_idle_poll(progress)
                    )
                sleep.assert_not_called()

    def test_encoded_output_timestamp_invalidates_stale_and_supports_legacy_records(self):
        item = {"processed_at": "2026-07-23T12:00:00Z"}
        configuration = {"signature": "current"}
        current_record = quality_record(
            self.worker, cache_key(27), configuration["signature"]
        )
        current_item = {
            "cache_key": current_record["cache_key"],
            "processed_at": item["processed_at"],
        }
        current_record["encoded_at"] = item["processed_at"]

        self.assertTrue(self.worker.encoded_output_current(
            item,
            {
                "encoded_at": "2026-07-23T12:00:00Z",
                "analyzed_at": "2026-07-23T12:01:00Z",
            },
        ))
        self.assertFalse(self.worker.encoded_output_current(
            item,
            {
                "encoded_at": "2026-07-23T11:59:59Z",
                "analyzed_at": "2026-07-23T12:01:00Z",
            },
        ))
        self.assertTrue(self.worker.encoded_output_current(
            item, {"analyzed_at": "2026-07-23T12:00:01Z"}
        ))
        self.assertFalse(self.worker.encoded_output_current(
            item, {"analyzed_at": "2026-07-23T11:59:59Z"}
        ))
        self.assertFalse(self.worker.encoded_output_current(
            item, {"analyzed_at": "not-a-timestamp"}
        ))
        self.assertTrue(self.worker.encoded_output_current(
            {}, {"encoded_at": "an-old-encoding"}
        ))
        self.assertTrue(self.worker.valid_record(
            current_item, current_record, configuration
        ))
        current_record["encoded_at"] = "2026-07-23T11:59:59Z"
        self.assertFalse(self.worker.valid_record(
            current_item, current_record, configuration
        ))

    def test_interlace_detection_and_analyzer_flag_plumbing(self):
        self.assertFalse(self.worker.source_is_interlaced({}))
        self.assertFalse(self.worker.source_is_interlaced({
            "video_streams": [{"field_order": "progressive"}],
        }))
        self.assertFalse(self.worker.source_is_interlaced({
            "video_streams": [{"field_order": "UNKNOWN"}],
        }))
        self.assertTrue(self.worker.source_is_interlaced({
            "video_streams": [{"field_order": "tt"}],
        }))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "hls-quality-analyzer"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            configuration = {
                "threads": 2,
                "frame_rate": 30,
                "scene_threshold": 10.0,
                "min_scene_seconds": 2.0,
            }
            item = catalog_item("interlaced", cache_key(24), 1)
            item["primary_video_stream_index"] = 4
            item["video_streams"] = [
                {
                    "index": 1,
                    "field_order": "progressive",
                    "default": False,
                    "width": 320,
                    "height": 180,
                },
                {
                    "index": 4,
                    "field_order": "bb",
                    "default": True,
                    "width": 1920,
                    "height": 1080,
                },
            ]
            with mock.patch.dict(
                os.environ, {"VIDEO_QUALITY_BINARY": str(binary)}, clear=False
            ), mock.patch.object(
                self.worker,
                "safe_item_paths",
                return_value=(root / "source.mov", root / "encoded.m3u8"),
            ), mock.patch.object(
                self.worker.subprocess,
                "Popen",
                side_effect=RuntimeError("capture command"),
            ) as popen:
                with self.assertRaisesRegex(RuntimeError, "capture command"):
                    self.worker.run_one(
                        root,
                        item,
                        configuration,
                        root / "data" / "progress.json",
                        [item],
                        {},
                        [item],
                        [],
                        [],
                    )

            command = popen.call_args.args[0]
            self.assertIn("--deinterlace-reference", command)
            stream_option = command.index("--reference-stream-index")
            self.assertEqual("4", command[stream_option + 1])

            item["video_streams"][1]["field_order"] = "progressive"
            with mock.patch.dict(
                os.environ, {"VIDEO_QUALITY_BINARY": str(binary)}, clear=False
            ), mock.patch.object(
                self.worker,
                "safe_item_paths",
                return_value=(root / "source.mov", root / "encoded.m3u8"),
            ), mock.patch.object(
                self.worker.subprocess,
                "Popen",
                side_effect=RuntimeError("capture progressive command"),
            ) as progressive_popen:
                with self.assertRaisesRegex(
                    RuntimeError, "capture progressive command"
                ):
                    self.worker.run_one(
                        root,
                        item,
                        configuration,
                        root / "data" / "progress.json",
                        [item],
                        {},
                        [item],
                        [],
                        [],
                    )
            progressive_command = progressive_popen.call_args.args[0]
            self.assertNotIn("--deinterlace-reference", progressive_command)
            stream_option = progressive_command.index("--reference-stream-index")
            self.assertEqual("4", progressive_command[stream_option + 1])

    def test_encoder_selected_global_video_stream_is_derived_for_legacy_items(self):
        streams = [
            {
                "index": 7,
                "codec_type": "video",
                "default": True,
                "width": 3000,
                "height": 3000,
                "attached_pic": True,
                "field_order": "progressive",
            },
            {
                "index": 2,
                "codec_type": "video",
                "default": False,
                "width": 3840,
                "height": 2160,
                "attached_pic": False,
                "field_order": "progressive",
            },
            {
                "index": 5,
                "codec_type": "video",
                "default": True,
                "width": 1280,
                "height": 720,
                "attached_pic": False,
                "field_order": "tt",
            },
        ]
        legacy = {"video_streams": streams}

        self.assertEqual(5, self.worker.reference_stream_index(legacy))
        self.assertTrue(self.worker.source_is_interlaced(legacy))
        self.assertEqual(
            5,
            self.scanner.cached_primary_video_stream(streams)["index"],
        )

        explicit = dict(legacy, primary_video_stream_index=2)
        self.assertEqual(2, self.worker.reference_stream_index(explicit))
        self.assertFalse(self.worker.source_is_interlaced(explicit))

    def test_scanner_records_and_backfills_primary_video_stream_metadata(self):
        raw_attached = {
            "index": 9,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 640,
            "height": 640,
            "disposition": {"attached_pic": 1, "default": 1},
        }
        cleaned = self.scanner.clean_stream(raw_attached)
        self.assertTrue(cleaned["attached_pic"])

        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary)
            key = cache_key(29)
            (cache_root / key).mkdir()
            item = {
                "cache_key": key,
                "video_streams": [
                    cleaned,
                    {
                        "index": 3,
                        "codec_type": "video",
                        "default": True,
                        "width": 1920,
                        "height": 1080,
                        "attached_pic": False,
                    },
                ],
            }
            enriched, changed = self.scanner.add_cached_primary_video_stream_index(
                item, cache_root
            )
            self.assertTrue(changed)
            self.assertEqual(3, enriched["primary_video_stream_index"])
            saved = json.loads(
                (cache_root / key / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(3, saved["primary_video_stream_index"])

    def test_expired_failure_rejoins_queue_without_starving_new_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed = catalog_item("failed-first", cache_key(25), 1)
            ready = catalog_item("ready-second", cache_key(26), 2)
            write_json(
                root / "data" / "catalog.json",
                {"items": [failed, ready]},
            )
            configuration = {
                "signature": "current",
                "require_content_analysis": False,
            }
            failure_path = root / "data" / "quality-failures.json"
            write_json(failure_path, {
                "items": {
                    failed["cache_key"]: {
                        "retry_after_epoch": time.time() + 30,
                        "settings_signature": configuration["signature"],
                    },
                },
            })

            _, _, _, _, pending, _, cooling = self.worker.queue_state(
                root, configuration
            )

            self.assertEqual(["ready-second"], [item["id"] for item in pending])
            self.assertEqual(["failed-first"], [item["id"] for item in cooling])

            write_json(failure_path, {
                "items": {
                    failed["cache_key"]: {
                        "retry_after_epoch": time.time() - 1,
                        "settings_signature": configuration["signature"],
                    },
                },
            })
            _, _, _, _, pending, _, cooling = self.worker.queue_state(
                root, configuration
            )
            self.assertEqual(
                ["ready-second", "failed-first"],
                [item["id"] for item in pending],
            )
            self.assertEqual([], cooling)

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

    def test_hls_media_playlist_parser_uses_exact_extinf_and_media_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = catalog_item("video", cache_key(30), 1)
            playlist = create_hls_media_cache(
                root, item, [6.006, 3.25], media_sequence=41
            )
            hls_root = root / "cache" / item["cache_key"] / "hls"

            parsed = self.worker.parse_hls_media_playlist(
                playlist, hls_root
            )

            self.assertEqual(41, parsed["media_sequence"])
            self.assertAlmostEqual(9.256, parsed["duration_seconds"])
            self.assertEqual(
                [41, 42],
                [segment["sequence"] for segment in parsed["segments"]],
            )
            self.assertEqual(
                ["seg-000000.ts", "seg-000001.ts"],
                [segment["uri"] for segment in parsed["segments"]],
            )
            self.assertAlmostEqual(
                6.006, parsed["segments"][1]["start_seconds"]
            )
            self.assertAlmostEqual(
                3.25, parsed["segments"][1]["duration_seconds"]
            )
            self.assertEqual(2, parsed["segments"][0]["size_bytes"])
            self.assertEqual(3, parsed["segments"][1]["size_bytes"])

            escaped = playlist.parent / "unsafe.m3u8"
            escaped.write_text(
                "#EXTM3U\n#EXTINF:1.0,\n../escape.ts\n",
                encoding="utf-8",
            )
            (hls_root / "escape.ts").write_bytes(b"unsafe")
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                self.worker.parse_hls_media_playlist(escaped, hls_root)

    def test_dashboard_derives_all_scene_and_hls_metric_aggregates(self):
        report = synthetic_quality_report(
            [100, 80, 60, 40, 20, 0], scene_split=2
        )
        report["preprocessing"] = {
            "reference_deinterlace": True,
            "reference_deinterlace_filter": "yadif=deint=interlaced",
        }
        report["warnings"] = ["Source and output durations differ."]
        item = catalog_item("video", cache_key(31), 1, duration=6)
        variant = {
            "name": "1080p",
            "playlist": "1080p/index.m3u8",
            "width": 1920,
            "height": 1080,
            "frame_rate": 30,
            "video_bitrate": 6_500_000,
        }
        playlist_data = {
            "media_sequence": 7,
            "target_duration_seconds": 3,
            "duration_seconds": 6,
            "segments": [
                {
                    "index": 0,
                    "sequence": 7,
                    "uri": "seg-000000.ts",
                    "start_seconds": 0,
                    "end_seconds": 3,
                    "duration_seconds": 3,
                    "size_bytes": 300,
                },
                {
                    "index": 1,
                    "sequence": 8,
                    "uri": "seg-000001.ts",
                    "start_seconds": 3,
                    "end_seconds": 6,
                    "duration_seconds": 3,
                    "size_bytes": 600,
                },
            ],
        }

        dashboard = self.worker.build_quality_dashboard(
            report,
            item,
            variant,
            playlist_data,
            "fingerprint",
            {"report": {"size": 10}, "media_playlist": {"size": 20}},
        )

        self.assertEqual(
            self.worker.DASHBOARD_SCHEMA_VERSION,
            dashboard["schema_version"],
        )
        self.assertTrue(dashboard["hdr_normalized"])
        self.assertEqual(report["summary"], dashboard["summary"])
        self.assertEqual(
            report["preprocessing"],
            dashboard["report_metadata"]["preprocessing"],
        )
        self.assertEqual(
            report["warnings"],
            dashboard["report_metadata"]["warnings"],
        )
        self.assertEqual(7, dashboard["rendition"]["media_sequence"])
        self.assertEqual(2, dashboard["rendition"]["segment_count"])
        self.assertEqual(2, len(dashboard["scenes"]))
        self.assertEqual(2, len(dashboard["hls_segments"]))

        first_segment = dashboard["hls_segments"][0]
        self.assertEqual(3, first_segment["frame_count"])
        self.assertEqual([1, 2], first_segment["scene_indexes"])
        self.assertAlmostEqual(
            80.0, first_segment["metrics"]["composite"]["mean"]
        )
        self.assertAlmostEqual(
            60.0,
            first_segment["metrics"]["composite"]["worst_decile"],
        )
        self.assertAlmostEqual(60.0, first_segment["metrics"]["composite"]["min"])
        self.assertAlmostEqual(100.0, first_segment["metrics"]["composite"]["max"])
        self.assertAlmostEqual(74.0, first_segment["score"])
        self.assertEqual("Good", first_segment["band"])
        self.assertEqual(800, first_segment["bitrate_bps"])

        second_segment = dashboard["hls_segments"][1]
        self.assertAlmostEqual(14.0, second_segment["score"])
        self.assertEqual("Poor", second_segment["band"])
        for metric in self.worker.DASHBOARD_METRICS:
            self.assertEqual(
                {"mean", "worst_decile", "min", "max"},
                set(second_segment["metrics"][metric]),
            )

        first_scene = dashboard["scenes"][0]
        self.assertEqual(2, first_scene["frame_count"])
        self.assertAlmostEqual(87.0, first_scene["score"])
        self.assertEqual("Very good", first_scene["band"])
        self.assertEqual(
            len(report["frames"]),
            dashboard["overview"]["source_frame_count"],
        )
        self.assertEqual(
            len(report["frames"]),
            dashboard["overview"]["point_count"],
        )
        self.assertTrue(all(
            metric in dashboard["overview"]["points"][0]
            for metric in self.worker.DASHBOARD_METRICS
        ))

    def test_metric_aware_overview_is_capped_and_keeps_each_metric_outlier(self):
        frames = []
        outliers = {}
        for index in range(9000):
            frame = {
                "frame": index,
                "time_seconds": index / 30,
                "scene_index": 1,
            }
            for metric_index, metric in enumerate(
                self.worker.DASHBOARD_METRICS
            ):
                outlier = 100 + metric_index * 500
                outliers[metric] = outlier
                frame[metric] = (
                    -1000.0 - metric_index
                    if index == outlier
                    else 50.0 + metric_index
                )
            frames.append(frame)
        segments = [{
            "index": 0,
            "start_seconds": 0,
            "end_seconds": 1000,
        }]

        points = self.worker.metric_aware_overview_points(frames, segments)
        selected = {point["frame"] for point in points}

        self.assertLessEqual(
            len(points), self.worker.DASHBOARD_POINT_LIMIT
        )
        self.assertIn(0, selected)
        self.assertIn(8999, selected)
        for metric, frame in outliers.items():
            with self.subTest(metric=metric):
                self.assertIn(frame, selected)

    def test_idle_backfill_is_cached_and_never_mutates_report_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = catalog_item("good", cache_key(32), 1, duration=6)
            bad = catalog_item("bad", cache_key(33), 2, duration=6)
            playlist = create_hls_media_cache(
                root, good, [3, 3], media_sequence=12
            )
            signature = "current"
            records = {}
            original_states = {}
            for item in (good, bad):
                report = synthetic_quality_report(
                    [90, 80, 70, 60, 50, 40], scene_split=3
                )
                report["gallery"] = {
                    "video_id": item["id"],
                    "cache_key": item["cache_key"],
                }
                record = quality_record(
                    self.worker, item["cache_key"], signature
                )
                record["artifacts"] = create_report_artifacts(
                    root, item["cache_key"], report
                )
                records[item["id"]] = record
                original_states[item["id"]] = copy.deepcopy(
                    record["artifacts"]
                )
            write_json(root / "data" / "catalog.json", {
                "scan": {"in_progress": False},
                "items": [good, bad],
            })
            write_json(root / "data" / "quality-index.json", {
                "items": records,
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
            configuration = {
                "signature": signature,
                "require_content_analysis": False,
            }

            with mock.patch.object(
                self.worker, "parse_arguments", return_value=arguments
            ), mock.patch.object(
                self.worker, "settings", return_value=configuration
            ), mock.patch.object(
                self.worker, "run_one"
            ) as run_one, redirect_stdout(io.StringIO()):
                result = self.worker.main()

            self.assertEqual(0, result)
            run_one.assert_not_called()
            dashboard_path = (
                root / "data" / "quality" / good["cache_key"]
                / "dashboard.json"
            )
            self.assertTrue(dashboard_path.is_file())
            self.assertFalse(
                (
                    root / "data" / "quality" / bad["cache_key"]
                    / "dashboard.json"
                ).exists()
            )
            first_dashboard_state = dashboard_path.stat()
            self.assertFalse(self.worker.ensure_quality_dashboard(root, good))
            self.assertEqual(
                first_dashboard_state.st_mtime_ns,
                dashboard_path.stat().st_mtime_ns,
            )
            first_fingerprint = json.loads(
                dashboard_path.read_text(encoding="utf-8")
            )["fingerprint"]
            playlist.write_text(
                playlist.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            self.assertTrue(self.worker.ensure_quality_dashboard(root, good))
            self.assertNotEqual(
                first_fingerprint,
                json.loads(
                    dashboard_path.read_text(encoding="utf-8")
                )["fingerprint"],
            )
            self.assertEqual(0o644, dashboard_path.stat().st_mode & 0o777)

            saved_index = json.loads(
                (root / "data" / "quality-index.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(2, saved_index["analyzed_count"])
            self.assertEqual(0, saved_index["pending_count"])
            for item in (good, bad):
                self.assertEqual(
                    original_states[item["id"]],
                    saved_index["items"][item["id"]]["artifacts"],
                )
                self.assertNotIn(
                    "dashboard.json",
                    saved_index["items"][item["id"]]["artifacts"],
                )
                self.assertTrue(self.worker.report_artifacts_ready(
                    root, item, saved_index["items"][item["id"]]
                ))
            _, _, current, _, pending, _, _ = self.worker.queue_state(
                root, configuration
            )
            self.assertEqual({"bad", "good"}, set(current))
            self.assertEqual([], pending)
            self.assertEqual(
                "gallery-quality-v2", self.worker.WORKER_VERSION
            )
            self.assertEqual(
                ("report.json", "frames.csv", "report.html"),
                self.worker.REPORT_ARTIFACTS,
            )
            self.assertEqual(
                ("report.json", "frames.csv"),
                self.worker.MEASUREMENT_ARTIFACTS,
            )

    def test_standalone_html_is_replaceable_without_remeasuring(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(34)
            item = catalog_item("video", key, 1)
            record = quality_record(self.worker, key, "current")
            record["artifacts"] = create_report_artifacts(
                root, key, synthetic_quality_report([90, 80, 70])
            )
            report_root = root / "data" / "quality" / key

            (report_root / "report.html").write_text(
                "replacement presentation\n", encoding="utf-8"
            )
            self.assertTrue(
                self.worker.report_artifacts_ready(root, item, record)
            )

            (report_root / "frames.csv").write_text(
                "changed measurement\n", encoding="utf-8"
            )
            self.assertFalse(
                self.worker.report_artifacts_ready(root, item, record)
            )

    def test_standalone_report_backfill_is_fingerprinted_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = catalog_item("video", cache_key(35), 1, duration=6)
            create_hls_media_cache(root, item, [3, 3], media_sequence=20)
            report = synthetic_quality_report(
                [92, 84, 76, 68, 60, 52], scene_split=3
            )
            report["gallery"] = {
                "video_id": item["id"],
                "cache_key": item["cache_key"],
            }
            record = quality_record(self.worker, item["cache_key"], "current")
            record["artifacts"] = create_report_artifacts(
                root, item["cache_key"], report
            )
            self.assertTrue(self.worker.ensure_quality_dashboard(root, item))

            renderer = root / "quality-report-renderer"
            renderer.write_text(
                "#!/usr/bin/env python3\n"
                "import argparse\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--report-json', required=True)\n"
                "parser.add_argument('--dashboard-json')\n"
                "parser.add_argument('--output', required=True)\n"
                "parser.add_argument('--fingerprint', required=True)\n"
                "parser.add_argument('--title')\n"
                "args = parser.parse_args()\n"
                "Path(args.output).write_text(\n"
                "    '<!doctype html><head>'\n"
                "    '<meta name=\"quality-report-renderer\" content=\"2\">'\n"
                "    '<meta name=\"quality-report-fingerprint\" content=\"' +\n"
                "    args.fingerprint + '\"></head><body></body>',\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            renderer.chmod(0o755)
            environment = {
                "VIDEO_QUALITY_REPORT_RENDERER": str(renderer),
            }
            html_path = (
                root / "data" / "quality" / item["cache_key"] / "report.html"
            )
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertTrue(
                    self.worker.ensure_standalone_report(root, item)
                )
                first_state = html_path.stat()
                first_fingerprint = (
                    self.worker.embedded_standalone_report_fingerprint(
                        html_path
                    )
                )
                self.assertRegex(first_fingerprint or "", r"^[0-9a-f]{64}$")
                self.assertFalse(
                    self.worker.ensure_standalone_report(root, item)
                )
                self.assertEqual(
                    first_state.st_mtime_ns, html_path.stat().st_mtime_ns
                )
                html_path.chmod(0o600)
                self.assertFalse(
                    self.worker.ensure_standalone_report(root, item)
                )
                self.assertEqual(0o644, html_path.stat().st_mode & 0o777)

                item["title"] = "A renamed video"
                self.assertTrue(
                    self.worker.ensure_standalone_report(root, item)
                )
                renamed_fingerprint = (
                    self.worker.embedded_standalone_report_fingerprint(
                        html_path
                    )
                )
                self.assertNotEqual(first_fingerprint, renamed_fingerprint)

                dashboard_path = html_path.with_name("dashboard.json")
                dashboard_path.write_text(
                    dashboard_path.read_text(encoding="utf-8") + "\n",
                    encoding="utf-8",
                )
                self.assertTrue(
                    self.worker.ensure_standalone_report(root, item)
                )
                self.assertNotEqual(
                    first_fingerprint,
                    self.worker.embedded_standalone_report_fingerprint(
                        html_path
                    ),
                )

                result = self.worker.backfill_standalone_reports(
                    root, [item], {"video": record}
                )
            self.assertEqual(0, result["generated"])
            self.assertEqual(1, result["cached"])
            self.assertEqual([], result["errors"])
            self.assertEqual(
                self.worker.artifact_state(html_path),
                record["artifacts"]["report.html"],
            )
            self.assertTrue(
                self.worker.report_artifacts_ready(root, item, record)
            )

    def test_render_reports_only_preserves_measurements_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = catalog_item("video", cache_key(36), 1, duration=6)
            create_hls_media_cache(root, item, [3, 3], media_sequence=30)
            report = synthetic_quality_report(
                [92, 84, 76, 68, 60, 52], scene_split=3
            )
            report["gallery"] = {
                "video_id": item["id"],
                "cache_key": item["cache_key"],
            }
            record = quality_record(
                self.worker, item["cache_key"], "older-analyzer-signature"
            )
            record["artifacts"] = create_report_artifacts(
                root, item["cache_key"], report
            )
            write_json(root / "data" / "catalog.json", {
                "scan": {"in_progress": False},
                "items": [item],
            })
            index_path = root / "data" / "quality-index.json"
            write_json(index_path, {
                "settings_signature": "older-analyzer-signature",
                "items": {item["id"]: record},
            })
            report_root = (
                root / "data" / "quality" / item["cache_key"]
            )
            immutable_before = {
                name: (report_root / name).read_bytes()
                for name in self.worker.MEASUREMENT_ARTIFACTS
            }
            index_before = index_path.read_bytes()

            renderer = root / "quality-report-renderer"
            renderer.write_text(
                "#!/usr/bin/env python3\n"
                "import argparse\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--report-json', required=True)\n"
                "parser.add_argument('--dashboard-json')\n"
                "parser.add_argument('--output', required=True)\n"
                "parser.add_argument('--fingerprint', required=True)\n"
                "parser.add_argument('--title')\n"
                "args = parser.parse_args()\n"
                "Path(args.output).write_text(\n"
                "    '<meta name=\"quality-report-renderer\" content=\"2\">'\n"
                "    '<meta name=\"quality-report-fingerprint\" content=\"' +\n"
                "    args.fingerprint + '\">', encoding='utf-8')\n",
                encoding="utf-8",
            )
            renderer.chmod(0o755)
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
                render_reports_only=True,
                items=1,
                video_id=None,
            )
            with mock.patch.dict(
                os.environ,
                {"VIDEO_QUALITY_REPORT_RENDERER": str(renderer)},
                clear=False,
            ), mock.patch.object(
                self.worker, "parse_arguments", return_value=arguments
            ), mock.patch.object(
                self.worker, "settings"
            ) as settings, mock.patch.object(
                self.worker, "run_one"
            ) as run_one, mock.patch.object(
                self.worker, "prune_reports"
            ) as prune_reports, mock.patch.object(
                self.worker, "throttle_idle_poll"
            ) as throttle, mock.patch.object(
                self.worker.sys,
                "argv",
                ["/usr/local/bin/hls-gallery-quality-status-example"],
            ), redirect_stdout(io.StringIO()):
                result = self.worker.main()

            self.assertEqual(0, result)
            settings.assert_not_called()
            run_one.assert_not_called()
            prune_reports.assert_not_called()
            throttle.assert_not_called()
            self.assertEqual(index_before, index_path.read_bytes())
            for name, content in immutable_before.items():
                self.assertEqual(content, (report_root / name).read_bytes())
            self.assertTrue((report_root / "dashboard.json").is_file())
            self.assertEqual(
                0o644,
                (report_root / "report.html").stat().st_mode & 0o777,
            )

    def test_existing_report_metrics_are_backfilled_without_reanalysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = cache_key(16)
            item = catalog_item("video", key, 1)
            signature = "current"
            report = {
                "gallery": {"video_id": "video", "cache_key": key},
                "hdr_normalized": True,
                "summary": {
                    "score": 87.654,
                    "band": "Very good",
                    "vmaf_standard": 91.234,
                    "vmaf_phone": 96.5,
                    "psnr_y": 42.345,
                    "ssim": 0.987654321,
                    "phash_similarity": 97.456,
                    "temporal_consistency": 98.765,
                },
            }
            record = quality_record(self.worker, key, signature)
            record["analyzed_at"] = "2026-07-23T12:00:00Z"
            record["artifacts"] = create_report_artifacts(root, key, report)
            write_json(root / "data" / "catalog.json", {"items": [item]})
            write_json(root / "data" / "quality-index.json", {
                "items": {"video": record},
            })

            _, _, records, _, pending, _, _ = self.worker.queue_state(
                root,
                {"signature": signature, "require_content_analysis": False},
            )

            self.assertEqual([], pending)
            self.assertEqual(87.65, records["video"]["summary"]["score"])
            self.assertEqual(91.23, records["video"]["summary"]["vmaf_standard"])
            self.assertEqual(0.987654, records["video"]["summary"]["ssim"])
            self.assertTrue(records["video"]["hdr_normalized"])

    def test_idle_progress_includes_the_newest_current_result(self):
        first = catalog_item("first", cache_key(17), 1)
        second = catalog_item("second", cache_key(18), 2)
        records = {
            "first": {
                "cache_key": first["cache_key"],
                "analyzed_at": "2026-07-23T12:00:00Z",
                "score": 70,
                "band": "Good",
                "summary": {"score": 70, "band": "Good", "vmaf_standard": 75},
            },
            "second": {
                "cache_key": second["cache_key"],
                "analyzed_at": "2026-07-23T13:00:00Z",
                "score": 92,
                "band": "Excellent",
                "summary": {"score": 92, "band": "Excellent", "vmaf_standard": 95},
            },
        }

        payload = self.worker.progress_payload(
            "idle", [first, second], records, [], [], [],
        )

        self.assertEqual("second", payload["last_result"]["video_id"])
        self.assertEqual(92, payload["last_result"]["score"])
        self.assertEqual(95, payload["last_result"]["summary"]["vmaf_standard"])

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
                "last_result": {
                    "video_id": "complete",
                    "score": 91.2,
                    "band": "Excellent",
                },
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
            self.assertEqual("complete", progress["last_result"]["video_id"])

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
                "last_result": {
                    "video_id": "complete",
                    "score": 91.2,
                    "band": "Excellent",
                },
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
            self.assertEqual("complete", progress["last_result"]["video_id"])

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
                    "summary": {
                        "score": 88.0,
                        "band": "Very good",
                        "vmaf_standard": 91.2,
                        "psnr_y": 39.4,
                        "ssim": 0.9821,
                        "phash_similarity": 97.5,
                    },
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
            cards = json.loads((data / "quality-cards.json").read_text(encoding="utf-8"))
            progress = json.loads(
                (data / "quality-analysis-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, index["analyzed_count"])
            self.assertEqual(2, index["pending_count"])
            self.assertEqual(2, progress["pending_count"])
            self.assertEqual(3, progress["catalog_count"])
            self.assertEqual(["One", "Three"], progress["upcoming"])
            self.assertEqual(88.0, index["items"]["two"]["summary"]["score"])
            self.assertEqual(91.2, index["items"]["two"]["summary"]["vmaf_standard"])
            self.assertEqual("two", index["last_result"]["video_id"])
            self.assertEqual("two", progress["last_result"]["video_id"])
            self.assertEqual(1, cards["analyzed_count"])
            self.assertEqual(2, cards["pending_count"])
            self.assertEqual(
                {
                    "score": 88.0,
                    "band": "Very good",
                    "vmaf_standard": 91.2,
                    "ssim": 0.9821,
                    "psnr_y": 39.4,
                    "phash_similarity": 97.5,
                },
                cards["items"]["two"]["summary"],
            )
            self.assertNotIn("artifacts", cards["items"]["two"])
            self.assertNotIn("settings_signature", cards)
            self.assertEqual("two", cards["last_result"]["video_id"])
            self.assertEqual(
                0o600,
                (data / "quality-index.json").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o644,
                (data / "quality-cards.json").stat().st_mode & 0o777,
            )


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
            "interval_seconds": 1,
            "max_load": 0.0,
            "threads": 2,
            "frame_rate": 30,
            "scene_threshold": 10.0,
            "min_scene_seconds": 2.0,
            "failure_retry_seconds": 30,
        }, values["quality_analysis"])

        config_without_timing = self.example()
        del config_without_timing["quality_analysis"]["interval_seconds"]
        del config_without_timing["quality_analysis"]["max_load"]
        del config_without_timing["quality_analysis"]["failure_retry_seconds"]
        defaults = self.configure.validate(
            REPO_ROOT, EXAMPLE_CONFIG, config_without_timing
        )["quality_analysis"]
        self.assertEqual(1, defaults["interval_seconds"])
        self.assertEqual(0.0, defaults["max_load"])
        self.assertEqual(30, defaults["failure_retry_seconds"])

    def test_quality_scheduler_rejects_values_below_new_minima(self):
        cases = (
            ("interval_seconds", 0, r"quality_analysis\.interval_seconds"),
            ("max_load", -0.1, r"quality_analysis\.max_load"),
            (
                "failure_retry_seconds",
                0,
                r"quality_analysis\.failure_retry_seconds",
            ),
        )
        for field, value, pattern in cases:
            with self.subTest(field=field):
                config = self.example()
                config["quality_analysis"][field] = value
                with self.assertRaisesRegex(
                    self.configure.ConfigError, pattern
                ):
                    self.configure.validate(REPO_ROOT, EXAMPLE_CONFIG, config)

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
            self.assertIn("VIDEO_QUALITY_MAX_LOAD=0.0", service)
            self.assertIn("VIDEO_QUALITY_FAILURE_RETRY_SECONDS=30", service)
            timer = (
                output / "systemd" / "hls-gallery-quality.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("OnUnitInactiveSec=1s", timer)
            self.assertIn("RandomizedDelaySec=0", timer)
            self.assertIn("AccuracySec=100ms", timer)
            self.assertNotIn("@@", service)
            self.assertNotIn("@@", timer)


if __name__ == "__main__":
    unittest.main()
