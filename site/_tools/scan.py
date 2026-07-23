#!/usr/bin/env python3
"""Incrementally build thumbnails, HLS renditions, and a public video catalog."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import quote


APP_VERSION = "1.4.1"
CACHE_VERSION = 6
HLS_PRESET = "superfast"
ALLOWED_HLS_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
DEFAULT_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".mxf", ".ogv", ".ts", ".webm", ".wmv",
}

RENDITION_PROFILES = (
    {"height": 360, "video_bitrate": 1_000_000, "audio_bitrate": 96_000},
    {"height": 480, "video_bitrate": 1_800_000, "audio_bitrate": 112_000},
    {"height": 720, "video_bitrate": 3_600_000, "audio_bitrate": 128_000},
    {"height": 1080, "video_bitrate": 6_500_000, "audio_bitrate": 160_000},
)


class MediaError(RuntimeError):
    pass


def log(message):
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(timestamp, message), flush=True)


def utc_iso(timestamp=None):
    if timestamp is None:
        timestamp = time.time()
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def fraction(value):
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return number(value)


def canonical_hash(value, length=16):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def public_path(*parts):
    return "/".join(quote(str(part), safe="") for part in parts)


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return fallback


def run(command, description):
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode:
        stderr = (process.stderr or "").strip()
        tail = "\n".join(stderr.splitlines()[-18:])
        raise MediaError("{} failed (exit {}):\n{}".format(description, process.returncode, tail))
    return process.stdout


def ffprobe_json(ffprobe, media_path):
    output = run([
        ffprobe,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(media_path),
    ], "ffprobe for {}".format(media_path.name))
    try:
        return json.loads(output)
    except ValueError as error:
        raise MediaError("ffprobe returned invalid JSON for {}: {}".format(media_path.name, error))


def packet_timeline_duration(ffprobe, media_path, video_stream_index):
    """Recover duration from packet timestamps when an unfinished container has none.

    Browser/MediaRecorder WebMs are often playable but lack the closing segment
    metadata that supplies format.duration.  Stream ffprobe output line by line
    so this fallback remains memory-bounded even for long recordings.
    """
    command = [
        ffprobe,
        "-v", "warning",
        "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time",
        "-of", "compact=p=0:nk=0",
        str(media_path),
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    maximum_end = 0.0
    for raw_line in process.stdout or []:
        fields = {}
        for portion in raw_line.strip().split("|"):
            key, separator, value = portion.partition("=")
            if separator:
                fields[key] = value
        if integer(fields.get("stream_index"), -1) != integer(video_stream_index, -2):
            continue
        timestamp = max(number(fields.get("pts_time"), -1.0), number(fields.get("dts_time"), -1.0))
        if timestamp < 0:
            continue
        maximum_end = max(maximum_end, timestamp + max(0.0, number(fields.get("duration_time"))))
    return_code = process.wait()
    if return_code and maximum_end <= 0:
        raise MediaError("Packet timeline probe failed for {} (exit {})".format(media_path.name, return_code))
    return maximum_end


def embedded_creation_time(probe):
    """Return the source's embedded creation timestamp without inventing one."""
    tag_sets = [(probe.get("format") or {}).get("tags") or {}]
    tag_sets.extend((stream.get("tags") or {}) for stream in (probe.get("streams") or []))
    preferred_keys = ("creation_time", "com.apple.quicktime.creationdate", "date")
    for tags in tag_sets:
        normalized = {str(key).casefold(): value for key, value in tags.items()}
        for key in preferred_keys:
            value = str(normalized.get(key) or "").strip()
            if value:
                return value
    return None


def add_cached_creation_time(item, source, ffprobe, cache_root):
    """Backfill creation metadata without rebuilding thumbnails or HLS."""
    if "creation_at" in item:
        return item, False
    probe = ffprobe_json(ffprobe, source)
    enriched = dict(item)
    enriched["creation_at"] = embedded_creation_time(probe)
    cache_key = str(enriched.get("cache_key") or "")
    if cache_key and Path(cache_key).name == cache_key:
        atomic_write_json(cache_root / cache_key / "metadata.json", enriched)
    return enriched, True


def stream_rotation(stream):
    tags = stream.get("tags") or {}
    if tags.get("rotate") is not None:
        return integer(tags.get("rotate")) % 360
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            return integer(side_data.get("rotation")) % 360
    return 0


def displayed_dimensions(stream):
    width = integer(stream.get("width"))
    height = integer(stream.get("height"))
    if stream_rotation(stream) in {90, 270}:
        width, height = height, width
    return width, height


def clean_tags(stream):
    tags = stream.get("tags") or {}
    allowed = {}
    for key in ("language", "title", "handler_name"):
        if tags.get(key):
            allowed[key] = str(tags[key])
    return allowed


def clean_stream(stream):
    codec_type = stream.get("codec_type") or "unknown"
    tags = clean_tags(stream)
    details = {
        "index": integer(stream.get("index")),
        "codec_type": codec_type,
        "codec_name": stream.get("codec_name") or "unknown",
        "codec_long_name": stream.get("codec_long_name") or "",
        "profile": stream.get("profile") or "",
        "bit_rate": integer(stream.get("bit_rate")),
        "duration_seconds": number(stream.get("duration")),
        "language": tags.get("language", "und"),
        "title": tags.get("title") or tags.get("handler_name") or "",
        "default": bool((stream.get("disposition") or {}).get("default")),
    }
    if codec_type == "video":
        width, height = displayed_dimensions(stream)
        details.update({
            "width": width,
            "height": height,
            "coded_width": integer(stream.get("coded_width")),
            "coded_height": integer(stream.get("coded_height")),
            "frame_rate": fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
            "pixel_format": stream.get("pix_fmt") or "",
            "color_space": stream.get("color_space") or "",
            "color_transfer": stream.get("color_transfer") or "",
            "color_primaries": stream.get("color_primaries") or "",
            "field_order": stream.get("field_order") or "",
            "display_aspect_ratio": stream.get("display_aspect_ratio") or "",
            "rotation": stream_rotation(stream),
            "attached_pic": bool((stream.get("disposition") or {}).get("attached_pic")),
        })
    elif codec_type == "audio":
        details.update({
            "sample_rate": integer(stream.get("sample_rate")),
            "channels": integer(stream.get("channels")),
            "channel_layout": stream.get("channel_layout") or "",
            "sample_format": stream.get("sample_fmt") or "",
        })
    return details


def primary_video_stream(streams):
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")]
    if not video_streams:
        return None
    return sorted(video_streams, key=lambda stream: (not bool((stream.get("disposition") or {}).get("default")), -integer(stream.get("width")) * integer(stream.get("height"))))[0]


def cached_primary_video_stream(video_streams):
    """Reproduce primary_video_stream() from cached, cleaned stream metadata."""
    candidates = [
        stream for stream in (video_streams or [])
        if isinstance(stream, dict) and not bool(stream.get("attached_pic"))
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda stream: (
        not bool(stream.get("default")),
        -integer(stream.get("width")) * integer(stream.get("height")),
    ))[0]


def add_cached_primary_video_stream_index(item, cache_root):
    """Backfill the encoder-selected global stream index without rebuilding media."""
    if integer(item.get("primary_video_stream_index"), -1) >= 0:
        return item, False
    selected = cached_primary_video_stream(item.get("video_streams"))
    selected_index = integer((selected or {}).get("index"), -1)
    if selected_index < 0:
        return item, False
    enriched = dict(item)
    enriched["primary_video_stream_index"] = selected_index
    cache_key = str(enriched.get("cache_key") or "")
    if cache_key and Path(cache_key).name == cache_key:
        atomic_write_json(cache_root / cache_key / "metadata.json", enriched)
    return enriched, True


def primary_audio_stream(streams):
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio_streams:
        return None
    return sorted(audio_streams, key=lambda stream: (not bool((stream.get("disposition") or {}).get("default")), integer(stream.get("index"))))[0]


def choose_rendition(source_height, target_height, max_video_bitrate=0, max_audio_bitrate=0):
    """Return one output rendition, capped at target height without upscaling."""
    height = min(max(2, int(source_height or 240)), max(2, int(target_height)))
    if height % 2:
        height -= 1
    profile = next((item for item in RENDITION_PROFILES if height <= item["height"]), RENDITION_PROFILES[-1])
    bitrate = min(profile["video_bitrate"], max_video_bitrate) if max_video_bitrate > 0 else profile["video_bitrate"]
    audio_bitrate = min(profile["audio_bitrate"], max_audio_bitrate) if max_audio_bitrate > 0 else profile["audio_bitrate"]
    return {
        "name": "{}p".format(height),
        "height": height,
        "video_bitrate": bitrate,
        "maxrate": "{}k".format(math.ceil(bitrate * 1.08 / 1000)),
        "bufsize": "{}k".format(math.ceil(bitrate * 2.0 / 1000)),
        "audio_bitrate": audio_bitrate,
    }


def generate_thumbnails(ffmpeg, source, output_dir, video_stream, interval, width, duration, max_thumbnails):
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_pattern = output_dir / "thumb-%06d.jpg"
    video_filter = "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{})',scale=w='min({},iw)':h=-2:force_original_aspect_ratio=decrease,setsar=1".format(interval, width)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:{}".format(integer(video_stream.get("index"))),
        "-an", "-sn", "-dn",
        "-vf", video_filter,
        "-fps_mode", "vfr",
        "-q:v", "4",
        "-start_number", "0",
    ]
    if max_thumbnails > 0:
        command.extend(["-frames:v", str(max_thumbnails)])
    command.append(str(frame_pattern))
    run(command, "thumbnail generation for {}".format(source.name))

    images = sorted(output_dir.glob("thumb-*.jpg"))
    if not images:
        fallback = output_dir / "thumb-000000.jpg"
        seek_time = min(1.0, max(0.0, duration / 10.0))
        run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "{:.3f}".format(seek_time),
            "-i", str(source),
            "-map", "0:{}".format(integer(video_stream.get("index"))),
            "-frames:v", "1", "-an", "-sn", "-dn",
            "-vf", "scale=w='min({},iw)':h=-2:force_original_aspect_ratio=decrease,setsar=1".format(width),
            "-q:v", "4", str(fallback),
        ], "fallback thumbnail generation for {}".format(source.name))
        images = [fallback]
    return images


def probe_segment_dimensions(ffprobe, playlist):
    try:
        probe = ffprobe_json(ffprobe, playlist)
        stream = primary_video_stream(probe.get("streams") or [])
        if stream:
            return displayed_dimensions(stream)
    except MediaError:
        pass
    return 0, 0


def generate_hls(ffmpeg, ffprobe, source, output_dir, video_stream, audio_stream, source_width, source_height, frame_rate, segment_seconds, target_height, preset):
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = []
    renditions = [choose_rendition(source_height, target_height)]
    output_frame_rate = min(frame_rate, 30.0) if frame_rate > 0 else 30.0
    gop = max(24, int(round(output_frame_rate * segment_seconds)))
    field_order = str(video_stream.get("field_order") or "").strip().lower()
    is_interlaced = field_order not in {"", "unknown", "progressive"}

    for rendition in renditions:
        variant_dir = output_dir / rendition["name"]
        variant_dir.mkdir(parents=True, exist_ok=True)
        playlist = variant_dir / "index.m3u8"
        segment_pattern = variant_dir / "seg-%06d.ts"

        filters = []
        if is_interlaced:
            filters.append("yadif=deint=interlaced")
        if frame_rate > 30.5:
            filters.append("fps=30")
        filters.extend([
            "scale=w=-2:h={}:force_original_aspect_ratio=decrease:force_divisible_by=2".format(rendition["height"]),
            "setsar=1",
        ])

        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts",
            "-i", str(source),
            "-map", "0:{}".format(integer(video_stream.get("index"))),
        ]
        if audio_stream:
            command.extend(["-map", "0:{}".format(integer(audio_stream.get("index")))])
        command.extend([
            "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", preset,
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-b:v", str(rendition["video_bitrate"]),
            "-maxrate", rendition["maxrate"],
            "-bufsize", rendition["bufsize"],
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*{})".format(segment_seconds),
        ])
        if audio_stream:
            command.extend([
                "-c:a", "aac",
                "-b:a", str(rendition["audio_bitrate"]),
                "-ac", "2",
                "-ar", "48000",
            ])
        command.extend([
            "-max_muxing_queue_size", "2048",
            "-avoid_negative_ts", "make_zero",
            "-f", "hls",
            "-hls_time", str(segment_seconds),
            "-hls_playlist_type", "vod",
            "-hls_segment_type", "mpegts",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(segment_pattern),
            str(playlist),
        ])
        started = time.monotonic()
        log("ENCODE {} -> {} using x264 preset {}{}".format(
            source.name,
            rendition["name"],
            preset,
            " with deinterlacing" if is_interlaced else "",
        ))
        run(command, "{} HLS rendition for {}".format(rendition["name"], source.name))
        log("ENCODED {} -> {} in {:.1f}s".format(source.name, rendition["name"], time.monotonic() - started))

        width, height = probe_segment_dimensions(ffprobe, playlist)
        if not width or not height:
            height = rendition["height"]
            width = int(round((source_width / float(source_height or 1)) * height))
            if width % 2:
                width += 1
        audio_bitrate = rendition["audio_bitrate"] if audio_stream else 0
        maxrate = integer(str(rendition["maxrate"]).rstrip("k")) * 1000
        bandwidth = maxrate + audio_bitrate
        variants.append({
            "name": rendition["name"],
            "width": width,
            "height": height,
            "frame_rate": round(output_frame_rate, 3),
            "video_bitrate": rendition["video_bitrate"],
            "audio_bitrate": audio_bitrate,
            "bandwidth": bandwidth,
            "playlist": "{}/index.m3u8".format(rendition["name"]),
        })

    master = output_dir / "master.m3u8"
    with master.open("w", encoding="ascii", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        handle.write("#EXT-X-VERSION:3\n")
        handle.write("#EXT-X-INDEPENDENT-SEGMENTS\n")
        for variant in variants:
            handle.write("#EXT-X-STREAM-INF:BANDWIDTH={},AVERAGE-BANDWIDTH={},RESOLUTION={}x{},FRAME-RATE={:.3f}\n".format(
                variant["bandwidth"],
                variant["video_bitrate"] + variant["audio_bitrate"],
                variant["width"],
                variant["height"],
                variant["frame_rate"],
            ))
            handle.write(variant["playlist"] + "\n")
    os.chmod(master, 0o644)
    return variants


def generate_media_outputs(ffmpeg, ffprobe, source, output_dir, video_stream, audio_stream,
                           source_width, source_height, frame_rate, segment_seconds,
                           target_height, preset, thumbnail_interval, thumbnail_width,
                           max_thumbnails, max_video_bitrate=0, max_audio_bitrate=0):
    """Decode once, then split frames to the HLS encoder and JPEG timeline."""
    hls_dir = output_dir / "hls"
    thumbnail_dir = output_dir / "thumbs"
    hls_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    rendition = choose_rendition(
        source_height, target_height, max_video_bitrate, max_audio_bitrate,
    )
    output_frame_rate = min(frame_rate, 30.0) if frame_rate > 0 else 30.0
    gop = max(24, int(round(output_frame_rate * segment_seconds)))
    field_order = str(video_stream.get("field_order") or "").strip().lower()
    is_interlaced = field_order not in {"", "unknown", "progressive"}
    variant_dir = hls_dir / rendition["name"]
    variant_dir.mkdir(parents=True, exist_ok=True)
    playlist = variant_dir / "index.m3u8"
    segment_pattern = variant_dir / "seg-%06d.ts"
    frame_pattern = thumbnail_dir / "thumb-%06d.jpg"

    shared_filters = []
    if is_interlaced:
        shared_filters.append("yadif=deint=interlaced")
    if frame_rate > 30.5:
        shared_filters.append("fps=30")
    shared_filters.append("split=2[hlsin][thumbin]")
    filter_graph = [
        "[0:{}]{}".format(integer(video_stream.get("index")), ",".join(shared_filters)),
        "[hlsin]scale=w=-2:h={}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[hlsv]".format(rendition["height"]),
        "[thumbin]select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{})',scale=w='min({},iw)':h=-2:force_original_aspect_ratio=decrease,setsar=1[thumbv]".format(
            thumbnail_interval, thumbnail_width,
        ),
    ]

    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts",
        "-i", str(source),
        "-filter_complex", ";".join(filter_graph),
        "-map", "[hlsv]",
    ]
    if audio_stream:
        command.extend(["-map", "0:{}".format(integer(audio_stream.get("index")))])
    command.extend([
        "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-c:v", "libx264",
        "-preset", preset,
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-b:v", str(rendition["video_bitrate"]),
        "-maxrate", rendition["maxrate"],
        "-bufsize", rendition["bufsize"],
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-force_key_frames", "expr:gte(t,n_forced*{})".format(segment_seconds),
    ])
    if audio_stream:
        command.extend([
            "-c:a", "aac",
            "-b:a", str(rendition["audio_bitrate"]),
            "-ac", "2",
            "-ar", "48000",
        ])
    command.extend([
        "-max_muxing_queue_size", "2048",
        "-avoid_negative_ts", "make_zero",
        "-f", "hls",
        "-hls_time", str(segment_seconds),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(segment_pattern),
        str(playlist),
        "-map", "[thumbv]",
        "-map_metadata", "-1", "-map_chapters", "-1", "-an", "-sn", "-dn",
        "-c:v", "mjpeg",
        "-q:v", "4",
        "-fps_mode", "vfr",
        "-start_number", "0",
    ])
    if max_thumbnails > 0:
        command.extend(["-frames:v", str(max_thumbnails)])
    command.append(str(frame_pattern))

    started = time.monotonic()
    log("ENCODE {} -> {} HLS + {}s thumbnails in one decode using x264 preset {}{}".format(
        source.name,
        rendition["name"],
        thumbnail_interval,
        preset,
        " with deinterlacing" if is_interlaced else "",
    ))
    run(command, "combined HLS and thumbnail generation for {}".format(source.name))
    log("ENCODED {} -> {} HLS + thumbnails in {:.1f}s".format(
        source.name, rendition["name"], time.monotonic() - started,
    ))

    images = sorted(thumbnail_dir.glob("thumb-*.jpg"))
    if not images:
        raise MediaError("Combined encode produced no thumbnails for {}".format(source.name))

    width, height = probe_segment_dimensions(ffprobe, playlist)
    if not width or not height:
        height = rendition["height"]
        width = int(round((source_width / float(source_height or 1)) * height))
        if width % 2:
            width += 1
    audio_bitrate = rendition["audio_bitrate"] if audio_stream else 0
    maxrate = integer(str(rendition["maxrate"]).rstrip("k")) * 1000
    variants = [{
        "name": rendition["name"],
        "width": width,
        "height": height,
        "frame_rate": round(output_frame_rate, 3),
        "video_bitrate": rendition["video_bitrate"],
        "audio_bitrate": audio_bitrate,
        "bandwidth": maxrate + audio_bitrate,
        "playlist": "{}/index.m3u8".format(rendition["name"]),
    }]

    master = hls_dir / "master.m3u8"
    with master.open("w", encoding="ascii", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        handle.write("#EXT-X-VERSION:3\n")
        handle.write("#EXT-X-INDEPENDENT-SEGMENTS\n")
        for variant in variants:
            handle.write("#EXT-X-STREAM-INF:BANDWIDTH={},AVERAGE-BANDWIDTH={},RESOLUTION={}x{},FRAME-RATE={:.3f}\n".format(
                variant["bandwidth"],
                variant["video_bitrate"] + variant["audio_bitrate"],
                variant["width"],
                variant["height"],
                variant["frame_rate"],
            ))
            handle.write(variant["playlist"] + "\n")
    os.chmod(master, 0o644)
    return images, variants


def source_signature(relative, stat_result):
    """Identify a source only by filesystem facts that change with the file."""
    return {
        "relative_path": relative,
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
    }


def swap_directory(temporary, final):
    backup = final.parent / (".old-" + final.name + "-" + canonical_hash(time.time(), 8))
    moved_old = False
    try:
        if final.exists():
            os.replace(str(final), str(backup))
            moved_old = True
        os.replace(str(temporary), str(final))
        if moved_old:
            shutil.rmtree(str(backup), ignore_errors=True)
    except Exception:
        if moved_old and not final.exists() and backup.exists():
            os.replace(str(backup), str(final))
        raise


def publish_cache_tree(root):
    """Make generated HLS and thumbnail output traversable/readable by Apache."""
    for directory, child_directories, filenames in os.walk(str(root), followlinks=False):
        os.chmod(directory, 0o755)
        for child_directory in child_directories:
            child_path = os.path.join(directory, child_directory)
            if not os.path.islink(child_path):
                os.chmod(child_path, 0o755)
        for filename in filenames:
            file_path = os.path.join(directory, filename)
            if not os.path.islink(file_path):
                os.chmod(file_path, 0o644)


def repair_completed_cache_roots(cache_root):
    """Repair cache roots created by 1.0.0, whose tempfile mode was 0700."""
    repaired = 0
    for entry in cache_root.iterdir():
        if not entry.is_dir() or "--" not in entry.name or entry.name.startswith("."):
            continue
        try:
            mode = entry.stat().st_mode & 0o777
            if mode != 0o755:
                os.chmod(str(entry), 0o755)
                repaired += 1
        except OSError:
            continue
    return repaired


def active_build_directories(cache_root):
    """Return temporary cache directory names still referenced by a live FFmpeg."""
    active = set()
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return active
    cache_prefix = str(cache_root) + os.sep
    for process in proc_root.glob("[0-9]*"):
        try:
            payload = (process / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = [part.decode("utf-8", "replace") for part in payload.split(b"\0") if part]
        if not arguments or os.path.basename(arguments[0]) != "ffmpeg":
            continue
        for argument in arguments:
            if not argument.startswith(cache_prefix):
                continue
            relative = argument[len(cache_prefix):]
            name = relative.split(os.sep, 1)[0]
            if name.startswith(".building-"):
                active.add(name)
    return active


def clean_abandoned_workdirs(cache_root):
    """Remove interrupted temporary builds before a new locked scan begins."""
    active = active_build_directories(cache_root)
    removed = 0
    for entry in cache_root.iterdir():
        if not entry.is_dir() or entry.name in active:
            continue
        if entry.name.startswith(".building-") or entry.name.startswith(".old-"):
            shutil.rmtree(str(entry), ignore_errors=True)
            if not entry.exists():
                removed += 1
    return removed


def reusable_item_from_directory(cache_dir, relative, stat_result):
    """Validate a completed current-pipeline cache against its source file."""
    cache_key = cache_dir.name
    if not cache_key or cache_key.startswith(".") or Path(cache_key).name != cache_key:
        return None
    state = load_json(cache_dir / "state.json", None)
    if not isinstance(state, dict):
        return None
    if (
        state.get("relative_path") != relative
        or integer(state.get("size"), -1) != stat_result.st_size
        or integer(state.get("mtime_ns"), -1) != stat_result.st_mtime_ns
    ):
        return None
    metadata = load_json(cache_dir / "metadata.json", None)
    if (
        not isinstance(metadata, dict)
        or metadata.get("cache_key") != cache_key
        or integer(metadata.get("cache_version"), -1) != CACHE_VERSION
        or len(metadata.get("hls_variants") or []) != 1
    ):
        return None
    if not (cache_dir / "hls" / "master.m3u8").is_file() or not (cache_dir / "thumbs").is_dir():
        return None
    return metadata


def reusable_cached_item(previous, stable_id, relative, stat_result, cache_root):
    """Reuse cataloged or orphaned completed output while the source is unchanged."""
    checked = set()
    if isinstance(previous, dict):
        cache_key = str(previous.get("cache_key") or "")
        if cache_key:
            checked.add(cache_key)
            cached = reusable_item_from_directory(cache_root / cache_key, relative, stat_result)
            if cached:
                return cached

    candidates = []
    for cache_dir in cache_root.glob(stable_id + "--*"):
        if cache_dir.name in checked or cache_dir.is_symlink() or not cache_dir.is_dir():
            continue
        try:
            candidates.append((cache_dir.stat().st_mtime_ns, cache_dir))
        except OSError:
            continue
    for _modified, cache_dir in sorted(candidates, reverse=True):
        cached = reusable_item_from_directory(cache_dir, relative, stat_result)
        if cached:
            return cached
    return None


def process_video(path, relative, stat_result, settings, cache_root, ffmpeg, ffprobe, previous=None, force=False):
    stable_id = source_id(relative)
    signature = source_signature(relative, stat_result)
    version = canonical_hash(signature, 14)
    cache_key = "{}--{}".format(stable_id, version)
    final_dir = cache_root / cache_key
    metadata_path = final_dir / "metadata.json"

    if not force:
        cached = reusable_cached_item(previous, stable_id, relative, stat_result, cache_root)
        if cached:
            return cached, False
        if metadata_path.is_file():
            cached = load_json(metadata_path, None)
            if cached and cached.get("version") == version:
                return cached, False

    probe = ffprobe_json(ffprobe, path)
    streams = probe.get("streams") or []
    video_stream = primary_video_stream(streams)
    if not video_stream:
        raise MediaError("No usable video stream was found")
    audio_stream = primary_audio_stream(streams)
    format_info = probe.get("format") or {}

    source_width, source_height = displayed_dimensions(video_stream)
    if source_width <= 0 or source_height <= 0:
        raise MediaError("The primary video stream has invalid dimensions")
    frame_rate = fraction(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    duration_source = "container"
    duration = number(format_info.get("duration"))
    if duration <= 0:
        duration = max([number(stream.get("duration")) for stream in streams] + [0.0])
        duration_source = "stream"
    if duration <= 0:
        log("PROBE {} has no embedded duration; scanning packet timestamps".format(relative))
        duration = packet_timeline_duration(ffprobe, path, video_stream.get("index"))
        duration_source = "packet_timestamps"
    if duration <= 0:
        raise MediaError("The video duration could not be determined from metadata or packet timestamps")

    build_dir = Path(tempfile.mkdtemp(prefix=".building-{}-".format(stable_id), dir=str(cache_root)))
    try:
        thumbnail_files, variants = generate_media_outputs(
            ffmpeg, ffprobe, path, build_dir, video_stream, audio_stream,
            source_width, source_height, frame_rate, settings["hls_segment_seconds"],
            settings["hls_target_height"], settings["hls_preset"],
            settings["thumbnail_interval"], settings["thumbnail_width"],
            settings["max_thumbnails"], settings["video_bitrate"], settings["audio_bitrate"],
        )

        final_stat = path.stat()
        if final_stat.st_size != stat_result.st_size or final_stat.st_mtime_ns != stat_result.st_mtime_ns:
            raise MediaError("The source file changed while it was being processed; it will be retried")

        cleaned_streams = [clean_stream(stream) for stream in streams]
        thumbnails = []
        for index, image in enumerate(thumbnail_files):
            thumbnails.append({
                "time_seconds": min(float(index * settings["thumbnail_interval"]), duration),
                "url": public_path("cache", cache_key, "thumbs", image.name),
            })
        modified_at = utc_iso(stat_result.st_mtime)
        item = {
            "cache_version": CACHE_VERSION,
            "id": stable_id,
            "version": version,
            "cache_key": cache_key,
            "title": path.stem,
            "source_relative": relative,
            "size_bytes": stat_result.st_size,
            "modified_at": modified_at,
            "creation_at": embedded_creation_time(probe),
            "duration_seconds": round(duration, 3),
            "duration_source": duration_source,
            "bit_rate": integer(format_info.get("bit_rate")),
            "format_name": format_info.get("format_name") or "",
            "format_long_name": format_info.get("format_long_name") or "",
            "primary_video_stream_index": integer(video_stream.get("index")),
            "video_streams": [stream for stream in cleaned_streams if stream["codec_type"] == "video"],
            "audio_streams": [stream for stream in cleaned_streams if stream["codec_type"] == "audio"],
            "subtitle_streams": [stream for stream in cleaned_streams if stream["codec_type"] == "subtitle"],
            "poster_url": thumbnails[0]["url"],
            "thumbnails": thumbnails,
            "hls_url": public_path("cache", cache_key, "hls", "master.m3u8"),
            "hls_variants": variants,
            "processed_at": utc_iso(),
        }
        atomic_write_json(build_dir / "metadata.json", item)
        atomic_write_json(build_dir / "state.json", signature)
        publish_cache_tree(build_dir)
        swap_directory(build_dir, final_dir)
        return item, True
    except Exception:
        shutil.rmtree(str(build_dir), ignore_errors=True)
        raise


def discover_videos(media_root, extensions):
    videos = []
    if not media_root.exists():
        return videos
    for path in media_root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        relative_parts = path.relative_to(media_root).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        if path.suffix.lower() in extensions:
            videos.append(path)
    return sorted(videos, key=lambda value: str(value).casefold())


def source_id(relative):
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:18]


def update_ingest_order(order_path, videos, media_root):
    """Persist first-seen upload order independently from filenames and source edits."""
    existed = order_path.is_file()
    payload = load_json(order_path, {})
    stored = payload.get("items", {}) if isinstance(payload, dict) else {}
    if not isinstance(stored, dict):
        stored = {}

    records = {}
    pending = []
    present_ids = set()
    highest_sequence = 0
    changed = not existed or payload.get("schema_version") != 1
    observed_epoch = time.time()

    for path in videos:
        relative = path.relative_to(media_root).as_posix()
        stable_id = source_id(relative)
        present_ids.add(stable_id)
        record = stored.get(stable_id)
        sequence = integer(record.get("sequence")) if isinstance(record, dict) else 0
        if sequence > 0 and record.get("relative_path") == relative:
            records[stable_id] = {
                "relative_path": relative,
                "sequence": sequence,
                "uploaded_at": str(record.get("uploaded_at") or utc_iso(observed_epoch)),
            }
            highest_sequence = max(highest_sequence, sequence)
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        pending.append((stat_result.st_mtime_ns, relative.casefold(), stable_id, relative, stat_result.st_mtime))

    next_sequence = max(integer(payload.get("next_sequence"), 1), highest_sequence + 1)
    bootstrap_existing_library = not existed
    for _mtime_ns, _relative_key, stable_id, relative, modified_epoch in sorted(pending):
        records[stable_id] = {
            "relative_path": relative,
            "sequence": next_sequence,
            "uploaded_at": utc_iso(modified_epoch if bootstrap_existing_library else observed_epoch),
        }
        next_sequence += 1
        changed = True

    if set(stored) != present_ids:
        changed = True

    output = {
        "schema_version": 1,
        "updated_at": utc_iso(),
        "next_sequence": next_sequence,
        "items": records,
    }
    if changed or payload.get("next_sequence") != next_sequence:
        atomic_write_json(order_path, output)
    return records


def order_videos_by_upload(videos, media_root, records):
    def upload_key(path):
        relative = path.relative_to(media_root).as_posix()
        record = records.get(source_id(relative)) or {}
        return (
            integer(record.get("sequence"), 2**63 - 1),
            relative.casefold(),
        )
    return sorted(videos, key=upload_key)


def apply_upload_metadata(item, record):
    if not isinstance(item, dict) or not isinstance(record, dict):
        return item
    sequence = integer(record.get("sequence"))
    uploaded_at = str(record.get("uploaded_at") or "")
    if sequence > 0:
        item["upload_sequence"] = sequence
    if uploaded_at:
        item["uploaded_at"] = uploaded_at
    return item


def clean_cache(cache_root, active_keys, retention_seconds):
    now = time.time()
    removed = 0
    for entry in cache_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".building-") or entry.name.startswith(".old-"):
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > 3600:
                shutil.rmtree(str(entry), ignore_errors=True)
                removed += 1
            continue
        if entry.name in active_keys:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age >= retention_seconds:
            shutil.rmtree(str(entry), ignore_errors=True)
            removed += 1
    return removed


def command_exists(value):
    if os.path.sep in value:
        return os.path.isfile(value) and os.access(value, os.X_OK)
    return shutil.which(value) is not None


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Build the cached video catalog and HLS streams.")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)), help="Video application root")
    parser.add_argument("--force", action="store_true", help="Rebuild every video even if its signature is unchanged")
    parser.add_argument("--wait-for-lock", action="store_true", help="Wait for another scan to finish instead of exiting")
    parser.add_argument("--verbose", action="store_true", help="Print cached video entries as they are checked")
    return parser.parse_args()


def publish_catalog(catalog_path, item_map, settings, source_count, processed_count, cached_count, skipped_count, error_count):
    """Publish every currently ready item without waiting for the whole queue."""
    items = sorted(
        item_map.values(),
        key=lambda item: (str(item.get("title", "")).casefold(), str(item.get("source_relative", "")).casefold()),
    )
    catalog = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "generated_at": utc_iso(),
        "thumbnail_interval_seconds": settings["thumbnail_interval"],
        "hls_segment_seconds": settings["hls_segment_seconds"],
        "count": len(items),
        "scan": {
            "source_count": source_count,
            "processed": processed_count,
            "cached": cached_count,
            "skipped": skipped_count,
            "errors": error_count,
            "in_progress": True,
        },
        "items": items,
    }
    atomic_write_json(catalog_path, catalog)
    return items


def main():
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    media_root = root / "media"
    cache_root = root / "cache"
    data_root = root / "data"
    cache_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    force_marker = data_root / "force-rebuild"
    if force_marker.is_file() and not arguments.force:
        arguments.force = True
        log("A changed encoding configuration requested one full cache rebuild")

    ffmpeg = os.environ.get("VIDEO_FFMPEG", "ffmpeg")
    ffprobe = os.environ.get("VIDEO_FFPROBE", "ffprobe")
    if not command_exists(ffmpeg) or not command_exists(ffprobe):
        raise SystemExit("ffmpeg and ffprobe must be installed and available in PATH")

    configured_preset = os.environ.get("VIDEO_HLS_PRESET", HLS_PRESET).strip().lower()
    if configured_preset not in ALLOWED_HLS_PRESETS:
        raise SystemExit("VIDEO_HLS_PRESET must be one of {}".format(", ".join(sorted(ALLOWED_HLS_PRESETS))))
    settings = {
        "thumbnail_interval": max(1, integer(os.environ.get("VIDEO_THUMB_INTERVAL", "10"), 10)),
        "thumbnail_width": max(160, integer(os.environ.get("VIDEO_THUMB_WIDTH", "480"), 480)),
        "max_thumbnails": max(0, integer(os.environ.get("VIDEO_MAX_THUMBNAILS", "0"), 0)),
        "hls_segment_seconds": max(2, integer(os.environ.get("VIDEO_HLS_SEGMENT_SECONDS", "6"), 6)),
        "hls_target_height": max(2, integer(os.environ.get("VIDEO_HLS_HEIGHT", "1080"), 1080)),
        "hls_preset": configured_preset,
        "video_bitrate": max(250_000, integer(os.environ.get("VIDEO_HLS_VIDEO_BITRATE", "6500000"), 6_500_000)),
        "audio_bitrate": max(32_000, integer(os.environ.get("VIDEO_HLS_AUDIO_BITRATE", "160000"), 160_000)),
        "settle_seconds": max(0, integer(os.environ.get("VIDEO_SETTLE_SECONDS", "60"), 60)),
        "failure_retry_seconds": max(60, integer(os.environ.get("VIDEO_FAILURE_RETRY_SECONDS", "300"), 300)),
        "cache_retention_seconds": max(3600, integer(os.environ.get("VIDEO_CACHE_RETENTION_SECONDS", "86400"), 86400)),
    }
    extensions = set(DEFAULT_EXTENSIONS)
    extra_extensions = os.environ.get("VIDEO_EXTENSIONS", "")
    if extra_extensions.strip():
        extensions = {value.strip().lower() for value in extra_extensions.split(",") if value.strip()}
        extensions = {value if value.startswith(".") else "." + value for value in extensions}

    lock_path = data_root / "scan.lock"
    lock_handle = lock_path.open("a+")
    try:
        lock_operation = fcntl.LOCK_EX if arguments.wait_for_lock else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock_handle.fileno(), lock_operation)
    except BlockingIOError:
        log("Another scan is already running; exiting cleanly")
        return 0

    abandoned = clean_abandoned_workdirs(cache_root)
    if abandoned:
        log("Removed {} abandoned temporary build director{}".format(
            abandoned, "y" if abandoned == 1 else "ies",
        ))

    repaired_roots = repair_completed_cache_roots(cache_root)
    if repaired_roots:
        log("Repaired web permissions on {} completed cache director{}".format(
            repaired_roots, "y" if repaired_roots == 1 else "ies",
        ))

    catalog_path = data_root / "catalog.json"
    previous_catalog = load_json(catalog_path, {"items": []})
    previous_by_id = {item.get("id"): item for item in previous_catalog.get("items", []) if item.get("id")}
    failures_path = data_root / "failures.json"
    failures = load_json(failures_path, {})
    if not isinstance(failures, dict):
        failures = {}

    videos = discover_videos(media_root, extensions)
    ingest_records = update_ingest_order(data_root / "ingest-order.json", videos, media_root)
    videos = order_videos_by_upload(videos, media_root, ingest_records)
    for previous_item in previous_catalog.get("items", []):
        if isinstance(previous_item, dict) and previous_item.get("id"):
            apply_upload_metadata(previous_item, ingest_records.get(previous_item["id"]))
    log("Found {} source video{} in {}".format(len(videos), "" if len(videos) == 1 else "s", media_root))
    if arguments.verbose:
        log("Encoding policy: one rendition up to {}p, x264 preset {}".format(
            settings["hls_target_height"], settings["hls_preset"],
        ))
    items = []
    video_by_relative = {path.relative_to(media_root).as_posix(): path for path in videos}
    published_by_id = {}
    for previous_item in previous_catalog.get("items", []):
        if not isinstance(previous_item, dict) or not previous_item.get("id"):
            continue
        previous_path = video_by_relative.get(previous_item.get("source_relative"))
        if not previous_path:
            continue
        try:
            expected_version = canonical_hash(source_signature(previous_item["source_relative"], previous_path.stat()), 14)
        except OSError:
            continue
        if previous_item.get("version") == expected_version:
            published_by_id[previous_item["id"]] = previous_item
    for relative, video_path in video_by_relative.items():
        stable_id = source_id(relative)
        try:
            stat_result = video_path.stat()
        except OSError:
            continue
        recovered = reusable_cached_item(
            published_by_id.get(stable_id), stable_id, relative, stat_result, cache_root,
        )
        if recovered:
            apply_upload_metadata(recovered, ingest_records.get(stable_id))
            published_by_id[stable_id] = recovered
    processed_count = 0
    cached_count = 0
    skipped_count = 0
    error_count = 0
    now = time.time()
    publish_catalog(
        catalog_path, published_by_id, settings, len(videos),
        processed_count, cached_count, skipped_count, error_count,
    )

    for index, path in enumerate(videos, 1):
        relative = path.relative_to(media_root).as_posix()
        stable_id = source_id(relative)
        previous = previous_by_id.get(stable_id)
        try:
            stat_result = path.stat()
        except OSError as error:
            log("SKIP {}: {}".format(relative, error))
            skipped_count += 1
            continue

        if now - stat_result.st_mtime < settings["settle_seconds"]:
            log("WAIT {}/{} {} (file is still settling)".format(index, len(videos), relative))
            if previous:
                items.append(previous)
            skipped_count += 1
            continue

        signature = source_signature(relative, stat_result)
        signature_version = canonical_hash(signature, 14)
        failure = failures.get(stable_id) or {}
        if (
            not arguments.force
            and failure.get("version") == signature_version
            and now - number(failure.get("attempted_epoch")) < settings["failure_retry_seconds"]
        ):
            log("WAIT {}/{} {} (unchanged failure is in retry cooldown)".format(index, len(videos), relative))
            if previous:
                items.append(previous)
            skipped_count += 1
            continue

        try:
            item, changed = process_video(
                path, relative, stat_result, settings, cache_root, ffmpeg, ffprobe,
                previous=previous, force=arguments.force,
            )
            apply_upload_metadata(item, ingest_records.get(stable_id))
            creation_indexed = False
            stream_selection_indexed = False
            if not changed:
                item, stream_selection_indexed = add_cached_primary_video_stream_index(
                    item, cache_root
                )
            if not changed and "creation_at" not in item:
                try:
                    item, creation_indexed = add_cached_creation_time(item, path, ffprobe, cache_root)
                except Exception as creation_error:
                    log("WARN {}/{} {} creation date: {}".format(index, len(videos), relative, creation_error))
            items.append(item)
            published_by_id[stable_id] = item
            failures.pop(stable_id, None)
            if changed:
                processed_count += 1
                rendition_count = len(item["hls_variants"])
                thumbnail_count = len(item["thumbnails"])
                log("BUILD {}/{} {} -> {} HLS rendition{}, {} thumbnail{}".format(
                    index, len(videos), relative,
                    rendition_count, "" if rendition_count == 1 else "s",
                    thumbnail_count, "" if thumbnail_count == 1 else "s",
                ))
            else:
                cached_count += 1
                if arguments.verbose:
                    indexed = []
                    if creation_indexed:
                        indexed.append("creation date")
                    if stream_selection_indexed:
                        indexed.append("primary video stream")
                    suffix = " + {} indexed".format(" and ".join(indexed)) if indexed else ""
                    log("CACHE {}/{} {}{}".format(index, len(videos), relative, suffix))
            publish_catalog(
                catalog_path, published_by_id, settings, len(videos),
                processed_count, cached_count, skipped_count, error_count,
            )
        except Exception as error:
            error_count += 1
            message = str(error).strip() or error.__class__.__name__
            log("ERROR {}/{} {}: {}".format(index, len(videos), relative, message))
            failures[stable_id] = {
                "relative_path": relative,
                "version": signature_version,
                "attempted_at": utc_iso(),
                "attempted_epoch": time.time(),
                "error": message[-4000:],
            }
            if previous:
                items.append(previous)
            if arguments.verbose:
                traceback.print_exc()

    published_by_id = {item.get("id"): item for item in items if item.get("id")}
    items = publish_catalog(
        catalog_path, published_by_id, settings, len(videos),
        processed_count, cached_count, skipped_count, error_count,
    )
    final_catalog = load_json(catalog_path, {})
    if isinstance(final_catalog.get("scan"), dict):
        final_catalog["scan"]["in_progress"] = False
        atomic_write_json(catalog_path, final_catalog)
    atomic_write_json(failures_path, failures)

    active_keys = {item.get("cache_key") for item in items if item.get("cache_key")}
    removed = clean_cache(cache_root, active_keys, settings["cache_retention_seconds"])
    log("Catalog ready: {} listed, {} built, {} cached, {} errors, {} old caches removed".format(
        len(items), processed_count, cached_count, error_count, removed,
    ))
    if error_count == 0 and force_marker.is_file():
        try:
            force_marker.unlink()
        except OSError as error:
            log("WARN could not remove completed rebuild marker: {}".format(error))
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(130)
