#!/usr/bin/env python3
"""Run and cache post-encode objective quality analysis for gallery videos.

The heavy metric work is performed by the standalone C++ quality analyzer. This
worker supplies gallery queueing, source-aware cache identity, resource locks,
live progress, safe pruning, retry handling, and terminal status output.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


WORKER_VERSION = "gallery-quality-v2"
CACHE_KEY = re.compile(r"^[0-9a-f]{18}--[0-9a-f]{14}$")
BUILD_DIRECTORY = re.compile(
    r"^\.building-[0-9a-f]{18}--[0-9a-f]{14}-[A-Za-z0-9_-]+$"
)
OLD_DIRECTORY = re.compile(
    r"^\.old-[0-9a-f]{18}--[0-9a-f]{14}-[0-9]+$"
)
REPORT_ARTIFACTS = ("report.json", "frames.csv", "report.html")
ABANDONED_BUILD_SECONDS = 24 * 60 * 60
SUMMARY_METRICS = (
    ("vmaf_standard", 2),
    ("vmaf_phone", 2),
    ("psnr_y", 2),
    ("psnr_normalized", 2),
    ("ssim", 6),
    ("ssim_normalized", 2),
    ("phash_similarity", 2),
    ("temporal_consistency", 2),
)
CARD_SUMMARY_FIELDS = (
    "score",
    "band",
    "vmaf_standard",
    "ssim",
    "psnr_y",
    "phash_similarity",
)


def utc_iso(timestamp=None):
    moment = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return fallback


def atomic_write_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix="." + path.name + ".", delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(path))
    finally:
        try:
            handle.close()
        except Exception:
            pass
        if temporary.exists():
            temporary.unlink()


def clamp_percent(value):
    try:
        return round(min(100.0, max(0.0, float(value))), 1)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def compact_report_summary(report):
    """Project the analyzer report into the small metric set used by indexes."""
    if not isinstance(report, dict):
        return {}
    source = report.get("summary")
    if not isinstance(source, dict):
        source = {}
    projected = {}
    score = finite_number(
        source.get("score")
        if source.get("score") is not None
        else report.get("overall_score")
    )
    if score is not None:
        projected["score"] = round(score, 2)
    band = str(
        source.get("band")
        or report.get("quality_band")
        or report.get("band")
        or ""
    ).strip()
    if band:
        projected["band"] = band
    for field, precision in SUMMARY_METRICS:
        value = finite_number(source.get(field))
        if value is not None:
            projected[field] = round(value, precision)
    return projected


def report_is_hdr_normalized(report):
    if not isinstance(report, dict):
        return False
    if report.get("hdr_normalized") is True or report.get("hdr_normalization") is True:
        return True
    capabilities = report.get("capabilities")
    return (
        isinstance(capabilities, dict)
        and capabilities.get("hdr_normalization") is True
    )


def process_cmdlines():
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    values = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            values.append(raw.replace(b"\0", b" ").decode("utf-8", "replace"))
    return values


def active_resource_reason(root):
    root_text = str(root)
    for command in process_cmdlines():
        if "content_analyzer.py" in command and root_text in command:
            return "category analysis is active"
        if "ffmpeg" in command and (root_text in command or "/cache/.building-" in command):
            return "video encoding or another media measurement is active"
    maximum_load = number(os.environ.get("VIDEO_QUALITY_MAX_LOAD"), 0.0)
    try:
        current_load = os.getloadavg()[0]
    except (AttributeError, OSError):
        current_load = 0.0
    if maximum_load > 0 and current_load > maximum_load:
        return "one-minute load {:.2f} exceeds {:.2f}".format(current_load, maximum_load)
    return ""


def safe_item_paths(root, item):
    relative = str(item.get("source_relative") or "")
    cache_key = str(item.get("cache_key") or "")
    if not relative or not CACHE_KEY.fullmatch(cache_key):
        raise RuntimeError("catalog item has an invalid source or cache identity")
    media_root = (root / "media").resolve()
    source_candidate = media_root / relative
    if source_candidate.is_symlink():
        raise RuntimeError("catalog source is a symbolic link")
    source = source_candidate.resolve()
    try:
        source.relative_to(media_root)
    except ValueError:
        raise RuntimeError("catalog source escapes the media directory")
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("catalog source is missing or unsafe")
    cache_root = (root / "cache").resolve()
    cache_candidate = cache_root / cache_key
    if cache_candidate.is_symlink():
        raise RuntimeError("encoded cache directory is a symbolic link")
    cache_dir = cache_candidate.resolve()
    try:
        cache_dir.relative_to(cache_root)
    except ValueError:
        raise RuntimeError("encoded cache path escapes the cache directory")
    variants = item.get("hls_variants")
    if isinstance(variants, list):
        variants = [value for value in variants if isinstance(value, dict)]
    else:
        variants = []
    selected = max(
        variants,
        key=lambda value: (
            integer(value.get("height")),
            integer(value.get("width")),
            integer(value.get("video_bitrate")),
        ),
        default=None,
    )
    playlist = str(selected.get("playlist") or "") if selected else ""
    hls_candidate = cache_dir / "hls"
    if hls_candidate.is_symlink():
        raise RuntimeError("encoded HLS directory is a symbolic link")
    hls_root = hls_candidate.resolve()
    try:
        hls_root.relative_to(cache_dir)
    except ValueError:
        raise RuntimeError("encoded HLS path escapes the cache directory")
    distorted = (
        hls_root / playlist
        if playlist
        else hls_root / "master.m3u8"
    ).resolve()
    try:
        distorted.relative_to(hls_root)
    except ValueError:
        raise RuntimeError("encoded cache path escapes the cache directory")
    if not distorted.is_file():
        raise RuntimeError("encoded HLS comparison playlist is missing")
    return source, distorted


def settings():
    binary_path = Path(os.environ.get(
        "VIDEO_QUALITY_BINARY", "/usr/local/libexec/hls-video-gallery/hls-quality-analyzer"
    )).expanduser()
    binary_digest = "missing"
    if binary_path.is_file():
        digest = hashlib.sha256()
        try:
            with binary_path.open("rb") as handle:
                for portion in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(portion)
            binary_digest = digest.hexdigest()
        except OSError:
            binary_digest = "unreadable"
    value = {
        "worker_version": WORKER_VERSION,
        "binary_sha256": binary_digest,
        "threads": max(1, min(2, integer(os.environ.get("VIDEO_QUALITY_THREADS"), 2))),
        "scene_threshold": max(0.1, min(100.0, number(os.environ.get("VIDEO_QUALITY_SCENE_THRESHOLD"), 10.0))),
        "min_scene_seconds": max(0.1, min(120.0, number(os.environ.get("VIDEO_QUALITY_MIN_SCENE_SECONDS"), 2.0))),
        "frame_rate": max(1, min(120, integer(os.environ.get("VIDEO_QUALITY_FRAME_RATE"), 30))),
        "require_content_analysis": truthy(os.environ.get("VIDEO_QUALITY_REQUIRE_CONTENT", "false")),
        "expected_content_analyzer_version": str(
            os.environ.get("VIDEO_QUALITY_EXPECTED_CONTENT_VERSION", "")
        ).strip(),
        "failure_retry_seconds": max(
            1, integer(os.environ.get("VIDEO_QUALITY_FAILURE_RETRY_SECONDS"), 30)
        ),
    }
    measurement_identity = {
        "worker_version": value["worker_version"],
        "binary_sha256": value["binary_sha256"],
        "frame_rate": value["frame_rate"],
        "scene_threshold": value["scene_threshold"],
        "min_scene_seconds": value["min_scene_seconds"],
    }
    canonical = json.dumps(
        measurement_identity, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    value["signature"] = hashlib.sha256(canonical).hexdigest()[:20]
    return value


def item_ready_for_content(
    item, content_records, published_version, expected_version, required
):
    if not required:
        return True
    record = content_records.get(str(item.get("id") or ""))
    return (
        isinstance(record, dict)
        and record.get("cache_key") == item.get("cache_key")
        and expected_version
        and published_version == expected_version
        and record.get("analyzer_version") == expected_version
    )


def selected_video_stream(item):
    streams = item.get("video_streams")
    if not isinstance(streams, list):
        streams = []
    streams = [value for value in streams if isinstance(value, dict)]
    selected_index = integer(item.get("primary_video_stream_index"), -1)
    if selected_index >= 0:
        selected = next(
            (
                value for value in streams
                if integer(value.get("index"), -1) == selected_index
            ),
            None,
        )
        if selected is not None:
            return selected
        return {"index": selected_index}
    candidates = [
        value for value in streams
        if not truthy(value.get("attached_pic"))
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda value: (
            not bool(value.get("default")),
            -integer(value.get("width")) * integer(value.get("height")),
        ),
    )[0]


def reference_stream_index(item):
    stream = selected_video_stream(item)
    index = integer((stream or {}).get("index"), -1)
    if index < 0:
        raise RuntimeError(
            "catalog item does not identify the source video stream used for encoding"
        )
    return index


def source_is_interlaced(item):
    stream = selected_video_stream(item)
    field_order = str((stream or {}).get("field_order") or "").strip().lower()
    return field_order not in {"", "unknown", "progressive"}


def iso_epoch(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def encoded_output_current(item, record):
    current = str(item.get("processed_at") or "").strip()
    if not current:
        return True
    saved = str(record.get("encoded_at") or "").strip()
    if saved:
        return saved == current
    analyzed_epoch = iso_epoch(record.get("analyzed_at"))
    processed_epoch = iso_epoch(current)
    return (
        analyzed_epoch is not None
        and processed_epoch is not None
        and analyzed_epoch >= processed_epoch
    )


def throttle_idle_poll(progress_path):
    previous = load_json(progress_path, {})
    if not isinstance(previous, dict) or previous.get("state") not in {
        "complete", "waiting", "error", "pruned",
    }:
        return 0.0
    interval = max(
        1.0, number(os.environ.get("VIDEO_QUALITY_IDLE_POLL_SECONDS"), 30.0)
    )
    updated = iso_epoch(previous.get("updated_at"))
    if updated is None:
        return 0.0
    delay = max(0.0, interval - max(0.0, time.time() - updated))
    if delay > 0.0:
        time.sleep(delay)
    return delay


def valid_record(item, record, configuration):
    return (
        isinstance(record, dict)
        and record.get("cache_key") == item.get("cache_key")
        and record.get("settings_signature") == configuration["signature"]
        and record.get("worker_version") == WORKER_VERSION
        and encoded_output_current(item, record)
    )


def artifact_state(path):
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if path.is_symlink() or not path.is_file() or stat_result.st_size <= 0:
        return None
    return {
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
    }


def report_artifacts_ready(root, item, record):
    expected = record.get("artifacts") if isinstance(record, dict) else None
    if not isinstance(expected, dict):
        return False
    report_root = root / "data" / "quality" / str(item.get("cache_key") or "")
    if report_root.is_symlink() or not report_root.is_dir():
        return False
    for filename in REPORT_ARTIFACTS:
        current = artifact_state(report_root / filename)
        saved = expected.get(filename)
        if current is None or not isinstance(saved, dict):
            return False
        if (
            integer(saved.get("size"), -1) != current["size"]
            or integer(saved.get("mtime_ns"), -1) != current["mtime_ns"]
        ):
            return False
    return True


def hydrate_record_summary(root, item, record):
    """Backfill compact metrics from an existing report without remeasurement."""
    if not isinstance(record, dict):
        return record
    summary = record.get("summary")
    required = {"score", "vmaf_standard", "psnr_y", "ssim", "phash_similarity"}
    if isinstance(summary, dict) and required.issubset(summary):
        return record
    report = load_json(
        root / "data" / "quality" / str(item.get("cache_key") or "") / "report.json",
        None,
    )
    if not isinstance(report, dict):
        return record
    gallery = report.get("gallery")
    if isinstance(gallery, dict):
        if gallery.get("video_id") and str(gallery["video_id"]) != str(item.get("id")):
            return record
        if gallery.get("cache_key") and gallery["cache_key"] != item.get("cache_key"):
            return record
    compact = compact_report_summary(report)
    if not compact:
        return record
    enriched = dict(record)
    enriched["summary"] = compact
    if compact.get("score") is not None:
        enriched["score"] = compact["score"]
    if compact.get("band"):
        enriched["band"] = compact["band"]
    enriched["hdr_normalized"] = report_is_hdr_normalized(report)
    return enriched


def queue_state(root, configuration, force=False):
    catalog = load_json(root / "data" / "catalog.json", None)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise RuntimeError("a valid catalog is required before quality analysis")
    items = [
        item for item in catalog["items"]
        if isinstance(item, dict) and item.get("id") and CACHE_KEY.fullmatch(str(item.get("cache_key") or ""))
    ]
    items.sort(key=lambda item: (
        integer(item.get("upload_sequence"), 2**63 - 1),
        str(item.get("source_relative") or item.get("title") or "").casefold(),
    ))
    index = load_json(root / "data" / "quality-index.json", {})
    records = index.get("items") if isinstance(index, dict) and isinstance(index.get("items"), dict) else {}
    by_id = {str(item["id"]): item for item in items}
    records = {
        item_id: record for item_id, record in records.items()
        if (
            item_id in by_id
            and valid_record(by_id[item_id], record, configuration)
            and report_artifacts_ready(root, by_id[item_id], record)
        )
    }
    records = {
        item_id: hydrate_record_summary(root, by_id[item_id], record)
        for item_id, record in records.items()
    }
    content_index = load_json(root / "data" / "content-index.json", {})
    content_analyzer_version = (
        str(content_index.get("analyzer_version") or "")
        if isinstance(content_index, dict)
        else ""
    )
    content_records = (
        content_index.get("items")
        if isinstance(content_index, dict) and isinstance(content_index.get("items"), dict)
        else {}
    )
    failures_payload = load_json(root / "data" / "quality-failures.json", {})
    failures = failures_payload.get("items") if isinstance(failures_payload, dict) else {}
    if not isinstance(failures, dict):
        failures = {}
    live_keys = {str(item["cache_key"]) for item in items}
    failures = {
        key: value for key, value in failures.items()
        if key in live_keys and isinstance(value, dict)
        and value.get("settings_signature") == configuration["signature"]
    }
    now = time.time()
    pending = []
    retry_pending = []
    waiting_content = []
    cooling_down = []
    for item in items:
        item_id = str(item["id"])
        if not force and valid_record(item, records.get(item_id), configuration):
            continue
        if not item_ready_for_content(
            item,
            content_records,
            content_analyzer_version,
            configuration.get("expected_content_analyzer_version", ""),
            configuration["require_content_analysis"],
        ):
            waiting_content.append(item)
            continue
        failure = failures.get(str(item["cache_key"]))
        if not force and isinstance(failure, dict):
            if number(failure.get("retry_after_epoch")) > now:
                cooling_down.append(item)
            else:
                # Give never-attempted work priority over retries. With a short
                # retry cooldown and one item per service run, an early,
                # permanently broken upload must not starve the rest of the
                # collection.
                retry_pending.append(item)
            continue
        pending.append(item)
    pending.extend(retry_pending)
    return catalog, items, records, failures, pending, waiting_content, cooling_down


def average_analysis_ratio(records):
    samples = []
    for record in records.values():
        elapsed = number(record.get("analysis_seconds"))
        duration = number(record.get("duration_seconds"))
        if elapsed > 0.1 and duration > 0.1:
            samples.append(elapsed / duration)
    if not samples:
        return 2.0
    samples = sorted(samples[-40:])
    if len(samples) >= 6:
        trim = max(1, len(samples) // 10)
        samples = samples[trim:-trim]
    return max(0.01, sum(samples) / len(samples))


def forecast(records, pending):
    ratio = average_analysis_ratio(records)
    elapsed_samples = [
        number(record.get("analysis_seconds"))
        for record in records.values()
        if number(record.get("analysis_seconds")) > 0
    ]
    seconds = sum(max(1.0, number(item.get("duration_seconds"), 1.0)) * ratio for item in pending)
    return {
        "average_realtime_factor": round(ratio, 3),
        "average_seconds_per_video": round(
            sum(elapsed_samples[-40:]) / len(elapsed_samples[-40:]), 1
        ) if elapsed_samples else 0,
        "eta_seconds": round(seconds),
        "estimated_finish_at": utc_iso(time.time() + seconds) if pending else utc_iso(),
    }


def display_name(item):
    return str(item.get("title") or item.get("source_relative") or item.get("id") or "Untitled video")


def latest_result(items, records):
    if not records:
        return None
    by_id = {str(item.get("id")): item for item in items if item.get("id")}
    candidates = []
    for item_id, record in records.items():
        item = by_id.get(str(item_id))
        if not item or not isinstance(record, dict):
            continue
        if record.get("cache_key") != item.get("cache_key"):
            continue
        candidates.append((str(record.get("analyzed_at") or ""), str(item_id), item, record))
    if not candidates:
        return None
    _timestamp, item_id, item, record = max(candidates)
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    result = {
        "video_id": item_id,
        "cache_key": record.get("cache_key"),
        "title": display_name(item),
        "source_relative": item.get("source_relative") or "",
        "analyzed_at": record.get("analyzed_at"),
        "analysis_seconds": record.get("analysis_seconds"),
        "duration_seconds": record.get("duration_seconds"),
        "report_url": record.get("report_url"),
        "score": summary.get("score", record.get("score")),
        "band": summary.get("band", record.get("band")),
        "summary": summary,
        "hdr_normalized": record.get("hdr_normalized") is True,
    }
    return {key: value for key, value in result.items() if value is not None}


def quality_card_summary(record):
    """Return only the score fields needed by gallery listing cards."""
    if not isinstance(record, dict):
        return {}
    source = record.get("summary")
    if not isinstance(source, dict):
        source = {}
    merged = dict(source)
    if merged.get("score") is None and record.get("score") is not None:
        merged["score"] = record["score"]
    if not merged.get("band") and record.get("band"):
        merged["band"] = record["band"]
    compact = compact_report_summary({"summary": merged})
    return {
        field: compact[field]
        for field in CARD_SUMMARY_FIELDS
        if compact.get(field) is not None
    }


def quality_cards_payload(items, records, pending_count, updated_at=None):
    """Build the compact authenticated projection polled by listing pages."""
    cards = {}
    for item_id, record in records.items():
        if not isinstance(record, dict):
            continue
        cards[str(item_id)] = {
            "cache_key": record.get("cache_key"),
            "analyzed_at": record.get("analyzed_at"),
            "summary": quality_card_summary(record),
        }
        cards[str(item_id)] = {
            key: value
            for key, value in cards[str(item_id)].items()
            if value is not None
        }
    payload = {
        "schema_version": 1,
        "worker_version": WORKER_VERSION,
        "updated_at": updated_at or utc_iso(),
        "catalog_count": len(items),
        "analyzed_count": len(records),
        "pending_count": pending_count,
        "items": cards,
    }
    last = latest_result(items, records)
    if isinstance(last, dict):
        last_card = {
            key: last.get(key)
            for key in ("video_id", "cache_key", "title", "analyzed_at")
            if last.get(key) is not None
        }
        last_card["summary"] = quality_card_summary(last)
        payload["last_result"] = last_card
    return payload


def progress_payload(state, items, records, pending, waiting_content, cooling_down, **extra):
    catalog_total = len(items)
    payload = {
        "schema_version": 1,
        "worker_version": WORKER_VERSION,
        "state": state,
        "phase": extra.pop("phase", state),
        "phase_label": extra.pop("phase_label", state.replace("_", " ").title()),
        "updated_at": utc_iso(),
        "catalog_count": len(items),
        "analyzed_count": len(records),
        "pending_count": len(pending) + len(cooling_down),
        "waiting_content_count": len(waiting_content),
        "cooling_down_count": len(cooling_down),
        "percent": clamp_percent(100.0 * len(records) / catalog_total) if catalog_total else 100.0,
        "upcoming": [display_name(item) for item in pending + cooling_down],
    }
    last_result = extra.pop("last_result", latest_result(items, records))
    if isinstance(last_result, dict):
        payload["last_result"] = last_result
    payload.update(forecast(records, pending + cooling_down))
    payload.update(extra)
    return payload


def publish_index(path, items, records, configuration, pending_count):
    updated_at = utc_iso()
    payload = {
        "schema_version": 1,
        "worker_version": WORKER_VERSION,
        "settings_signature": configuration["signature"],
        "updated_at": updated_at,
        "catalog_count": len(items),
        "analyzed_count": len(records),
        "pending_count": pending_count,
        "items": records,
    }
    last_result = latest_result(items, records)
    if isinstance(last_result, dict):
        payload["last_result"] = last_result
    # The full index is worker state and can include artifact metadata. Keep it
    # private; the browser polls the deliberately small projection beside it.
    atomic_write_json(path, payload, mode=0o600)
    atomic_write_json(
        path.with_name("quality-cards.json"),
        quality_cards_payload(items, records, pending_count, updated_at),
        mode=0o644,
    )


def prune_reports(quality_root, keep_keys, now=None):
    removed = 0
    if quality_root.is_symlink() or quality_root.parent.is_symlink():
        raise RuntimeError("quality report root cannot be a symbolic link")
    if not quality_root.is_dir():
        return removed
    cutoff = number(now, time.time()) - ABANDONED_BUILD_SECONDS
    for path in quality_root.iterdir():
        if (
            not path.is_symlink()
            and path.is_dir()
            and (
                BUILD_DIRECTORY.fullmatch(path.name)
                or OLD_DIRECTORY.fullmatch(path.name)
            )
        ):
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified < cutoff:
                shutil.rmtree(str(path))
                removed += 1
            continue
        if (
            path.is_symlink()
            or not path.is_dir()
            or not CACHE_KEY.fullmatch(path.name)
            or path.name in keep_keys
        ):
            continue
        shutil.rmtree(str(path))
        removed += 1
    return removed


def acquire_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def report_record(root, report, item, configuration, elapsed):
    summary = compact_report_summary(report)
    report_root = root / "data" / "quality" / str(item["cache_key"])
    artifacts = {}
    for filename in REPORT_ARTIFACTS:
        current = artifact_state(report_root / filename)
        if current is None:
            raise RuntimeError("installed quality report artifact is missing: {}".format(filename))
        artifacts[filename] = current
    return {
        "cache_key": item["cache_key"],
        "worker_version": WORKER_VERSION,
        "analyzer_version": str(report.get("analyzer_version") or report.get("version") or ""),
        "settings_signature": configuration["signature"],
        "analyzed_at": utc_iso(),
        "analysis_seconds": round(elapsed, 1),
        "duration_seconds": round(number(item.get("duration_seconds")), 3),
        "encoded_at": item.get("processed_at"),
        "score": summary.get("score"),
        "band": str(summary.get("band") or ""),
        "summary": summary,
        "hdr_normalized": report_is_hdr_normalized(report),
        "report_url": "data/quality/{}/report.json".format(item["cache_key"]),
        "artifacts": artifacts,
    }


def enrich_report(report_path, item, configuration):
    report = load_json(report_path, None)
    if not isinstance(report, dict):
        raise RuntimeError("quality analyzer did not produce a valid report.json")
    report["gallery"] = {
        "video_id": item["id"],
        "cache_key": item["cache_key"],
        "title": display_name(item),
        "source_relative": item.get("source_relative") or "",
        "worker_version": WORKER_VERSION,
        "settings_signature": configuration["signature"],
    }
    report["artifacts"] = {
        "json": "report.json",
        "frames_csv": "frames.csv",
        "html": "report.html",
    }
    atomic_write_json(report_path, report)
    return report


def install_report_tree(build_dir, final_dir):
    if not (build_dir / "report.json").is_file():
        raise RuntimeError("quality analyzer completed without report.json")
    for path in [build_dir] + list(build_dir.rglob("*")):
        if path.is_dir():
            os.chmod(str(path), 0o755)
        elif path.is_file():
            os.chmod(str(path), 0o644)
    old_dir = None
    if final_dir.exists():
        old_dir = final_dir.with_name(".old-{}-{}".format(final_dir.name, os.getpid()))
        if old_dir.exists():
            shutil.rmtree(str(old_dir))
        os.replace(str(final_dir), str(old_dir))
    try:
        os.replace(str(build_dir), str(final_dir))
    except Exception:
        if old_dir and old_dir.exists() and not final_dir.exists():
            os.replace(str(old_dir), str(final_dir))
        raise
    if old_dir and old_dir.exists():
        shutil.rmtree(str(old_dir))


def run_one(root, item, configuration, progress_path, items, records, pending, waiting_content, cooling_down):
    binary = Path(os.environ.get(
        "VIDEO_QUALITY_BINARY", "/usr/local/libexec/hls-video-gallery/hls-quality-analyzer"
    )).expanduser()
    if not binary.is_file() or not os.access(str(binary), os.X_OK):
        raise RuntimeError("quality analyzer binary is missing or not executable: {}".format(binary))
    source, distorted = safe_item_paths(root, item)
    quality_root = root / "data" / "quality"
    quality_root.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(
        prefix=".building-{}-".format(item["cache_key"]), dir=str(quality_root)
    ))
    engine_progress = root / "data" / ".quality-engine-progress-{}.json".format(os.getpid())
    command = [
        str(binary),
        "--reference", str(source),
        "--reference-stream-index", str(reference_stream_index(item)),
        "--distorted", str(distorted),
        "--output-dir", str(build_dir),
        "--threads", str(configuration["threads"]),
        "--frame-rate", str(configuration["frame_rate"]),
        "--scene-threshold", str(configuration["scene_threshold"]),
        "--min-scene-seconds", str(configuration["min_scene_seconds"]),
        "--progress-json", str(engine_progress),
    ]
    if source_is_interlaced(item):
        command.append("--deinterlace-reference")
    started = time.time()
    current = {
        "video_id": item["id"],
        "cache_key": item["cache_key"],
        "title": display_name(item),
        "source_relative": item.get("source_relative") or "",
    }
    sanitized_command = shlex.join(command)
    process = None
    try:
        process = subprocess.Popen(command)
        while process.poll() is None:
            engine = load_json(engine_progress, {})
            if not isinstance(engine, dict):
                engine = {}
            if not engine.get("command") and not engine.get("ffmpeg_command"):
                engine["command"] = sanitized_command
            payload = progress_payload(
                "analyzing", items, records, pending, waiting_content, cooling_down,
                phase=str(engine.get("phase") or "measuring"),
                phase_label=str(engine.get("phase_label") or "Measuring encoded quality"),
                current=current,
                queue_position=1,
                queue_total=len(pending) + len(cooling_down),
                upcoming=[display_name(value) for value in pending[1:] + cooling_down],
                run_started_at=utc_iso(started),
                item_started_at=utc_iso(started),
                elapsed_seconds=round(time.time() - started, 1),
                engine=engine,
            )
            atomic_write_json(progress_path, payload)
            time.sleep(1)
        return_code = process.wait()
        if return_code:
            failed_progress = load_json(engine_progress, {})
            detail = (
                str(failed_progress.get("error") or "").strip()
                if isinstance(failed_progress, dict) else ""
            )
            raise RuntimeError(
                "quality analyzer exited with status {}{}".format(
                    return_code, ": " + detail if detail else "",
                )
            )
        report = enrich_report(build_dir / "report.json", item, configuration)
        install_report_tree(build_dir, quality_root / item["cache_key"])
        return report, time.time() - started
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        if build_dir.exists():
            shutil.rmtree(str(build_dir), ignore_errors=True)
        try:
            engine_progress.unlink()
        except FileNotFoundError:
            pass


def save_failures(path, failures):
    atomic_write_json(path, {
        "schema_version": 1,
        "updated_at": utc_iso(),
        "items": failures,
    }, mode=0o600)


def status_text(payload, include_all=False, include_command=False):
    state = str(payload.get("state") or "unknown").upper()
    phase = str(payload.get("phase_label") or payload.get("phase") or "")
    analyzed = integer(payload.get("analyzed_count"))
    pending = integer(payload.get("pending_count"))
    waiting = integer(payload.get("waiting_content_count"))
    percent = number(payload.get("percent"))
    parts = [
        "{} {:0.1f}% — {}".format(state, percent, phase),
        "{} measured, {} queued, {} awaiting categories".format(analyzed, pending, waiting),
    ]
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    engine = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
    if current:
        parts.append("Current: {}".format(current.get("title") or current.get("source_relative") or "video"))
    if engine:
        parts.append(
            "Metric pass: {:0.1f}%  {:0.1f} fps  {:0.2f}x  ETA {}s".format(
                number(engine.get("percent")), number(engine.get("fps")),
                number(engine.get("speed")), integer(engine.get("eta_seconds")),
            )
        )
        if include_command and engine.get("command"):
            parts.append("Command: {}".format(engine["command"]))
    last_result = payload.get("last_result") if isinstance(payload.get("last_result"), dict) else {}
    if last_result and not current:
        score = finite_number(last_result.get("score"))
        score_text = " — {:.1f}".format(score) if score is not None else ""
        band_text = " ({})".format(last_result.get("band")) if last_result.get("band") else ""
        parts.append(
            "Last: {}{}{}".format(
                last_result.get("title") or last_result.get("source_relative") or "video",
                score_text,
                band_text,
            )
        )
    upcoming = payload.get("upcoming") if isinstance(payload.get("upcoming"), list) else []
    if upcoming:
        limit = len(upcoming) if include_all else min(5, len(upcoming))
        parts.append("Up next: {}".format(", ".join(str(value) for value in upcoming[:limit])))
    return "\n".join(parts)


def show_status(progress_path, as_json=False, watch=False, include_all=False, include_command=False):
    previous = ""
    try:
        while True:
            payload = load_json(progress_path, {
                "state": "idle", "phase_label": "No quality status has been published yet",
            })
            rendered = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                if as_json else status_text(payload, include_all, include_command)
            )
            if rendered != previous:
                if watch and previous:
                    print()
                print(rendered, flush=True)
                previous = rendered
            if not watch:
                return 0
            time.sleep(2)
    except KeyboardInterrupt:
        return 130


def catalog_wait_payload(progress_path):
    """Preserve the last safe queue snapshot while the scanner owns its catalog."""
    previous = load_json(progress_path, {})
    if not isinstance(previous, dict):
        previous = {}
    upcoming = previous.get("upcoming")
    if not isinstance(upcoming, list):
        upcoming = []
    payload = {
        "schema_version": 1,
        "worker_version": WORKER_VERSION,
        "state": "waiting",
        "phase": "waiting_for_catalog",
        "phase_label": "Waiting for the catalog scan to finish",
        "updated_at": utc_iso(),
        "catalog_count": max(0, integer(previous.get("catalog_count"))),
        "analyzed_count": max(0, integer(previous.get("analyzed_count"))),
        "pending_count": max(0, integer(previous.get("pending_count"))),
        "waiting_content_count": max(
            0, integer(previous.get("waiting_content_count"))
        ),
        "cooling_down_count": max(0, integer(previous.get("cooling_down_count"))),
        "percent": clamp_percent(previous.get("percent")),
        "upcoming": upcoming,
        "reason": "the scanner currently owns the catalog",
    }
    for key in (
        "average_realtime_factor",
        "average_seconds_per_video",
        "estimated_finish_at",
        "eta_seconds",
    ):
        if key in previous:
            payload[key] = previous[key]
    if isinstance(previous.get("last_result"), dict):
        payload["last_result"] = previous["last_result"]
    return payload


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)))
    parser.add_argument("--items", type=int, default=1, help="maximum videos to measure this run")
    parser.add_argument("--video-id", help="measure one exact catalog video ID (manual administration)")
    parser.add_argument("--force", action="store_true", help="remeasure matching cached reports")
    parser.add_argument("--ignore-busy", action="store_true", help="ignore load/process checks (manual use only)")
    parser.add_argument("--prune-only", action="store_true", help="remove stale report records and output")
    parser.add_argument("--status", action="store_true", help="show the last published quality status")
    parser.add_argument("--watch", action="store_true", help="keep showing quality status")
    parser.add_argument("--json", action="store_true", help="emit status JSON")
    parser.add_argument("--all", action="store_true", help="show the complete upcoming queue")
    parser.add_argument("--command", action="store_true", help="show the active analyzer command")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    if "quality-status" in Path(sys.argv[0]).name:
        arguments.status = True
    root = Path(arguments.root).expanduser().resolve()
    data_root = root / "data"
    progress_path = data_root / "quality-analysis-progress.json"
    if arguments.status or arguments.watch or arguments.json:
        return show_status(
            progress_path, as_json=arguments.json, watch=arguments.watch,
            include_all=arguments.all, include_command=arguments.command,
        )
    if data_root.is_symlink():
        raise RuntimeError("gallery data root cannot be a symbolic link")
    data_root.mkdir(parents=True, exist_ok=True)
    # Active queues leave progress in the "idle" state and therefore continue
    # at the systemd timer's one-second cadence. Terminal/resource-wait states
    # back off to a 30-second poll without holding any gallery lock, avoiding
    # permanent one-process-per-second churn once the queue is empty.
    throttle_idle_poll(progress_path)
    quality_lock = acquire_lock(data_root / "quality-analysis.lock")
    if quality_lock is None:
        print("Another quality-analysis pass is already running; exiting cleanly")
        return 0
    configuration = settings()
    index_path = data_root / "quality-index.json"
    failures_path = data_root / "quality-failures.json"
    scan_lock = None
    post_lock = None
    try:
        # The scanner publishes partial in-progress catalogs while holding this
        # same lock. Take it before deriving or mutating any queue/cache state so
        # a partial snapshot can never make completed reports look orphaned.
        scan_lock = acquire_lock(data_root / "scan.lock")
        if scan_lock is None:
            atomic_write_json(
                progress_path, catalog_wait_payload(progress_path)
            )
            print("DEFER: the catalog scanner is active")
            return 0

        catalog_snapshot = load_json(data_root / "catalog.json", {})
        scan_snapshot = catalog_snapshot.get("scan") if isinstance(catalog_snapshot, dict) else {}
        if isinstance(scan_snapshot, dict) and scan_snapshot.get("in_progress") is True:
            wait_payload = catalog_wait_payload(progress_path)
            wait_payload["reason"] = "the published catalog is an in-progress snapshot"
            atomic_write_json(progress_path, wait_payload)
            print("DEFER: the catalog scan is still in progress")
            return 0
        catalog, items, records, failures, pending, waiting_content, cooling_down = queue_state(
            root, configuration, force=arguments.force and not arguments.video_id,
        )
        execution_pending = pending
        if arguments.video_id:
            requested = str(arguments.video_id)
            catalog_match = next((item for item in items if str(item.get("id")) == requested), None)
            if catalog_match is None:
                raise RuntimeError("requested video ID is not present in the current catalog")
            waiting_match = any(
                str(item.get("id")) == requested for item in waiting_content
            )
            pending_match = next(
                (item for item in pending if str(item.get("id")) == requested),
                None,
            )
            if arguments.force and not waiting_match:
                pending_match = catalog_match
                cooling_down = [
                    item for item in cooling_down
                    if str(item.get("id")) != requested
                ]
                failures.pop(str(catalog_match["cache_key"]), None)
            if pending_match is not None:
                pending = [pending_match] + [
                    item for item in pending
                    if str(item.get("id")) != requested
                ]
                execution_pending = [pending_match]
            else:
                execution_pending = []

        current_report_keys = {
            str(record.get("cache_key"))
            for record in records.values()
            if isinstance(record, dict) and CACHE_KEY.fullmatch(str(record.get("cache_key") or ""))
        }
        removed = prune_reports(data_root / "quality", current_report_keys)
        publish_index(
            index_path, items, records, configuration,
            len(pending) + len(waiting_content) + len(cooling_down),
        )
        save_failures(failures_path, failures)
        if arguments.prune_only:
            atomic_write_json(progress_path, progress_payload(
                "pruned", items, records, pending, waiting_content, cooling_down,
                phase_label="Quality report cache cleaned", removed_reports=removed,
            ))
            print("Quality reports: {} measured, {} removed".format(len(records), removed))
            return 0
        if not execution_pending:
            queue_empty = not pending and not waiting_content and not cooling_down
            state = "complete" if queue_empty else "waiting"
            label = "Quality analysis complete"
            if waiting_content:
                label = "Waiting for category analysis"
            elif cooling_down:
                label = "Waiting to retry failed measurements"
            elif arguments.video_id:
                label = "Requested quality report is already current"
            elif pending:
                label = "Quality work remains queued"
            atomic_write_json(progress_path, progress_payload(
                state, items, records, pending, waiting_content, cooling_down,
                phase="complete" if state == "complete" else "waiting",
                phase_label=label,
            ))
            print("Quality index: {} measured, {} pending".format(
                len(records), len(pending) + len(waiting_content) + len(cooling_down)
            ))
            return 0
        if not arguments.ignore_busy:
            reason = active_resource_reason(root)
            if reason:
                atomic_write_json(progress_path, progress_payload(
                    "waiting", items, records, pending, waiting_content, cooling_down,
                    phase="waiting_for_resources", phase_label="Quality analysis is waiting",
                    reason=reason,
                ))
                print("DEFER: {} ({} videos queued)".format(reason, len(pending)))
                return 0

        # Holding both locks keeps an encoder or visual analyzer from beginning
        # during a measurement. The main scanner waits rather than racing us.
        post_lock = acquire_lock(data_root / "post-process.lock")
        if post_lock is None:
            atomic_write_json(progress_path, progress_payload(
                "waiting", items, records, pending, waiting_content, cooling_down,
                phase="waiting_for_categories", phase_label="Waiting for category analysis",
            ))
            return 0

        processed = 0
        processed_ids = set()
        for item in execution_pending[:max(1, arguments.items)]:
            current_pending = [
                value for value in pending
                if str(value.get("id")) not in processed_ids
            ]
            try:
                report, elapsed = run_one(
                    root, item, configuration, progress_path, items, records,
                    current_pending, waiting_content, cooling_down,
                )
            except Exception as error:
                cache_key = str(item["cache_key"])
                previous = failures.get(cache_key) if isinstance(failures.get(cache_key), dict) else {}
                attempts = integer(previous.get("attempts")) + 1
                retry_after = time.time() + configuration["failure_retry_seconds"]
                failures[cache_key] = {
                    "failed_at": utc_iso(),
                    "failed_at_epoch": round(time.time()),
                    "retry_after": utc_iso(retry_after),
                    "retry_after_epoch": round(retry_after),
                    "attempts": attempts,
                    "settings_signature": configuration["signature"],
                    "error": str(error)[-2000:],
                }
                save_failures(failures_path, failures)
                atomic_write_json(progress_path, progress_payload(
                    "error", items, records, current_pending, waiting_content, cooling_down,
                    phase="error", phase_label="Quality measurement stopped",
                    current={
                        "video_id": item["id"], "cache_key": cache_key,
                        "title": display_name(item),
                        "source_relative": item.get("source_relative") or "",
                    },
                    error=str(error)[-2000:],
                ))
                raise
            records[str(item["id"])] = report_record(
                root, report, item, configuration, elapsed
            )
            failures.pop(str(item["cache_key"]), None)
            processed_ids.add(str(item["id"]))
            processed += 1
            remaining = [
                value for value in pending
                if str(value.get("id")) not in processed_ids
            ]
            publish_index(
                index_path, items, records, configuration,
                len(remaining) + len(waiting_content) + len(cooling_down),
            )
            save_failures(failures_path, failures)
            summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            print("MEASURED {} — {} ({:.1f}) in {:.1f}s".format(
                item.get("source_relative") or display_name(item),
                summary.get("band") or "complete",
                number(summary.get("score") or report.get("overall_score")),
                elapsed,
            ))

        remaining = [
            value for value in pending
            if str(value.get("id")) not in processed_ids
        ]
        atomic_write_json(progress_path, progress_payload(
            "complete" if not remaining and not waiting_content and not cooling_down else "idle",
            items, records, remaining, waiting_content, cooling_down,
            phase="complete" if not remaining and not waiting_content and not cooling_down else "waiting_for_next_batch",
            phase_label=(
                "Quality analysis complete"
                if not remaining and not waiting_content and not cooling_down
                else "Waiting for the next quality batch"
            ),
            last_processed=processed,
        ))
        return 0
    finally:
        if post_lock is not None:
            post_lock.close()
        if scan_lock is not None:
            scan_lock.close()
        quality_lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
