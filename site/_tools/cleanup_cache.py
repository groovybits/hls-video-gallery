#!/usr/bin/env python3
"""Remove generated cache directories that the live catalog no longer uses."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys


GENERATED_CACHE = re.compile(r"^[0-9a-f]{18}--[0-9a-f]{14}$")
TEMPORARY_CACHE = re.compile(r"^\.(?:building|old)-")


def allocated_bytes(path):
    total = 0
    try:
        total += path.lstat().st_blocks * 512
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return total
    for directory, child_directories, filenames in os.walk(str(path), followlinks=False):
        for name in child_directories + filenames:
            child = Path(directory) / name
            try:
                total += child.lstat().st_blocks * 512
            except OSError:
                pass
    return total


def human_size(value):
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return "{:.1f} {}".format(amount, unit) if unit != "B" else "{} B".format(int(amount))
        amount /= 1024.0
    return "{} B".format(value)


def load_catalog(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError("Cannot read the live catalog {}: {}".format(path, error))
    items = catalog.get("items") if isinstance(catalog, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("The live catalog does not contain a valid items list")
    return catalog


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Delete unreferenced generated video cache output.")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)), help="Video application root")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be removed without deleting it")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    marker = root / ".hls-video-gallery"
    cache_root = root / "cache"
    data_root = root / "data"

    if not marker.is_file():
        raise RuntimeError("Refusing cleanup because the application marker is missing from {}".format(root))
    if cache_root.is_symlink() or not cache_root.is_dir() or cache_root.resolve().parent != root:
        raise RuntimeError("Refusing cleanup because the cache directory is invalid")

    catalog = load_catalog(data_root / "catalog.json")
    active_keys = {
        str(item.get("cache_key"))
        for item in catalog["items"]
        if isinstance(item, dict) and GENERATED_CACHE.fullmatch(str(item.get("cache_key") or ""))
    }

    lock_handle = (data_root / "scan.lock").open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("A catalog scan is still running; wait for it to finish before cleaning")

    removed = 0
    reclaimed = 0
    preserved = 0
    ignored = 0
    for entry in sorted(cache_root.iterdir(), key=lambda value: value.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name in active_keys:
            preserved += 1
            continue
        if not GENERATED_CACHE.fullmatch(entry.name) and not TEMPORARY_CACHE.match(entry.name):
            ignored += 1
            continue
        size = allocated_bytes(entry)
        action = "WOULD REMOVE" if arguments.dry_run else "REMOVE"
        print("{} {} ({})".format(action, entry.name, human_size(size)), flush=True)
        if not arguments.dry_run:
            shutil.rmtree(str(entry))
        removed += 1
        reclaimed += size

    verb = "would reclaim" if arguments.dry_run else "reclaimed"
    print(
        "Cleanup complete: {} unused cache director{}, {} {}; {} active preserved, {} unknown ignored".format(
            removed,
            "y" if removed == 1 else "ies",
            verb,
            human_size(reclaimed),
            preserved,
            ignored,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as error:
        print("Cleanup stopped safely: {}".format(error), file=sys.stderr)
        sys.exit(2)
