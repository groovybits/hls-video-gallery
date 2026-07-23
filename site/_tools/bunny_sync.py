#!/usr/bin/env python3
"""Incrementally mirror completed video caches to Bunny Edge Storage."""

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import shlex
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlparse


SCHEMA_VERSION = 1
MANAGED_PREFIX = "hls-video-gallery/v1/cache"
CACHE_KEY_RE = re.compile(r"^[0-9a-f]{18}--[0-9a-f]{14}$")
REVISION_RE = re.compile(r"^[0-9a-f]{16}$")
PUBLIC_SUFFIXES = {".jpg", ".jpeg", ".m3u8", ".png", ".ts", ".webp"}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_CONFIG = "/etc/hls-video-gallery/bunny.env"
DEFAULT_WORKERS = 4
thread_local = threading.local()


class SyncError(RuntimeError):
    pass


class ConfigurationError(SyncError):
    pass


class AuthenticationError(SyncError):
    pass


def utc_iso(timestamp=None):
    if timestamp is None:
        timestamp = time.time()
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def format_bytes(value):
    amount = float(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    return "{:.0f} {}".format(amount, units[index]) if index == 0 or amount >= 10 else "{:.1f} {}".format(amount, units[index])


def atomic_write_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_json(path, fallback=None):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return fallback


def load_env(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigurationError("Cannot read Bunny configuration {}: {}".format(path, error))
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = raw_value.strip().strip("\"'")
        values[key] = value
    return values


def storage_http_endpoint(configured):
    """Accept either Bunny's HTTP or S3 endpoint and select the HTTP API host."""
    parsed = urlparse(configured if "://" in configured else "https://" + configured)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ConfigurationError("BUNNY_STORAGE_ENDPOINT is invalid")
    s3_match = re.match(r"^([a-z]+)-s3\.storage\.bunnycdn\.com$", host)
    if s3_match:
        region = s3_match.group(1)
        host = "storage.bunnycdn.com" if region in {"de", "eu"} else "{}.storage.bunnycdn.com".format(region)
    if not re.match(r"^(?:[a-z]+\.)?storage\.bunnycdn\.com$", host):
        raise ConfigurationError("BUNNY_STORAGE_ENDPOINT is not a Bunny Storage endpoint")
    return host


def validated_config(path):
    values = load_env(path)
    required = ("BUNNY_STORAGE_ZONE", "BUNNY_STORAGE_PASSWORD", "BUNNY_STORAGE_ENDPOINT", "BUNNY_CDN_HOST")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ConfigurationError("Missing Bunny setting{}: {}".format("s" if len(missing) != 1 else "", ", ".join(missing)))
    zone = values["BUNNY_STORAGE_ZONE"]
    if not re.match(r"^[A-Za-z0-9_-]+$", zone):
        raise ConfigurationError("BUNNY_STORAGE_ZONE contains unsupported characters")
    cdn_host = values["BUNNY_CDN_HOST"].strip().lower()
    if "://" in cdn_host:
        cdn_host = urlparse(cdn_host).hostname or ""
    if not re.match(r"^[a-z0-9.-]+$", cdn_host):
        raise ConfigurationError("BUNNY_CDN_HOST is invalid")
    return {
        "zone": zone,
        "password": values["BUNNY_STORAGE_PASSWORD"],
        "storage_host": storage_http_endpoint(values["BUNNY_STORAGE_ENDPOINT"]),
        "cdn_host": cdn_host,
    }


def request_path(zone, remote_path, directory=False):
    components = [quote(zone, safe="")]
    components.extend(quote(part, safe="") for part in remote_path.strip("/").split("/") if part)
    path = "/" + "/".join(components)
    return path + "/" if directory else path


class StorageClient:
    def __init__(self, host, zone, access_key, timeout=180):
        self.host = host
        self.zone = zone
        self.access_key = access_key
        self.timeout = timeout
        self.connection = None

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None

    def connect(self):
        if self.connection is None:
            context = ssl.create_default_context()
            self.connection = http.client.HTTPSConnection(self.host, timeout=self.timeout, context=context)
        return self.connection

    def upload(self, local_path, remote_path):
        content_type = content_type_for(local_path)
        size = local_path.stat().st_size
        path = request_path(self.zone, remote_path)
        last_error = None
        for attempt in range(5):
            try:
                connection = self.connect()
                with local_path.open("rb") as body:
                    connection.request("PUT", path, body=body, headers={
                        "AccessKey": self.access_key,
                        "Content-Type": content_type,
                        "Content-Length": str(size),
                        "User-Agent": "HLSVideoGalleryBunnySync/1.0",
                    })
                    response = connection.getresponse()
                    payload = response.read(4096)
                if response.status == 201:
                    return size
                if response.status == 401:
                    raise AuthenticationError("Bunny Storage rejected the access key or region endpoint (HTTP 401)")
                message = payload.decode("utf-8", errors="replace").strip()[:300]
                last_error = SyncError("Upload returned HTTP {}{}".format(response.status, ": " + message if message else ""))
                if response.status not in RETRYABLE_STATUS:
                    raise last_error
            except AuthenticationError:
                raise
            except Exception as error:
                last_error = error
            self.close()
            if attempt < 4:
                time.sleep(min(12, 1.5 * (2 ** attempt)))
        raise SyncError("Upload failed for {}: {}".format(local_path.name, last_error))

    def delete_directory(self, remote_path):
        path = request_path(self.zone, remote_path, directory=True)
        last_error = None
        for attempt in range(4):
            try:
                connection = self.connect()
                connection.request("DELETE", path, headers={
                    "AccessKey": self.access_key,
                    "User-Agent": "HLSVideoGalleryBunnySync/1.0",
                })
                response = connection.getresponse()
                payload = response.read(4096)
                if response.status in {200, 204, 404}:
                    return
                if response.status == 401:
                    raise AuthenticationError("Bunny Storage rejected the access key or region endpoint (HTTP 401)")
                message = payload.decode("utf-8", errors="replace").strip()[:300]
                last_error = SyncError("Delete returned HTTP {}{}".format(response.status, ": " + message if message else ""))
                if response.status not in RETRYABLE_STATUS:
                    raise last_error
            except AuthenticationError:
                raise
            except Exception as error:
                last_error = error
            self.close()
            if attempt < 3:
                time.sleep(min(10, 1.5 * (2 ** attempt)))
        raise SyncError("Remote cleanup failed: {}".format(last_error))

    def probe(self):
        path = request_path(self.zone, "", directory=True)
        connection = self.connect()
        connection.request("GET", path, headers={"AccessKey": self.access_key, "User-Agent": "HLSVideoGalleryBunnySync/1.0"})
        response = connection.getresponse()
        response.read(4096)
        if response.status == 401:
            raise AuthenticationError("Bunny Storage rejected the access key or region endpoint (HTTP 401)")
        if response.status != 200:
            raise SyncError("Bunny Storage probe returned HTTP {}".format(response.status))


def content_type_for(path):
    suffix = path.suffix.lower()
    known = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".m3u8": "application/vnd.apple.mpegurl",
        ".png": "image/png",
        ".ts": "video/mp2t",
        ".webp": "image/webp",
    }
    return known.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def worker_client(config):
    client = getattr(thread_local, "storage_client", None)
    if client is None:
        client = StorageClient(config["storage_host"], config["zone"], config["password"])
        thread_local.storage_client = client
    return client


def catalog_items(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise SyncError("The local catalog is missing or invalid; no uploads or deletions were attempted")
    items = {}
    for item in catalog["items"]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("cache_key") or "")
        if CACHE_KEY_RE.fullmatch(key):
            items[key] = item
    return items


def public_files(cache_dir):
    files = []
    for top_name in ("thumbs", "thumbs10", "hls"):
        top = cache_dir / top_name
        if not top.is_dir() or top.is_symlink():
            continue
        for path in top.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in PUBLIC_SUFFIXES:
                continue
            relative = path.relative_to(cache_dir).as_posix()
            if any(part.startswith(".") for part in Path(relative).parts):
                continue
            stat_result = path.stat()
            files.append({"path": path, "relative": relative, "size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns})
    if not (cache_dir / "hls" / "master.m3u8").is_file():
        raise SyncError("{} is missing hls/master.m3u8".format(cache_dir.name))
    if not any(entry["relative"].startswith(("thumbs/", "thumbs10/")) for entry in files):
        raise SyncError("{} has no public thumbnails".format(cache_dir.name))
    return sorted(files, key=upload_order)


def upload_order(entry):
    relative = entry["relative"]
    if relative.endswith("hls/master.m3u8"):
        priority = 3
    elif relative.endswith(".m3u8"):
        priority = 2
    elif relative.startswith(("thumbs/", "thumbs10/")):
        priority = 0
    else:
        priority = 1
    return priority, relative


def cache_revision(cache_dir, files):
    metadata = load_json(cache_dir / "metadata.json", {})
    digest = hashlib.sha256()
    digest.update(cache_dir.name.encode("ascii"))
    digest.update(str(metadata.get("processed_at") or "").encode("utf-8"))
    for entry in sorted(files, key=lambda value: value["relative"]):
        digest.update("\0{}:{}:{}".format(entry["relative"], entry["size"], entry["mtime_ns"]).encode("utf-8"))
    return digest.hexdigest()[:16]


def candidates(root, items):
    result = []
    for key, item in items.items():
        cache_dir = root / "cache" / key
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            continue
        metadata = load_json(cache_dir / "metadata.json", {})
        if metadata.get("cache_key") != key:
            continue
        files = public_files(cache_dir)
        revision = cache_revision(cache_dir, files)
        result.append({
            "key": key,
            "item": item,
            "revision": revision,
            "files": files,
            "bytes": sum(entry["size"] for entry in files),
            "remote_prefix": "{}/{}/{}/".format(MANAGED_PREFIX, key, revision),
        })
    return sorted(result, key=lambda entry: (entry["bytes"], entry["key"]))


def default_state(config):
    return {
        "schema_version": SCHEMA_VERSION,
        "storage_zone": config["zone"],
        "cdn_host": config["cdn_host"],
        "entries": {},
        "tombstones": {},
        "obsolete": [],
    }


def load_state(path, config):
    state = load_json(path, None)
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        return default_state(config)
    if state.get("storage_zone") != config["zone"]:
        raise ConfigurationError("Bunny storage zone changed; move the old sync state aside after reviewing remote data")
    state["cdn_host"] = config["cdn_host"]
    state.setdefault("entries", {})
    state.setdefault("tombstones", {})
    state.setdefault("obsolete", [])
    return state


def publish_map(path, state, config):
    entries = {}
    for key, record in state.get("entries", {}).items():
        if not CACHE_KEY_RE.fullmatch(key) or not record.get("complete"):
            continue
        remote_prefix = str(record.get("remote_prefix") or "")
        revision = str(record.get("revision") or "")
        if not remote_prefix.startswith(MANAGED_PREFIX + "/" + key + "/") or not REVISION_RE.fullmatch(revision):
            continue
        entries[key] = {
            "revision": revision,
            "remote_prefix": remote_prefix,
            "completed_at": record.get("completed_at"),
            "file_count": int(record.get("file_count") or 0),
            "bytes": int(record.get("bytes") or 0),
        }
    value = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_iso(),
        "cdn_host": config["cdn_host"],
        "entries": entries,
    }
    atomic_write_json(path, value, 0o644)


def write_status(path, phase, **values):
    status = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_iso(),
        "phase": phase,
    }
    status.update(values)
    atomic_write_json(path, status, 0o644)


def upload_one(config, local_path, remote_path):
    return worker_client(config).upload(local_path, remote_path)


def sync_candidate(candidate, state, state_path, map_path, status_path, config, workers, totals):
    key = candidate["key"]
    revision = candidate["revision"]
    previous = state["entries"].get(key) or {}
    if previous.get("revision") != revision:
        if previous.get("complete") and previous.get("remote_prefix"):
            state["obsolete"].append({
                "remote_prefix": previous["remote_prefix"],
                "delete_after": time.time() + 86400,
            })
        previous = {
            "revision": revision,
            "remote_prefix": candidate["remote_prefix"],
            "uploaded": {},
            "complete": False,
        }
        state["entries"][key] = previous
        atomic_write_json(state_path, state, 0o600)

    uploaded = previous.setdefault("uploaded", {})
    pending = [entry for entry in candidate["files"] if int(uploaded.get(entry["relative"], -1)) != entry["size"]]
    if not pending:
        previous.update({
            "complete": True,
            "completed_at": previous.get("completed_at") or utc_iso(),
            "file_count": len(candidate["files"]),
            "bytes": candidate["bytes"],
        })
        atomic_write_json(state_path, state, 0o600)
        publish_map(map_path, state, config)
        return 0

    uploaded_this_item = 0
    started = time.monotonic()
    write_status(status_path, "uploading", current_key=key, current_title=candidate["item"].get("title"),
                 item_files_done=len(candidate["files"]) - len(pending), item_files_total=len(candidate["files"]),
                 item_bytes_total=candidate["bytes"], uploaded_bytes_session=totals["uploaded"],
                 pending_bytes_session=totals["pending"], synced_items=totals["synced"], total_items=totals["items"])

    last_saved = time.monotonic()
    completed_since_save = 0
    finished_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for entry in pending:
            remote_path = candidate["remote_prefix"] + entry["relative"]
            future = executor.submit(upload_one, config, entry["path"], remote_path)
            futures[future] = entry
        try:
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                transferred = future.result()
                uploaded[entry["relative"]] = entry["size"]
                uploaded_this_item += transferred
                totals["uploaded"] += transferred
                totals["pending"] = max(0, totals["pending"] - transferred)
                completed_since_save += 1
                finished_count += 1
                elapsed = max(0.001, time.monotonic() - started)
                rate = uploaded_this_item / elapsed
                done_files = len(candidate["files"]) - len(pending) + finished_count
                write_status(status_path, "uploading", current_key=key, current_title=candidate["item"].get("title"),
                             current_file=entry["relative"], item_files_done=done_files,
                             item_files_total=len(candidate["files"]), item_bytes_total=candidate["bytes"],
                             uploaded_bytes_session=totals["uploaded"], pending_bytes_session=totals["pending"],
                             bytes_per_second=round(rate), eta_seconds=round(totals["pending"] / rate) if rate else None,
                             synced_items=totals["synced"], total_items=totals["items"])
                if completed_since_save >= 20 or time.monotonic() - last_saved >= 5:
                    atomic_write_json(state_path, state, 0o600)
                    completed_since_save = 0
                    last_saved = time.monotonic()
        except Exception:
            for future in futures:
                future.cancel()
            atomic_write_json(state_path, state, 0o600)
            raise

    previous.update({
        "complete": True,
        "completed_at": utc_iso(),
        "file_count": len(candidate["files"]),
        "bytes": candidate["bytes"],
    })
    atomic_write_json(state_path, state, 0o600)
    publish_map(map_path, state, config)
    return uploaded_this_item


def reconcile_deletions(active_keys, state, state_path, map_path, config, storage):
    now = time.time()
    tombstones = state.setdefault("tombstones", {})
    changed = False
    for key in list(state.get("entries", {})):
        if key in active_keys:
            if key in tombstones:
                tombstones.pop(key, None)
                changed = True
            continue
        first_seen = float(tombstones.get(key) or 0)
        if not first_seen:
            tombstones[key] = now
            changed = True
            continue
        if now - first_seen < 30:
            continue
        storage.delete_directory("{}/{}".format(MANAGED_PREFIX, key))
        state["entries"].pop(key, None)
        tombstones.pop(key, None)
        changed = True

    retained_obsolete = []
    for record in state.get("obsolete", []):
        prefix = str(record.get("remote_prefix") or "")
        delete_after = float(record.get("delete_after") or 0)
        if delete_after > now:
            retained_obsolete.append(record)
            continue
        if re.match(r"^" + re.escape(MANAGED_PREFIX) + r"/[0-9a-f]{18}--[0-9a-f]{14}/[0-9a-f]{16}/$", prefix):
            storage.delete_directory(prefix)
            changed = True
    if retained_obsolete != state.get("obsolete", []):
        state["obsolete"] = retained_obsolete
        changed = True
    if changed:
        atomic_write_json(state_path, state, 0o600)
        publish_map(map_path, state, config)


def run_sync(root, config_path, workers):
    config = validated_config(config_path)
    data_root = root / "data"
    catalog_path = data_root / "catalog.json"
    state_path = data_root / "bunny-sync-state.json"
    map_path = data_root / "cdn-map.json"
    status_path = data_root / "bunny-sync-status.json"
    lock_path = data_root / "bunny-sync.lock"
    data_root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        catalog = load_json(catalog_path, None)
        items = catalog_items(catalog)
        state = load_state(state_path, config)
        prepared = candidates(root, items)
        active_keys = {entry["key"] for entry in prepared}
        pending_bytes = 0
        synced = 0
        for entry in prepared:
            record = state["entries"].get(entry["key"]) or {}
            if record.get("complete") and record.get("revision") == entry["revision"]:
                synced += 1
                continue
            uploaded = record.get("uploaded", {}) if record.get("revision") == entry["revision"] else {}
            pending_bytes += sum(file_entry["size"] for file_entry in entry["files"] if int(uploaded.get(file_entry["relative"], -1)) != file_entry["size"])
        totals = {"items": len(prepared), "synced": synced, "pending": pending_bytes, "uploaded": 0}
        write_status(status_path, "starting", synced_items=synced, total_items=len(prepared), pending_bytes_session=pending_bytes)
        storage = StorageClient(config["storage_host"], config["zone"], config["password"])
        storage.probe()
        for entry in prepared:
            record = state["entries"].get(entry["key"]) or {}
            if record.get("complete") and record.get("revision") == entry["revision"]:
                continue
            sync_candidate(entry, state, state_path, map_path, status_path, config, workers, totals)
            totals["synced"] += 1
        reconcile_deletions(active_keys, state, state_path, map_path, config, storage)
        atomic_write_json(state_path, state, 0o600)
        publish_map(map_path, state, config)
        write_status(status_path, "idle", synced_items=totals["synced"], total_items=len(prepared),
                     uploaded_bytes_session=totals["uploaded"], pending_bytes_session=0, last_success_at=utc_iso())
    return 0


def print_status(root):
    status = load_json(root / "data" / "bunny-sync-status.json", None)
    if not isinstance(status, dict):
        print("Bunny sync has not published status yet.")
        return 1
    print("Bunny CDN sync: {}".format(status.get("phase", "unknown")))
    print("Updated: {}".format(status.get("updated_at", "unknown")))
    print("Ready: {}/{} video caches".format(status.get("synced_items", 0), status.get("total_items", 0)))
    if status.get("current_title"):
        print("Current: {}".format(status["current_title"]))
    if status.get("item_files_total"):
        print("Files: {}/{}".format(status.get("item_files_done", 0), status["item_files_total"]))
    if status.get("uploaded_bytes_session") is not None:
        print("Uploaded this pass: {}".format(format_bytes(status.get("uploaded_bytes_session"))))
    if status.get("pending_bytes_session") is not None:
        print("Remaining this pass: {}".format(format_bytes(status.get("pending_bytes_session"))))
    if status.get("bytes_per_second"):
        print("Transfer: {}/s".format(format_bytes(status["bytes_per_second"])))
    if status.get("eta_seconds") is not None:
        print("Estimated remaining: {} minutes".format(max(1, round(float(status["eta_seconds"]) / 60))))
    if status.get("error"):
        print("Error: {}".format(status["error"]))
        return 2
    return 0


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Mirror completed video caches to Bunny Edge Storage")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)))
    parser.add_argument("--config", default=os.environ.get("BUNNY_VIDEO_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("BUNNY_SYNC_WORKERS", DEFAULT_WORKERS)))
    parser.add_argument("--watch", action="store_true", help="Continue watching the catalog for changes")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between completed passes in watch mode")
    parser.add_argument("--status", action="store_true", help="Print sanitized synchronization status")
    arguments = parser.parse_args()
    if Path(sys.argv[0]).name.startswith("hls-gallery-bunny-status") and len(sys.argv) == 1:
        arguments.status = True
    return arguments


def main():
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    if arguments.status:
        return print_status(root)
    config_path = Path(arguments.config).expanduser().resolve()
    workers = max(1, min(8, arguments.workers))
    status_path = root / "data" / "bunny-sync-status.json"
    while True:
        try:
            code = run_sync(root, config_path, workers)
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            write_status(status_path, "error", error=message[-1000:])
            print("[{}] Bunny sync error: {}".format(utc_iso(), message), file=sys.stderr, flush=True)
            code = 2
        if not arguments.watch:
            return code
        time.sleep(max(10, arguments.interval))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
