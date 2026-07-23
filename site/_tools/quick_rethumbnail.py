#!/usr/bin/env python3
"""Refresh thumbnails for currently listed legacy videos without taking the main scan lock."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote


INTERVAL = 10


def log(message):
    print("[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def utc_iso():
    return dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path, value):
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
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_source(media_root, relative):
    relative_path = Path(str(relative))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeError("unsafe source path")
    source = (media_root / relative_path).resolve()
    try:
        source.relative_to(media_root)
    except ValueError:
        raise RuntimeError("source escapes media directory")
    if not source.is_file():
        raise RuntimeError("source file is missing")
    return source


def safe_cache(cache_root, cache_key):
    cache_key = str(cache_key or "")
    if not cache_key or cache_key.startswith(".") or Path(cache_key).name != cache_key:
        raise RuntimeError("unsafe cache key")
    cache_dir = cache_root / cache_key
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise RuntimeError("cache directory is missing")
    return cache_dir


def catalog_still_lists(catalog_path, item_id, cache_key):
    catalog = load_json(catalog_path)
    for item in catalog.get("items") or []:
        if item.get("id") == item_id and item.get("cache_key") == cache_key:
            return catalog, item
    return catalog, None


def generate_thumbnails(ffmpeg, source, stream_index, output_dir):
    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o755)
    pattern = output_dir / "thumb-%06d.jpg"
    video_filter = "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,10)',scale=w='min(480,iw)':h=-2:force_original_aspect_ratio=decrease,setsar=1"
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-filter_threads", "1", "-threads", "1",
        "-i", str(source),
        "-map", "0:{}".format(stream_index),
        "-an", "-sn", "-dn",
        "-vf", video_filter,
        "-fps_mode", "vfr",
        "-q:v", "4",
        "-start_number", "0",
        str(pattern),
    ]
    process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if process.returncode:
        tail = "\n".join((process.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError("ffmpeg failed: {}".format(tail))
    images = sorted(output_dir.glob("thumb-*.jpg"))
    if not images:
        raise RuntimeError("ffmpeg produced no thumbnails")
    for image in images:
        os.chmod(image, 0o644)
    return images


def publish_catalog_item(catalog_path, item_id, cache_key, thumbnails):
    catalog, live_item = catalog_still_lists(catalog_path, item_id, cache_key)
    if not live_item:
        return False
    live_item["thumbnails"] = thumbnails
    live_item["poster_url"] = thumbnails[0]["url"]
    live_item["quick_thumbnail_interval_seconds"] = INTERVAL
    catalog["thumbnail_interval_seconds"] = INTERVAL
    catalog["generated_at"] = utc_iso()
    atomic_write_json(catalog_path, catalog)
    return True


def process_item(ffmpeg, root, catalog_path, item, position, total):
    item_id = str(item.get("id") or "")
    cache_key = str(item.get("cache_key") or "")
    _catalog, live_item = catalog_still_lists(catalog_path, item_id, cache_key)
    if not live_item:
        log("STOP: the main catalog has replaced the legacy entries")
        return False

    source = safe_source(root / "media", item.get("source_relative"))
    cache_dir = safe_cache(root / "cache", cache_key)
    stream_index = int(((item.get("video_streams") or [{}])[0]).get("index", 0))
    duration = float(item.get("duration_seconds") or 0)
    temporary = Path(tempfile.mkdtemp(prefix=".thumbs10-building-", dir=str(cache_dir)))
    try:
        log("THUMBS {}/{} {}".format(position, total, item.get("source_relative")))
        images = generate_thumbnails(ffmpeg, source, stream_index, temporary)
        final_dir = cache_dir / "thumbs10"
        backup = cache_dir / ".thumbs10-old"
        if backup.exists():
            shutil.rmtree(str(backup), ignore_errors=True)
        if final_dir.exists():
            os.replace(str(final_dir), str(backup))
        os.replace(str(temporary), str(final_dir))
        shutil.rmtree(str(backup), ignore_errors=True)

        thumbnails = [
            {
                "time_seconds": min(float(index * INTERVAL), duration),
                "url": "cache/{}/thumbs10/{}".format(quote(cache_key, safe=""), quote(image.name, safe="")),
            }
            for index, image in enumerate(images)
        ]
        metadata_path = cache_dir / "metadata.json"
        metadata = load_json(metadata_path)
        metadata["thumbnails"] = thumbnails
        metadata["poster_url"] = thumbnails[0]["url"]
        metadata["quick_thumbnail_interval_seconds"] = INTERVAL
        atomic_write_json(metadata_path, metadata)

        if not publish_catalog_item(catalog_path, item_id, cache_key, thumbnails):
            log("STOP: the main catalog changed before publish")
            return False
        log("READY {}/{} {} -> {} thumbnails".format(position, total, item.get("source_relative"), len(thumbnails)))
        return True
    finally:
        if temporary.exists():
            shutil.rmtree(str(temporary), ignore_errors=True)


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Refresh currently visible legacy thumbnails at 10-second intervals.")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)))
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    if not (root / ".hls-video-gallery").is_file():
        raise RuntimeError("application marker is missing")
    ffmpeg = shutil.which(os.environ.get("VIDEO_FFMPEG", "ffmpeg"))
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")

    lock_handle = (root / "data" / "rethumbnail.lock").open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another quick thumbnail worker is already running")
        return 0

    catalog_path = root / "data" / "catalog.json"
    catalog = load_json(catalog_path)
    items = [
        item
        for item in catalog.get("items") or []
        if int(item.get("cache_version") or 0) < 6 and int(item.get("quick_thumbnail_interval_seconds") or 0) != INTERVAL
    ]
    log("Found {} visible legacy video{} to refresh".format(len(items), "" if len(items) == 1 else "s"))
    completed = 0
    for position, item in enumerate(items, 1):
        try:
            if not process_item(ffmpeg, root, catalog_path, item, position, len(items)):
                break
            completed += 1
        except Exception as error:
            log("ERROR {}/{} {}: {}".format(position, len(items), item.get("source_relative"), error))
    log("Quick thumbnail refresh complete: {} of {} published".format(completed, len(items)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as error:
        log("STOPPED: {}".format(error))
        sys.exit(2)
