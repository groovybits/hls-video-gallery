#!/usr/bin/env python3
"""Publish low-overhead FFmpeg telemetry for the video library UI and CLI."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".mxf", ".ogv", ".ts", ".webm", ".wmv",
}
CURRENT_CACHE_VERSION = 6


def utc_iso(timestamp=None):
    if timestamp is None:
        timestamp = time.time()
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def fraction(value):
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator = float(denominator)
        return float(numerator) / denominator if denominator else 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return number(value)


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    os.close(descriptor)
    try:
        shutil.copyfile(str(source), temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_cmdline(pid):
    try:
        payload = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in payload.split(b"\0") if part]


def process_times(pid):
    """Return elapsed seconds and lifetime CPU percentage from Linux procfs."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        user_ticks = number(fields[11])
        system_ticks = number(fields[12])
        start_ticks = number(fields[19])
        ticks_per_second = number(os.sysconf("SC_CLK_TCK"), 100.0)
        uptime = number(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        elapsed = max(0.001, uptime - (start_ticks / ticks_per_second))
        cpu_percent = ((user_ticks + system_ticks) / ticks_per_second) / elapsed * 100.0
        return elapsed, cpu_percent
    except (OSError, IndexError, ValueError):
        return 0.0, 0.0


def process_identity(pid):
    """Return a PID-reuse-safe identity and wall-clock start time."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        start_ticks = int(fields[19])
        ticks_per_second = number(os.sysconf("SC_CLK_TCK"), 100.0)
        uptime = number(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        elapsed = max(0.0, uptime - (start_ticks / ticks_per_second))
        return "{}:{}".format(pid, start_ticks), max(0.0, time.time() - elapsed)
    except (OSError, IndexError, TypeError, ValueError):
        return str(pid), 0.0


def option_value(arguments, name, default=""):
    for index in range(len(arguments) - 2, -1, -1):
        if arguments[index] == name:
            return arguments[index + 1]
    return default


def output_argument(arguments, suffix):
    for argument in arguments:
        if argument.endswith(suffix) and ".building-" in argument:
            return argument
    return ""


def discover_videos(media_root):
    videos = []
    if not media_root.is_dir():
        return videos
    for path in media_root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if any(part.startswith(".") for part in path.relative_to(media_root).parts):
            continue
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(path)
    return sorted(videos, key=lambda value: str(value).casefold())


def find_ffmpeg(root):
    media_prefix = str(root / "media") + os.sep
    cache_prefix = str(root / "cache") + os.sep
    for proc_dir in Path("/proc").glob("[0-9]*"):
        arguments = read_cmdline(proc_dir.name)
        if not arguments or os.path.basename(arguments[0]) != "ffmpeg":
            continue
        joined = "\0".join(arguments)
        if media_prefix in joined and cache_prefix in joined and ".building-" in joined:
            return int(proc_dir.name), arguments
    return 0, []


def find_scanner(root):
    scanner = str(root / "_tools" / "scan.py")
    for proc_dir in Path("/proc").glob("[0-9]*"):
        arguments = read_cmdline(proc_dir.name)
        if scanner in arguments:
            return int(proc_dir.name)
    return 0


def source_from_arguments(arguments, media_root):
    source = option_value(arguments, "-i")
    try:
        path = Path(source).resolve()
        path.relative_to(media_root)
        return path
    except (OSError, ValueError):
        return None


def parse_playlist_position(playlist, segment_seconds):
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    durations = [number(value) for value in re.findall(r"^#EXTINF:([0-9.]+)", text, flags=re.MULTILINE)]
    if durations:
        return sum(durations), len(durations)
    segments = list(playlist.parent.glob("seg-*.ts")) if playlist.parent.is_dir() else []
    return len(segments) * segment_seconds, len(segments)


def thumbnail_position(pattern, interval):
    directory = Path(pattern).parent
    count = len(list(directory.glob("thumb-*.jpg"))) if directory.is_dir() else 0
    return count * interval, count


def sanitize_command(arguments, root):
    root_text = str(root)
    sanitized = []
    for argument in arguments:
        if argument.startswith(root_text):
            argument = "$VIDEO_ROOT" + argument[len(root_text):]
        sanitized.append(argument)
    return shlex.join(sanitized)


def display_rate(value):
    text = str(value or "")
    if text.isdigit():
        amount = int(text)
        if amount >= 1_000_000:
            return "{:.1f} Mb/s".format(amount / 1_000_000.0).replace(".0 ", " ")
        if amount >= 1_000:
            return "{:.0f} kb/s".format(amount / 1_000.0)
    if text.endswith("k") and text[:-1].isdigit():
        return text[:-1] + " kb/s"
    return text or "—"


def load_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return fallback


def packet_timeline_duration(ffprobe, media_path):
    """Return the first video stream's final packet timestamp, memory-bounded."""
    command = [
        ffprobe, "-v", "warning", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,duration_time",
        "-of", "compact=p=0:nk=0", str(media_path),
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError:
        return 0.0
    maximum_end = 0.0
    for raw_line in process.stdout or []:
        fields = {}
        for portion in raw_line.strip().split("|"):
            key, separator, value = portion.partition("=")
            if separator:
                fields[key] = value
        timestamp = max(number(fields.get("pts_time"), -1.0), number(fields.get("dts_time"), -1.0))
        if timestamp < 0:
            continue
        maximum_end = max(maximum_end, timestamp + max(0.0, number(fields.get("duration_time"))))
    process.wait()
    return maximum_end


class EncodingMonitor:
    def __init__(self, root, ffprobe):
        self.root = root
        self.media_root = root / "media"
        self.output_path = root / "data" / "encode-progress.json"
        self.ffprobe = ffprobe
        self.probe_cache = {}
        self.last_active = None
        self.last_seen_at = 0.0
        self.preview_key = None
        previous_payload = load_json(self.output_path, {})
        previous_phase = previous_payload.get("phase") if isinstance(previous_payload, dict) else ""
        self.last_hls_speed = number(previous_payload.get("speed")) if previous_phase in {"hls_encode", "combined_encode"} else 0.0
        self.duration_index_path = root / "data" / "queue-durations.json"
        duration_payload = load_json(self.duration_index_path, {})
        self.duration_index = duration_payload.get("items", {}) if isinstance(duration_payload, dict) else {}
        if not isinstance(self.duration_index, dict):
            self.duration_index = {}
        self.duration_lock = threading.Lock()
        self.duration_thread = None
        self.last_prune_check = -1_000_000_000.0
        self.ready_sources_cache = set()
        self.ready_sources_checked_at = 0.0
        self.queue_run_path = root / "data" / "encode-queue.json"
        self.queue_run = load_json(self.queue_run_path, {})
        if not isinstance(self.queue_run, dict):
            self.queue_run = {}
        self.seed_durations_from_catalog()

    def prune_missing_catalog_items(self):
        now = time.monotonic()
        if now - self.last_prune_check < 10:
            return []
        self.last_prune_check = now
        catalog_path = self.root / "data" / "catalog.json"
        lock_path = self.root / "data" / "catalog-prune.lock"
        lock_handle = lock_path.open("a+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            return []
        try:
            catalog = load_json(catalog_path, {})
            items = catalog.get("items", []) if isinstance(catalog, dict) else []
            if not isinstance(items, list):
                return []
            kept = []
            removed = []
            for item in items:
                relative = item.get("source_relative") if isinstance(item, dict) else ""
                source = self.media_root / relative if relative else None
                if source and source.is_file():
                    kept.append(item)
                else:
                    removed.append(item)
            if not removed:
                return []
            catalog["items"] = kept
            catalog["count"] = len(kept)
            catalog["generated_at"] = utc_iso()
            catalog["source_removal"] = {
                "removed_at": utc_iso(),
                "removed_count": len(removed),
            }
            if isinstance(catalog.get("scan"), dict):
                catalog["scan"]["source_count"] = len(discover_videos(self.media_root))
            atomic_write_json(catalog_path, catalog)
            removed_names = []
            for item in removed:
                relative = item.get("source_relative") or item.get("title") or "unknown"
                removed_names.append(relative)
                cache_key = str(item.get("cache_key") or "")
                if re.fullmatch(r"[0-9a-f]{18}--[0-9a-f]{14}", cache_key):
                    cache_dir = self.root / "cache" / cache_key
                    if cache_dir.is_dir():
                        shutil.rmtree(str(cache_dir), ignore_errors=True)
            print("[{}] PRUNE removed {} missing source{} from catalog: {}".format(
                dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                len(removed_names), "" if len(removed_names) == 1 else "s", ", ".join(removed_names),
            ), flush=True)
            return removed_names
        finally:
            lock_handle.close()

    def file_signature(self, path):
        try:
            stat_result = path.stat()
            return [stat_result.st_size, stat_result.st_mtime_ns]
        except OSError:
            return [0, 0]

    def remember_duration(self, path, duration):
        duration = number(duration)
        if duration <= 0:
            return False
        try:
            relative = path.relative_to(self.media_root).as_posix()
        except ValueError:
            return False
        entry = {"signature": self.file_signature(path), "duration_seconds": round(duration, 6)}
        with self.duration_lock:
            if self.duration_index.get(relative) == entry:
                return False
            self.duration_index[relative] = entry
        return True

    def indexed_duration(self, path, entries=None):
        try:
            relative = path.relative_to(self.media_root).as_posix()
        except ValueError:
            return 0.0
        if entries is None:
            with self.duration_lock:
                entry = dict(self.duration_index.get(relative) or {})
        else:
            entry = entries.get(relative) or {}
        if entry.get("signature") != self.file_signature(path):
            return 0.0
        return number(entry.get("duration_seconds"))

    def write_duration_index(self):
        with self.duration_lock:
            items = dict(self.duration_index)
        atomic_write_json(self.duration_index_path, {
            "schema_version": 1,
            "updated_at": utc_iso(),
            "items": items,
        })

    def seed_durations_from_catalog(self):
        catalog = load_json(self.root / "data" / "catalog.json", {})
        changed = False
        for item in catalog.get("items", []) if isinstance(catalog, dict) else []:
            relative = item.get("source_relative")
            if relative:
                changed = self.remember_duration(self.media_root / relative, item.get("duration_seconds")) or changed
        if changed:
            self.write_duration_index()

    def ready_source_relatives(self):
        """Count completed cache trees by validated source signature, not sort position."""
        now = time.monotonic()
        if now - self.ready_sources_checked_at < 5:
            return set(self.ready_sources_cache)
        ready = set()
        cache_root = self.root / "cache"
        for cache_dir in (cache_root.iterdir() if cache_root.is_dir() else []):
            if not cache_dir.is_dir() or cache_dir.name.startswith(".") or "--" not in cache_dir.name:
                continue
            state = load_json(cache_dir / "state.json", {})
            metadata = load_json(cache_dir / "metadata.json", {})
            relative = state.get("relative_path") if isinstance(state, dict) else ""
            source = self.media_root / relative if relative else None
            try:
                stat_result = source.stat() if source else None
            except OSError:
                continue
            if (
                not isinstance(metadata, dict)
                or metadata.get("cache_key") != cache_dir.name
                or int(metadata.get("cache_version", -1)) != CURRENT_CACHE_VERSION
                or len(metadata.get("hls_variants") or []) != 1
                or not (cache_dir / "hls" / "master.m3u8").is_file()
                or not (cache_dir / "thumbs").is_dir()
                or int(state.get("size", -1)) != stat_result.st_size
                or int(state.get("mtime_ns", -1)) != stat_result.st_mtime_ns
            ):
                continue
            ready.add(relative)
        self.ready_sources_cache = ready
        self.ready_sources_checked_at = now
        return set(ready)

    def published_source_relatives(self):
        catalog = load_json(self.root / "data" / "catalog.json", {})
        items = catalog.get("items", []) if isinstance(catalog, dict) else []
        return {
            item.get("source_relative")
            for item in items
            if isinstance(item, dict) and item.get("source_relative") and (self.media_root / item["source_relative"]).is_file()
        }

    def sources_processed_since(self, started_at):
        """Find sources completed during the current scanner process."""
        if started_at <= 0:
            return set()
        catalog = load_json(self.root / "data" / "catalog.json", {})
        items = catalog.get("items", []) if isinstance(catalog, dict) else []
        processed = set()
        threshold = started_at - 10.0
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            relative = item.get("source_relative")
            timestamp = item.get("processed_at")
            if not relative or not timestamp:
                continue
            try:
                processed_at = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OSError):
                continue
            if processed_at >= threshold and (self.media_root / relative).is_file():
                processed.add(relative)
        return processed

    def current_run_sources(self, relative_paths, current_relative, ready_sources):
        """Return the fixed work list for this scanner run, including completed items."""
        scanner_pid = find_scanner(self.root)
        scanner_key, scanner_started_at = process_identity(scanner_pid) if scanner_pid else ("", 0.0)
        stored_key = str(self.queue_run.get("scanner_identity") or "")
        stored_sources = self.queue_run.get("sources")
        if scanner_key and scanner_key == stored_key and isinstance(stored_sources, list):
            present = set(relative_paths)
            run_sources = [relative for relative in stored_sources if relative in present]
            if current_relative in run_sources:
                return run_sources

        recently_completed = self.sources_processed_since(scanner_started_at)
        work_sources = recently_completed | (set(relative_paths) - ready_sources)
        work_sources.add(current_relative)
        run_sources = [relative for relative in relative_paths if relative in work_sources]
        if current_relative not in run_sources:
            run_sources.append(current_relative)

        self.queue_run = {
            "schema_version": 1,
            "scanner_identity": scanner_key,
            "scanner_pid": scanner_pid,
            "scanner_started_at": utc_iso(scanner_started_at) if scanner_started_at else "",
            "created_at": utc_iso(),
            "sources": run_sources,
        }
        atomic_write_json(self.queue_run_path, self.queue_run)
        return run_sources

    def probe_duration(self, path, allow_packet_scan=True):
        command = [
            self.ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30, check=False)
            duration = number((result.stdout or "").strip().splitlines()[0])
            if duration <= 0 and allow_packet_scan:
                duration = packet_timeline_duration(self.ffprobe, path)
            return duration
        except (OSError, IndexError, subprocess.TimeoutExpired):
            return 0.0

    def duration_index_worker(self):
        while True:
            changed = False
            pending_writes = 0
            videos = discover_videos(self.media_root)
            present = {path.relative_to(self.media_root).as_posix() for path in videos}
            with self.duration_lock:
                for relative in list(self.duration_index):
                    if relative not in present:
                        self.duration_index.pop(relative, None)
                        changed = True
            for path in videos:
                if self.indexed_duration(path) > 0:
                    continue
                # Avoid sweeping every unfinished WebM from disk while FFmpeg is
                # already busy.  The active source is indexed by probe_source;
                # idle passes fill the rest of the queue progressively.
                duration = self.probe_duration(path, allow_packet_scan=not bool(find_ffmpeg(self.root)[0]))
                if self.remember_duration(path, duration):
                    changed = True
                    pending_writes += 1
                    if pending_writes >= 5:
                        self.write_duration_index()
                        pending_writes = 0
                time.sleep(0.05)
            if changed or pending_writes:
                self.write_duration_index()
            time.sleep(60)

    def start_duration_indexer(self):
        if self.duration_thread and self.duration_thread.is_alive():
            return
        self.duration_thread = threading.Thread(target=self.duration_index_worker, name="queue-duration-index", daemon=True)
        self.duration_thread.start()

    def publish_preview(self, phase, output, position_seconds, output_count, interval=10.0):
        output_path = Path(output)
        if phase in {"hls_encode", "combined_encode"}:
            try:
                build_dir = output_path.parents[2]
            except IndexError:
                return "", 0.0
            desired_index = max(0, int(position_seconds // interval))
        elif phase == "thumbnails":
            build_dir = output_path.parent.parent
            desired_index = max(0, output_count - 1)
        else:
            return "", 0.0

        thumbnail_dir = build_dir / "thumbs"
        exact = thumbnail_dir / "thumb-{:06d}.jpg".format(desired_index)
        if exact.is_file():
            selected = exact
        else:
            candidates = sorted(thumbnail_dir.glob("thumb-*.jpg")) if thumbnail_dir.is_dir() else []
            if not candidates:
                return "", 0.0
            eligible = [path for path in candidates if int(path.stem.rsplit("-", 1)[-1]) <= desired_index]
            selected = eligible[-1] if eligible else candidates[0]

        try:
            selected_index = int(selected.stem.rsplit("-", 1)[-1])
            stat_result = selected.stat()
            key = (str(selected), stat_result.st_size, stat_result.st_mtime_ns)
            if key != self.preview_key:
                atomic_copy(selected, self.root / "data" / "encode-preview.jpg")
                self.preview_key = key
            token = "{}-{}".format(selected_index, stat_result.st_mtime_ns)
            return "data/encode-preview.jpg?v=" + token, selected_index * interval
        except (OSError, ValueError):
            return "", 0.0

    def probe_source(self, source):
        try:
            signature = (str(source), source.stat().st_size, source.stat().st_mtime_ns)
        except OSError:
            signature = (str(source), 0, 0)
        if signature in self.probe_cache:
            return self.probe_cache[signature]
        command = [
            self.ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
            "-show_entries", "format=duration", "-of", "json", str(source),
        ]
        metadata = {"duration_seconds": 0.0, "source_fps": 0.0, "codec": "unknown", "width": 0, "height": 0}
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30, check=False)
            payload = json.loads(result.stdout or "{}")
            stream = (payload.get("streams") or [{}])[0]
            metadata.update({
                "duration_seconds": number((payload.get("format") or {}).get("duration")),
                "source_fps": fraction(stream.get("avg_frame_rate")) or fraction(stream.get("r_frame_rate")),
                "codec": stream.get("codec_name") or "unknown",
                "width": int(number(stream.get("width"))),
                "height": int(number(stream.get("height"))),
            })
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if metadata["duration_seconds"] <= 0:
            metadata["duration_seconds"] = packet_timeline_duration(self.ffprobe, source)
        self.probe_cache = {signature: metadata}
        if self.remember_duration(source, metadata["duration_seconds"]):
            self.write_duration_index()
        return metadata

    def queue_details(self, source, current_fraction, prediction_speed, forecast_overhead=1.08):
        videos = discover_videos(self.media_root)
        relative = source.relative_to(self.media_root).as_posix()
        relative_paths = [path.relative_to(self.media_root).as_posix() for path in videos]
        path_by_relative = dict(zip(relative_paths, videos))
        ready_sources = self.ready_source_relatives()
        published_sources = self.published_source_relatives()
        run_sources = self.current_run_sources(relative_paths, relative, ready_sources)
        try:
            run_zero_index = run_sources.index(relative)
        except ValueError:
            run_zero_index = 0
        upcoming = [item for item in run_sources[run_zero_index + 1:] if item not in ready_sources]
        with self.duration_lock:
            duration_entries = dict(self.duration_index)
        library_durations = [self.indexed_duration(path, duration_entries) for path in videos]
        run_paths = [path_by_relative[item] for item in run_sources if item in path_by_relative]
        run_durations = [self.indexed_duration(path, duration_entries) for path in run_paths]
        duration_by_relative = {
            path.relative_to(self.media_root).as_posix(): duration
            for path, duration in zip(run_paths, run_durations)
        }
        indexed_count = sum(1 for duration in run_durations if duration > 0)
        library_indexed_count = sum(1 for duration in library_durations if duration > 0)
        total_duration = sum(run_durations)
        current_duration = duration_by_relative.get(relative, 0.0)
        completed_duration = sum(
            duration for relative_path, duration in duration_by_relative.items()
            if relative_path in ready_sources
        )
        current_fraction = min(1.0, max(0.0, number(current_fraction)))
        if relative not in ready_sources:
            completed_duration += current_duration * current_fraction
        remaining_duration = sum(
            duration for relative_path, duration in duration_by_relative.items()
            if relative_path not in ready_sources
        )
        if relative not in ready_sources:
            remaining_duration = max(0.0, remaining_duration - current_duration * current_fraction)
        durations_complete = bool(run_sources) and indexed_count == len(run_sources)
        predicted_processing = (remaining_duration / prediction_speed * forecast_overhead) if durations_complete and prediction_speed > 0 else 0.0
        predicted_finish_at = utc_iso(time.time() + predicted_processing) if predicted_processing > 0 else ""
        return {
            "position": run_zero_index + 1,
            "total": len(run_sources),
            "completed": run_zero_index,
            "ready": len(ready_sources),
            "published": len(published_sources),
            "remaining_after_current": len(upcoming),
            "upcoming": upcoming,
            "total_duration_seconds": round(total_duration, 3),
            "completed_duration_seconds": round(completed_duration, 3),
            "remaining_duration_seconds": round(remaining_duration, 3),
            "duration_indexed_count": indexed_count,
            "duration_index_complete": durations_complete,
            "library_total": len(videos),
            "library_ready": len(ready_sources),
            "library_published": len(published_sources),
            "library_duration_indexed_count": library_indexed_count,
            "library_duration_index_complete": bool(videos) and library_indexed_count == len(videos),
            "prediction_speed": round(prediction_speed, 3),
            "predicted_processing_seconds": round(predicted_processing, 1),
            "predicted_finish_at": predicted_finish_at,
        }

    def active_snapshot(self, pid, arguments):
        source = source_from_arguments(arguments, self.media_root)
        if source is None:
            return None
        metadata = self.probe_source(source)
        elapsed, cpu_percent = process_times(pid)
        hls_output = output_argument(arguments, ".m3u8")
        thumbnail_output = output_argument(arguments, ".jpg")
        if hls_output and thumbnail_output:
            phase = "combined_encode"
            output = hls_output
        elif hls_output:
            phase = "hls_encode"
            output = hls_output
        elif thumbnail_output:
            phase = "thumbnails"
            output = thumbnail_output
        else:
            phase = "processing"
            output = arguments[-1] if arguments else ""
        interval = 10.0
        segment_seconds = number(option_value(arguments, "-hls_time"), 6.0)
        if phase in {"hls_encode", "combined_encode"}:
            if phase == "combined_encode":
                match = re.search(r"gte\(t-prev_selected_t\\?,([0-9.]+)\)", option_value(arguments, "-filter_complex"))
                interval = number(match.group(1), 10.0) if match else 10.0
            position_seconds, output_count = parse_playlist_position(Path(output), segment_seconds)
            output_fps = min(metadata["source_fps"], 30.0) if metadata["source_fps"] > 0 else 30.0
            if phase == "combined_encode":
                phase_label = "Encoding HLS + 10-second thumbnails"
                pass_label = "Single pass"
                pass_number = 1
                pass_total = 1
                phase_share = 0.0
                phase_weight = 1.0
            else:
                phase_label = "Encoding phone-ready HLS"
                pass_label = "Pass 2 of 2"
                pass_number = 2
                pass_total = 2
                phase_share = 0.10
                phase_weight = 0.90
        elif phase == "thumbnails":
            match = re.search(r"gte\(t-prev_selected_t\\?,([0-9.]+)\)", option_value(arguments, "-vf"))
            interval = number(match.group(1), 10.0) if match else 10.0
            position_seconds, output_count = thumbnail_position(output, interval)
            output_fps = metadata["source_fps"] or 30.0
            phase_label = "Building 10-second thumbnails"
            pass_label = "Pass 1 of 2"
            pass_number = 1
            pass_total = 2
            phase_share = 0.0
            phase_weight = 0.10
        else:
            position_seconds, output_count = 0.0, 0
            output_fps = metadata["source_fps"] or 30.0
            phase_label = "Processing media"
            pass_label = "Processing"
            pass_number = 1
            pass_total = 1
            phase_share = 0.0
            phase_weight = 1.0

        duration = metadata["duration_seconds"]
        position_seconds = min(position_seconds, duration) if duration > 0 else position_seconds
        percent = min(100.0, position_seconds / duration * 100.0) if duration > 0 else 0.0
        speed = position_seconds / elapsed if elapsed > 0 else 0.0
        processing_fps = speed * output_fps
        eta = (duration - position_seconds) / speed if speed > 0 and duration > position_seconds else 0.0
        if phase in {"hls_encode", "combined_encode"} and speed > 0:
            self.last_hls_speed = speed if self.last_hls_speed <= 0 else self.last_hls_speed * 0.85 + speed * 0.15
        prediction_speed = self.last_hls_speed or (speed if phase in {"hls_encode", "combined_encode"} else 0.7)
        file_fraction = phase_share + phase_weight * (percent / 100.0)
        forecast_overhead = 1.05 if phase == "combined_encode" else 1.12
        queue = self.queue_details(source, file_fraction, prediction_speed, forecast_overhead)
        preview_url, preview_time = self.publish_preview(phase, output, position_seconds, output_count, interval)
        library_total = queue["library_total"]
        overall_percent = ((queue["library_ready"] + file_fraction) / library_total * 100.0) if library_total else 0.0
        queue_percent = ((queue["position"] - 1 + file_fraction) / queue["total"] * 100.0) if queue["total"] else 0.0
        target = Path(output).parent.name if phase == "hls_encode" else "JPEG timeline"

        if phase == "combined_encode":
            target = Path(output).parent.name + " HLS + JPEG timeline"

        parameters = {
            "Pass": pass_label,
            "Phase": phase_label,
            "Source": "{}×{} · {} · {:.3f} fps".format(metadata["width"], metadata["height"], str(metadata["codec"]).upper(), metadata["source_fps"]).replace(".000 fps", " fps"),
            "Output": target,
            "Video encoder": "libx264 + MJPEG" if phase == "combined_encode" else option_value(arguments, "-c:v", "mjpeg" if phase == "thumbnails" else "unknown"),
            "Preset": option_value(arguments, "-preset", "—"),
            "Profile": option_value(arguments, "-profile:v", "—"),
            "Pixel format": option_value(arguments, "-pix_fmt", "—"),
            "Video rate": display_rate(option_value(arguments, "-b:v")),
            "Maximum rate": display_rate(option_value(arguments, "-maxrate")),
            "Rate buffer": display_rate(option_value(arguments, "-bufsize")),
            "Audio": (option_value(arguments, "-c:a") + " · " + display_rate(option_value(arguments, "-b:a"))).strip(" ·—") or "No audio",
            "GOP / segment": "{} frames · {} sec".format(option_value(arguments, "-g", "—"), option_value(arguments, "-hls_time", "—")),
            "Filter graph": option_value(arguments, "-filter_complex", option_value(arguments, "-vf", "—")),
        }
        if phase in {"thumbnails", "combined_encode"}:
            parameters.update({
                "Output interval": "{} sec".format(int(interval) if interval.is_integer() else interval),
                "JPEG quality": "q{}".format(option_value(arguments, "-q:v", "4")),
            })

        relative = source.relative_to(self.media_root).as_posix()
        return {
            "schema_version": 1,
            "active": True,
            "status": "active",
            "phase": phase,
            "phase_label": phase_label,
            "pass_label": pass_label,
            "pass_number": pass_number,
            "pass_total": pass_total,
            "updated_at": utc_iso(),
            "pid": pid,
            "source": relative,
            "source_name": source.name,
            "position_seconds": round(position_seconds, 3),
            "duration_seconds": round(duration, 3),
            "percent": round(percent, 2),
            "overall_percent": round(overall_percent, 2),
            "queue_percent": round(queue_percent, 2),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta, 1),
            "source_fps": round(metadata["source_fps"], 3),
            "output_fps": round(output_fps, 3),
            "processing_fps": round(processing_fps, 2),
            "speed": round(speed, 3),
            "cpu_percent": round(cpu_percent, 1),
            "output_count": output_count,
            "preview_url": preview_url,
            "preview_time_seconds": round(preview_time, 3),
            "queue": queue,
            "parameters": parameters,
            "command": sanitize_command(arguments, self.root),
            "note": "FPS and speed are calculated from completed HLS segments or thumbnail intervals and update as output is published. Queue prediction uses weighted whole-file progress, so it no longer resets between passes.",
        }

    def idle_snapshot(self):
        scanner_pid = find_scanner(self.root)
        now = time.time()
        if scanner_pid and self.last_active and now - self.last_seen_at < 30:
            payload = dict(self.last_active)
            payload.update({
                "status": "preparing",
                "phase": "preparing",
                "phase_label": "Finalizing this phase",
                "updated_at": utc_iso(),
                "pid": scanner_pid,
                "processing_fps": 0.0,
                "speed": 0.0,
                "eta_seconds": 0.0,
            })
            return payload
        videos = discover_videos(self.media_root)
        ready_count = len(self.ready_source_relatives())
        published_count = len(self.published_source_relatives())
        return {
            "schema_version": 1,
            "active": False,
            "status": "idle",
            "phase": "idle",
            "phase_label": "Encoder idle",
            "updated_at": utc_iso(),
            "source": "",
            "source_name": "",
            "percent": 0.0,
            "overall_percent": round(ready_count / len(videos) * 100.0, 2) if videos else 100.0,
            "queue_percent": 0.0,
            "processing_fps": 0.0,
            "speed": 0.0,
            "cpu_percent": 0.0,
            "eta_seconds": 0.0,
            "queue": {"position": 0, "total": 0, "completed": 0, "ready": ready_count, "published": published_count, "remaining_after_current": 0, "upcoming": [], "total_duration_seconds": 0.0, "remaining_duration_seconds": 0.0, "duration_indexed_count": 0, "duration_index_complete": False, "library_total": len(videos), "library_ready": ready_count, "library_published": published_count, "library_duration_indexed_count": 0, "library_duration_index_complete": False, "predicted_processing_seconds": 0.0, "predicted_finish_at": ""},
            "parameters": {},
            "command": "",
            "preview_url": "",
            "preview_time_seconds": 0.0,
            "note": "No FFmpeg job is running. The monitor will update automatically when the scanner starts one.",
        }

    def snapshot(self):
        pid, arguments = find_ffmpeg(self.root)
        payload = self.active_snapshot(pid, arguments) if pid else None
        if payload:
            self.last_active = payload
            self.last_seen_at = time.time()
            return payload
        return self.idle_snapshot()

    def publish(self):
        self.prune_missing_catalog_items()
        payload = self.snapshot()
        atomic_write_json(self.output_path, payload)
        return payload


def log_line(payload):
    if not payload.get("active"):
        return "IDLE no active FFmpeg job"
    queue = payload.get("queue") or {}
    return "{phase} {position}/{total} {source} | {percent:.1f}% | {fps:.1f} fps | {speed:.2f}x | CPU {cpu:.0f}% | ETA {eta:.0f}s".format(
        phase=payload.get("phase", "work").upper(),
        position=queue.get("position", 0),
        total=queue.get("total", 0),
        source=payload.get("source_name", "unknown"),
        percent=number(payload.get("percent")),
        fps=number(payload.get("processing_fps")),
        speed=number(payload.get("speed")),
        cpu=number(payload.get("cpu_percent")),
        eta=number(payload.get("eta_seconds")),
    )


def parse_arguments():
    parser = argparse.ArgumentParser(description="Publish live FFmpeg progress for the video library.")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", Path(__file__).resolve().parent.parent))
    parser.add_argument("--ffprobe", default=os.environ.get("VIDEO_FFPROBE", "ffprobe"))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--log-interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    monitor = EncodingMonitor(Path(arguments.root).expanduser().resolve(), arguments.ffprobe)
    if arguments.once:
        payload = monitor.publish()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    monitor.start_duration_indexer()
    last_log = 0.0
    previous_line = ""
    while True:
        payload = monitor.publish()
        line = log_line(payload)
        now = time.monotonic()
        if line != previous_line and (now - last_log >= max(1.0, arguments.log_interval) or not payload.get("active")):
            print("[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line), flush=True)
            previous_line = line
            last_log = now
        time.sleep(max(0.5, arguments.interval))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
