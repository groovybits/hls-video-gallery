#!/usr/bin/env python3
"""Manage an HLS Video Gallery from macOS, including direct Photos exports."""

import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid


APP_VERSION = "1.2.0"
CONFIG_PATH = Path(
    os.environ.get(
        "HLS_GALLERY_MANAGER_CONFIG",
        "~/Library/Application Support/HLS Video Gallery/manager.json",
    )
).expanduser()
VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".mxf", ".ogv", ".ts", ".webm", ".wmv",
}
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
REMOTE_ROOT_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")


PHOTOS_SCRIPT = r'''
function mediaRows(items) {
    var rows = [];
    items.forEach(function (item) {
        try {
            rows.push({id: String(item.id()), name: String(item.filename())});
        } catch (error) {
            // Skip incomplete Photos records and continue with usable videos.
        }
    });
    return rows;
}

function run(argv) {
    if (!argv.length) throw new Error("Missing Photos command");
    var commandName = argv[0];
    var Photos = Application("Photos");

    if (commandName === "albums") {
        return JSON.stringify(Photos.albums().map(function (album) {
            return {id: String(album.id()), name: String(album.name())};
        }));
    }

    if (commandName === "selection") {
        return JSON.stringify(mediaRows(Photos.selection()));
    }

    if (commandName === "album-items") {
        if (argv.length < 2) throw new Error("Missing album ID");
        var matches = Photos.albums.whose({id: argv[1]})();
        if (!matches.length) throw new Error("Photos album was not found");
        return JSON.stringify(mediaRows(matches[0].mediaItems()));
    }

    if (commandName === "export") {
        if (argv.length < 3) throw new Error("Missing export destination or media IDs");
        var exportItems = [];
        argv.slice(2).forEach(function (requestedId) {
            var matches = Photos.mediaItems.whose({id: requestedId})();
            if (matches.length) exportItems.push(matches[0]);
        });
        if (!exportItems.length) throw new Error("No matching Photos items were found");
        Photos.export(exportItems, {
            to: Path(argv[1]),
            usingOriginals: true
        });
        return String(exportItems.length);
    }

    throw new Error("Unknown Photos command: " + commandName);
}
'''


FINDER_PICK_SCRIPT = r'''
set chosenFiles to choose file with prompt "Choose videos to upload" with multiple selections allowed
set outputPaths to {}
repeat with chosenFile in chosenFiles
    set end of outputPaths to POSIX path of chosenFile
end repeat
set oldDelimiters to AppleScript's text item delimiters
set AppleScript's text item delimiters to ASCII character 30
set outputText to outputPaths as text
set AppleScript's text item delimiters to oldDelimiters
return outputText
'''


REMOTE_INVENTORY_CODE = r'''
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).expanduser().resolve()
media_root = (root / "media").resolve()
extensions = set(sys.argv[2].split(","))

def load(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, ValueError, TypeError):
        return default

catalog = load(root / "data" / "catalog.json", {})
order = load(root / "data" / "ingest-order.json", {})
progress = load(root / "data" / "encode-progress.json", {})
catalog_items = {
    str(item.get("source_relative")): item
    for item in catalog.get("items", [])
    if isinstance(item, dict) and item.get("source_relative")
}
order_items = order.get("items", {}) if isinstance(order, dict) else {}
if not isinstance(order_items, dict):
    order_items = {}

current_source = ""
source_data = progress.get("source")
if isinstance(source_data, dict):
    current_source = str(source_data.get("relative") or source_data.get("relative_path") or "")
elif isinstance(source_data, str):
    current_source = source_data

items = []
if media_root.is_dir():
    for path in media_root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in extensions:
                continue
            relative = path.relative_to(media_root).as_posix()
            file_stat = path.stat()
        except OSError:
            continue
        stable_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:18]
        ingest = order_items.get(stable_id, {})
        published = catalog_items.get(relative, {})
        if relative == current_source:
            state = "encoding"
        elif published:
            state = "ready"
        else:
            state = "queued"
        items.append({
            "source_relative": relative,
            "size_bytes": file_stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(
                file_stat.st_mtime, tz=dt.timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "upload_sequence": ingest.get("sequence") or published.get("upload_sequence") or 0,
            "uploaded_at": ingest.get("uploaded_at") or published.get("uploaded_at") or "",
            "state": state,
            "duration_seconds": published.get("duration_seconds") or 0,
            "title": published.get("title") or path.stem,
            "resolution": (
                published.get("video_streams", [{}])[0]
                if published.get("video_streams")
                else {}
            ),
            "cache_key": published.get("cache_key") or "",
            "processed_at": published.get("processed_at") or "",
        })

print(json.dumps({
    "root": str(root),
    "catalog_count": catalog.get("count", 0),
    "generated_at": catalog.get("generated_at"),
    "items": items,
    "progress": progress,
}, ensure_ascii=False))
'''


REMOTE_FINALIZE_CODE = r'''
import json
import os
from pathlib import Path
import sys

media_root = Path(sys.argv[1]).expanduser().resolve()
temporary = Path(sys.argv[2]).expanduser().resolve()
destination = Path(sys.argv[3]).expanduser().resolve()
replace = sys.argv[4] == "1"
temporary.relative_to(media_root)
destination.relative_to(media_root)
if not temporary.is_file():
    raise SystemExit("temporary upload is missing")
if destination.exists() and not replace:
    raise SystemExit("destination already exists")
os.replace(str(temporary), str(destination))
os.chmod(str(destination), 0o644)
print(json.dumps({"path": destination.name, "size_bytes": destination.stat().st_size}))
'''


REMOTE_DELETE_CODE = r'''
import json
from pathlib import Path
import sys

media_root = Path(sys.argv[1]).expanduser().resolve()
relative = sys.argv[2]
target = (media_root / relative).resolve()
target.relative_to(media_root)
if not target.is_file() or target.is_symlink():
    raise SystemExit("source video does not exist")
size = target.stat().st_size
target.unlink()
print(json.dumps({"deleted": relative, "size_bytes": size}))
'''


REMOTE_CLEANUP_CODE = r'''
from pathlib import Path
import sys

media_root = Path(sys.argv[1]).expanduser().resolve()
target = Path(sys.argv[2]).expanduser().resolve()
target.relative_to(media_root)
if target.is_file() and target.name.startswith(".gallery-upload-") and target.suffix == ".part":
    target.unlink()
'''


def eprint(*values):
    print(*values, file=sys.stderr)


def human_size(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "{:.0f} {}".format(size, unit) if unit == "B" else "{:.1f} {}".format(size, unit)
        size /= 1024


def human_duration(value):
    seconds = max(0, int(float(value or 0)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{}:{:02d}".format(minutes, seconds)


def display_date(value):
    if not value:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%b %d %H:%M")
    except ValueError:
        return str(value)[:15]


def ensure_video(path):
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def parse_photos_rows(output):
    try:
        rows = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("Photos returned an invalid response")
    if not isinstance(rows, list):
        raise RuntimeError("Photos returned an incomplete response")
    return [
        {"id": str(row["id"]), "name": str(row["name"])}
        for row in rows
        if isinstance(row, dict) and row.get("id") and row.get("name")
    ]


def run_command(command, capture=False, check=True, text=True):
    try:
        return subprocess.run(
            command,
            check=check,
            text=text,
            capture_output=capture,
        )
    except FileNotFoundError:
        raise RuntimeError("Required command is unavailable: {}".format(command[0]))
    except subprocess.CalledProcessError as error:
        detail = ""
        if capture:
            detail = (error.stderr or error.stdout or "").strip()
        if not detail:
            detail = "command exited with status {}".format(error.returncode)
        raise RuntimeError(detail)


def photos_command(*arguments):
    if sys.platform != "darwin":
        raise RuntimeError("Photos integration requires macOS")
    result = run_command(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", PHOTOS_SCRIPT, "--"] + list(arguments),
        capture=True,
    )
    return result.stdout


def photos_media(mode, album_id=""):
    arguments = [mode]
    if album_id:
        arguments.append(album_id)
    rows = parse_photos_rows(photos_command(*arguments))
    return [row for row in rows if Path(row["name"]).suffix.lower() in VIDEO_EXTENSIONS]


def export_photos_items(items, destination):
    if not items:
        return []
    before = {path.resolve() for path in destination.rglob("*") if path.is_file()}
    print("Photos is exporting {} original video{}.".format(
        len(items), "" if len(items) == 1 else "s"
    ))
    print("iCloud-backed originals may take a while to download; Photos controls this step.")
    for start in range(0, len(items), 100):
        chunk = items[start:start + 100]
        photos_command("export", str(destination), *[item["id"] for item in chunk])
        print("  Exported up to {} of {}".format(min(start + len(chunk), len(items)), len(items)))
    after = [path.resolve() for path in destination.rglob("*") if ensure_video(path)]
    exported = sorted((path for path in after if path not in before), key=lambda path: path.name.casefold())
    if not exported:
        raise RuntimeError("Photos completed without producing a supported video file")
    return exported


def validate_config(config):
    host = str(config.get("host") or "").strip()
    user = str(config.get("ssh_user") or "").strip()
    root = str(config.get("remote_root") or "").strip()
    identity = str(config.get("identity_file") or "").strip()
    if not HOST_PATTERN.fullmatch(host):
        raise ValueError("Host must be a hostname or IP address without a username")
    if not USER_PATTERN.fullmatch(user):
        raise ValueError("SSH user may contain letters, numbers, dots, underscores, and hyphens")
    if (
        not REMOTE_ROOT_PATTERN.fullmatch(root)
        or ".." in Path(root).parts
        or "//" in root
    ):
        raise ValueError(
            "Remote gallery root must be a simple absolute path using letters, "
            "numbers, dots, underscores, hyphens, and slashes"
        )
    if identity:
        identity_path = Path(identity).expanduser()
        if not identity_path.is_file():
            raise ValueError("SSH identity file does not exist: {}".format(identity_path))
        identity = str(identity_path)
    return {
        "schema_version": 1,
        "host": host,
        "ssh_user": user,
        "remote_root": root.rstrip("/"),
        "identity_file": identity,
    }


def save_config(config):
    validated = validate_config(config)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    temporary.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CONFIG_PATH)
    return validated


def load_config(required=True):
    try:
        return validate_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        if required:
            raise RuntimeError("Connection is not configured yet. Choose Configure connection first.")
        return {}
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Invalid manager configuration: {}".format(error))


def prompt(label, default=""):
    suffix = " [{}]".format(default) if default else ""
    value = input("{}{}: ".format(label, suffix)).strip()
    return value or default


def configure(arguments=None, interactive=True):
    existing = load_config(required=False)
    values = {
        "host": getattr(arguments, "host", "") or existing.get("host", ""),
        "ssh_user": getattr(arguments, "ssh_user", "") or existing.get("ssh_user", ""),
        "remote_root": getattr(arguments, "remote_root", "") or existing.get("remote_root", ""),
        "identity_file": getattr(arguments, "identity_file", "") or existing.get("identity_file", ""),
    }
    if interactive:
        print("\nGallery connection")
        values["host"] = prompt("Server hostname or IP", values["host"])
        values["ssh_user"] = prompt(
            "SSH user",
            values["ssh_user"] or getpass.getuser(),
        )
        values["remote_root"] = prompt(
            "Remote gallery root",
            values["remote_root"] or "/var/www/html/videos",
        )
        values["identity_file"] = prompt(
            "Optional SSH key path",
            values["identity_file"],
        )
    configured = save_config(values)
    print("Saved connection: {}@{}".format(configured["ssh_user"], configured["host"]))
    print("Gallery root: {}".format(configured["remote_root"]))
    return configured


def ssh_base(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    control_path = str(CONFIG_PATH.parent / "ssh-%C")
    command = [
        "ssh",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=10m",
        "-o", "ControlPath={}".format(control_path),
    ]
    if config.get("identity_file"):
        command.extend(["-i", config["identity_file"]])
    command.append("{}@{}".format(config["ssh_user"], config["host"]))
    return command


def scp_base(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    control_path = str(CONFIG_PATH.parent / "ssh-%C")
    command = [
        "scp",
        "-p",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=10m",
        "-o", "ControlPath={}".format(control_path),
    ]
    if config.get("identity_file"):
        command.extend(["-i", config["identity_file"]])
    return command


def remote_python(config, code, *arguments, capture=True, check=True):
    remote_command = "python3 -c {} {}".format(
        shlex.quote(code),
        " ".join(shlex.quote(str(argument)) for argument in arguments),
    ).rstrip()
    return run_command(
        ssh_base(config) + [remote_command],
        capture=capture,
        check=check,
    )


def test_connection(config):
    result = remote_python(
        config,
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "raise SystemExit(0 if (p/'.hls-video-gallery').is_file() or "
        "(p/'data/catalog.json').is_file() else 3)",
        config["remote_root"],
        capture=True,
    )
    return result.returncode == 0


def inventory(config):
    result = remote_python(
        config,
        REMOTE_INVENTORY_CODE,
        config["remote_root"],
        ",".join(sorted(VIDEO_EXTENSIONS)),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("The server returned an invalid inventory response")
    if not isinstance(payload.get("items"), list):
        raise RuntimeError("The server inventory is incomplete")
    return payload


def sort_inventory(items, sort_name):
    if sort_name == "upload-oldest":
        return sorted(items, key=lambda item: (
            int(item.get("upload_sequence") or 2**63 - 1),
            item["source_relative"].casefold(),
        ))
    if sort_name == "name":
        return sorted(items, key=lambda item: item["source_relative"].casefold())
    if sort_name == "state":
        return sorted(items, key=lambda item: (
            item.get("state", ""),
            -(int(item.get("upload_sequence") or 0)),
        ))
    return sorted(items, key=lambda item: (
        -(int(item.get("upload_sequence") or 0)),
        item["source_relative"].casefold(),
    ))


def print_inventory(
    payload,
    sort_name="upload-newest",
    page=1,
    page_size=25,
    show_all=False,
):
    items = sort_inventory(payload["items"], sort_name)
    page_size = max(1, int(page_size))
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(int(page), page_count))
    start = 0 if show_all else (page - 1) * page_size
    visible = items if show_all else items[start:start + page_size]
    page_text = "" if show_all else " · page {} of {}".format(page, page_count)
    print("\n{} source video{} · {} published{}".format(
        len(items),
        "" if len(items) == 1 else "s",
        payload.get("catalog_count", 0),
        page_text,
    ))
    print("{:>4}  {:<9}  {:<13}  {:>8}  {:>8}  {}".format(
        "#", "STATE", "UPLOADED", "LENGTH", "SIZE", "SOURCE"
    ))
    print("-" * min(110, shutil.get_terminal_size((110, 30)).columns))
    for index, item in enumerate(visible, start + 1):
        print("{:>4}  {:<9}  {:<13}  {:>8}  {:>8}  {}".format(
            index,
            item.get("state", "?")[:9],
            display_date(item.get("uploaded_at")),
            human_duration(item.get("duration_seconds")) if item.get("duration_seconds") else "—",
            human_size(item.get("size_bytes")),
            item["source_relative"],
        ))
    return items


def browse_inventory(payload, choose=False):
    page = 1
    page_size = 25
    page_count = max(1, (len(payload["items"]) + page_size - 1) // page_size)
    while True:
        items = print_inventory(payload, page=page, page_size=page_size)
        choices = []
        if page < page_count:
            choices.append("[n]ext")
        if page > 1:
            choices.append("[p]revious")
        if choose:
            choices.append("video number/name")
        choices.append("[q]uit")
        answer = input("{}: ".format(", ".join(choices))).strip()
        if answer.lower() == "n" and page < page_count:
            page += 1
        elif answer.lower() == "p" and page > 1:
            page -= 1
        elif answer.lower() == "q" or not answer:
            return None
        elif choose:
            return select_item(items, answer)
        else:
            print("Choose n, p, or q.")


def select_item(items, query):
    query = str(query or "").strip()
    if query.isdigit():
        index = int(query)
        if 1 <= index <= len(items):
            return items[index - 1]
    exact = [item for item in items if item["source_relative"].casefold() == query.casefold()]
    if len(exact) == 1:
        return exact[0]
    matches = [item for item in items if query.casefold() in item["source_relative"].casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("No source video matched {!r}".format(query))
    raise RuntimeError("More than one source matched; use its list number or full relative path")


def print_details(item):
    stream = item.get("resolution") or {}
    dimensions = ""
    if stream.get("width") and stream.get("height"):
        dimensions = "{}×{}".format(stream["width"], stream["height"])
    print("\n{}".format(item.get("title") or Path(item["source_relative"]).stem))
    rows = [
        ("Source", item["source_relative"]),
        ("State", item.get("state") or "unknown"),
        ("Upload order", item.get("upload_sequence") or "not indexed"),
        ("Uploaded", display_date(item.get("uploaded_at"))),
        ("Modified", display_date(item.get("modified_at"))),
        ("Duration", human_duration(item.get("duration_seconds")) if item.get("duration_seconds") else "not indexed"),
        ("Size", human_size(item.get("size_bytes"))),
        ("Resolution", dimensions or "not indexed"),
        ("Codec", stream.get("codec_name") or "not indexed"),
        ("Processed", display_date(item.get("processed_at"))),
    ]
    width = max(len(label) for label, _value in rows)
    for label, value in rows:
        print("  {:<{}}  {}".format(label + ":", width + 1, value))


def remote_media_root(config):
    return config["remote_root"] + "/media"


def upload_one(config, source, replace=False):
    media_root = remote_media_root(config)
    final_path = media_root + "/" + source.name
    temporary_path = media_root + "/.gallery-upload-{}.part".format(uuid.uuid4().hex)
    target = "{}@{}".format(config["ssh_user"], config["host"])
    destination = "{}:{}".format(target, temporary_path)
    try:
        run_command(scp_base(config) + [str(source), destination])
        result = remote_python(
            config,
            REMOTE_FINALIZE_CODE,
            media_root,
            temporary_path,
            final_path,
            "1" if replace else "0",
        )
        return json.loads(result.stdout)
    except Exception:
        remote_python(
            config,
            REMOTE_CLEANUP_CODE,
            media_root,
            temporary_path,
            capture=True,
            check=False,
        )
        raise


def upload_files(config, paths, replace=False):
    files = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if ensure_video(path):
            files.append(path)
        else:
            eprint("Skipping unsupported or missing file: {}".format(path))
    if not files:
        raise RuntimeError("No supported video files were selected")

    current = inventory(config)
    existing = {item["source_relative"] for item in current["items"]}
    uploaded = []
    for index, source in enumerate(files, 1):
        should_replace = replace
        if source.name in existing and not replace:
            answer = input(
                "{} already exists. [s]kip, [r]eplace, or [q]uit? [s]: ".format(source.name)
            ).strip().lower() or "s"
            if answer.startswith("q"):
                break
            if not answer.startswith("r"):
                print("Skipped {}".format(source.name))
                continue
            should_replace = True
        print("\nUploading {} of {}: {} ({})".format(
            index, len(files), source.name, human_size(source.stat().st_size)
        ))
        result = upload_one(config, source, replace=should_replace)
        uploaded.append(result)
        existing.add(source.name)
        print("Queued {} for gallery processing.".format(result["path"]))
    print("\nUploaded {} video{}.".format(len(uploaded), "" if len(uploaded) == 1 else "s"))
    return uploaded


def choose_photos_album():
    albums = parse_photos_rows(photos_command("albums"))
    if not albums:
        raise RuntimeError("Photos did not return any user albums")
    albums.sort(key=lambda album: album["name"].casefold())
    print("\nPhotos albums")
    for index, album in enumerate(albums, 1):
        print("{:>4}. {}".format(index, album["name"]))
    selected = prompt("Album number")
    if not selected.isdigit() or not 1 <= int(selected) <= len(albums):
        raise RuntimeError("Invalid album number")
    return albums[int(selected) - 1]


def upload_photos(config, mode, replace=False):
    if mode == "selection":
        items = photos_media("selection")
        label = "current Photos selection"
    else:
        album = choose_photos_album()
        items = photos_media("album-items", album["id"])
        label = "Photos album {!r}".format(album["name"])
    if not items:
        raise RuntimeError("{} contains no supported video items".format(label))
    print("\nFound {} video{} in {}.".format(
        len(items), "" if len(items) == 1 else "s", label
    ))
    with tempfile.TemporaryDirectory(prefix="hls-gallery-photos-") as temporary:
        exported = export_photos_items(items, Path(temporary))
        return upload_files(config, exported, replace=replace)


def choose_finder_files():
    if sys.platform != "darwin":
        raise RuntimeError("Finder selection requires macOS")
    result = run_command(
        ["/usr/bin/osascript", "-e", FINDER_PICK_SCRIPT],
        capture=True,
    )
    return [value for value in result.stdout.rstrip("\n").split(chr(30)) if value]


def delete_item(config, item, assume_yes=False):
    print_details(item)
    if not assume_yes:
        print("\nThis permanently deletes the source video from the server.")
        confirmation = input("Type DELETE to continue: ").strip()
        if confirmation != "DELETE":
            print("Deletion cancelled.")
            return False
    result = remote_python(
        config,
        REMOTE_DELETE_CODE,
        remote_media_root(config),
        item["source_relative"],
    )
    deleted = json.loads(result.stdout)
    print("Deleted {} ({}).".format(
        deleted["deleted"], human_size(deleted["size_bytes"])
    ))
    print("The next gallery scan will remove its listing and retired cache.")
    return True


def print_status(payload):
    progress = payload.get("progress") or {}
    queue = progress.get("queue") or {}
    state = progress.get("state") or progress.get("phase") or "idle"
    print("\nGallery status")
    print("  State:       {}".format(state))
    print("  Sources:     {}".format(len(payload.get("items", []))))
    print("  Published:   {}".format(payload.get("catalog_count", 0)))
    if queue:
        print("  Queue:       {} of {}".format(
            queue.get("position", 0), queue.get("total", 0)
        ))
        print("  Order:       {}".format(queue.get("order_label") or "oldest upload first"))
        if queue.get("predicted_finish_at"):
            print("  Finish:      {}".format(display_date(queue["predicted_finish_at"])))
    source = progress.get("source")
    if isinstance(source, dict):
        source = source.get("relative") or source.get("relative_path") or source.get("name")
    if source:
        print("  Current:     {}".format(source))
    if progress.get("fps"):
        print("  FFmpeg FPS:  {}".format(progress["fps"]))
    if progress.get("speed"):
        print("  Speed:       {}× real time".format(progress["speed"]))


def interactive_menu():
    while True:
        try:
            config = load_config(required=False)
            if config:
                connection = "{}@{}".format(config["ssh_user"], config["host"])
            else:
                connection = "not configured"
            print("\nHLS Gallery Manager {} · {}".format(APP_VERSION, connection))
            print("  1. Upload current Photos selection")
            print("  2. Upload a Photos album")
            print("  3. Choose video files in Finder")
            print("  4. Browse server collection")
            print("  5. View processing status")
            print("  6. View video details")
            print("  7. Delete a source video")
            print("  8. Configure connection")
            print("  0. Quit")
            choice = input("Choose an action: ").strip()
            if choice == "0":
                return
            if choice == "8":
                configure()
                continue
            config = load_config()
            if choice == "1":
                upload_photos(config, "selection")
            elif choice == "2":
                upload_photos(config, "album")
            elif choice == "3":
                upload_files(config, choose_finder_files())
            elif choice == "4":
                browse_inventory(inventory(config))
            elif choice == "5":
                print_status(inventory(config))
            elif choice in {"6", "7"}:
                selected = browse_inventory(inventory(config), choose=True)
                if not selected:
                    continue
                if choice == "6":
                    print_details(selected)
                else:
                    delete_item(config, selected)
            else:
                print("Unknown choice.")
        except (RuntimeError, ValueError, OSError) as error:
            eprint("\nError: {}".format(error))
            if "Not authorized" in str(error) or "-1743" in str(error):
                eprint(
                    "Allow Terminal to control Photos in System Settings → "
                    "Privacy & Security → Automation."
                )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=APP_VERSION)
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser("configure", help="save or change the gallery connection")
    setup.add_argument("--host", default="")
    setup.add_argument("--user", dest="ssh_user", default="")
    setup.add_argument("--remote-root", default="")
    setup.add_argument("--identity-file", default="")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--test", action="store_true")

    upload = commands.add_parser("upload", help="upload local video files")
    upload.add_argument("paths", nargs="+")
    upload.add_argument("--replace", action="store_true")

    selection = commands.add_parser(
        "photos-selection", help="export and upload the current Photos selection"
    )
    selection.add_argument("--replace", action="store_true")

    album = commands.add_parser(
        "photos-album", help="choose, export, and upload a Photos album"
    )
    album.add_argument("--replace", action="store_true")

    listing = commands.add_parser("list", help="list source videos and ingest state")
    listing.add_argument(
        "--sort",
        choices=["upload-newest", "upload-oldest", "name", "state"],
        default="upload-newest",
    )
    listing.add_argument("--page", type=int, default=1)
    listing.add_argument("--page-size", type=int, default=25)
    listing.add_argument("--all", action="store_true")

    details = commands.add_parser("details", help="show details for one source video")
    details.add_argument("query")

    delete = commands.add_parser("delete", help="permanently delete one source video")
    delete.add_argument("query")
    delete.add_argument("--yes", action="store_true", help="skip the DELETE confirmation")

    commands.add_parser("status", help="show scanner and encoder status")
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if not arguments.command:
        interactive_menu()
        return 0
    if arguments.command == "configure":
        config = configure(arguments, interactive=not arguments.non_interactive)
        if arguments.test:
            test_connection(config)
            print("Connection test passed.")
        return 0

    config = load_config()
    if arguments.command == "upload":
        upload_files(config, arguments.paths, replace=arguments.replace)
    elif arguments.command == "photos-selection":
        upload_photos(config, "selection", replace=arguments.replace)
    elif arguments.command == "photos-album":
        upload_photos(config, "album", replace=arguments.replace)
    elif arguments.command == "list":
        print_inventory(
            inventory(config),
            arguments.sort,
            page=arguments.page,
            page_size=arguments.page_size,
            show_all=arguments.all,
        )
    elif arguments.command in {"details", "delete"}:
        items = sort_inventory(inventory(config)["items"], "upload-newest")
        item = select_item(items, arguments.query)
        if arguments.command == "details":
            print_details(item)
        else:
            delete_item(config, item, assume_yes=arguments.yes)
    elif arguments.command == "status":
        print_status(inventory(config))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("\nCancelled.")
        raise SystemExit(130)
    except (RuntimeError, ValueError, OSError) as error:
        eprint("Error: {}".format(error))
        raise SystemExit(1)
