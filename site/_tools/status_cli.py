#!/usr/bin/env python3
"""Human-readable terminal view of the video library encoder telemetry."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

DISPLAY_NAME = "HLS Video Gallery"
SHOW_CATEGORIES = True


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def duration(value):
    seconds = max(0, int(round(number(value))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{}:{:02d}".format(minutes, seconds)


def long_duration(value):
    seconds = max(0, int(round(number(value))))
    if not seconds:
        return "calculating"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours or days:
        parts.append("{}h".format(hours))
    parts.append("{}m".format(minutes))
    return " ".join(parts)


def finish_time(value):
    if not value:
        return "calculating"
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
        return parsed.strftime("%a %b %-d at %-I:%M %p")
    except (TypeError, ValueError, OSError):
        return "calculating"


def progress_bar(percent, width=34):
    percent = min(100.0, max(0.0, number(percent)))
    filled = int(round(width * percent / 100.0))
    return "[{}{}]".format("#" * filled, "-" * (width - filled))


def read_encoding_payload(root):
    path = root / "data" / "encode-progress.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": False, "error": "Telemetry has not been published yet: {}".format(path)}
    except (OSError, ValueError) as error:
        return {"active": False, "error": "Unable to read telemetry: {}".format(error)}


def read_category_payload(root):
    path = root / "data" / "content-analysis-progress.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "unknown", "error": "Category telemetry has not been published yet: {}".format(path)}
    except (OSError, ValueError) as error:
        return {"state": "unknown", "error": "Unable to read category telemetry: {}".format(error)}


def render_encoding(payload, include_command=False, include_all=False):
    lines = ["{} encoder".format(DISPLAY_NAME), "=" * 72]
    if payload.get("error"):
        lines.append(payload["error"])
        return "\n".join(lines)
    if not payload.get("active"):
        lines.extend([
            "Status       IDLE — no FFmpeg job is running",
            "Updated      {}".format(payload.get("updated_at", "unknown")),
            "", payload.get("note", "The monitor will update when encoding starts."),
        ])
        return "\n".join(lines)

    queue = payload.get("queue") or {}
    lines.extend([
        "Status       {}".format(payload.get("phase_label", payload.get("phase", "active"))),
        "Pass         {}".format(payload.get("pass_label", "Processing")),
        "Current      {}".format(payload.get("source", "unknown")),
        "Queue        item {} of {} this run · {} processed · {} after current".format(
            queue.get("position", 0), queue.get("total", 0), queue.get("completed", 0),
            queue.get("remaining_after_current", 0)
        ),
        "Catalog      {} live · {} ready of {} sources".format(
            queue.get("library_published", queue.get("published", 0)),
            queue.get("library_ready", queue.get("ready", 0)),
            queue.get("library_total", queue.get("total", 0)),
        ),
        "Phase        {} {:5.1f}% · {} / {}".format(
            progress_bar(payload.get("percent")), number(payload.get("percent")),
            duration(payload.get("position_seconds")), duration(payload.get("duration_seconds"))
        ),
        "Performance  {:.1f} processing fps · {:.2f}x realtime · {:.0f}% CPU".format(
            number(payload.get("processing_fps")), number(payload.get("speed")), number(payload.get("cpu_percent"))
        ),
        "Timing       elapsed {} · phase ETA {}".format(duration(payload.get("elapsed_seconds")), duration(payload.get("eta_seconds"))),
        "Library      {} {:5.1f}% ready overall".format(progress_bar(payload.get("overall_percent")), number(payload.get("overall_percent"))),
        "Queue media  {} this run · {} remaining".format(long_duration(queue.get("total_duration_seconds")), long_duration(queue.get("remaining_duration_seconds"))),
        "Prediction   {} processing · finish {}".format(long_duration(queue.get("predicted_processing_seconds")), finish_time(queue.get("predicted_finish_at"))),
        "Duration map {} of {} files indexed{}".format(
            queue.get("duration_indexed_count", 0), queue.get("total", 0), " · complete" if queue.get("duration_index_complete") else ""
        ),
        "Updated      {}".format(payload.get("updated_at", "unknown")),
        "", "FFmpeg parameters", "-" * 72,
    ])
    parameters = payload.get("parameters") or {}
    for label, value in parameters.items():
        lines.append("{:<14} {}".format(label, value))
    upcoming = queue.get("upcoming") or []
    lines.extend(["", "Up next", "-" * 72])
    if upcoming:
        visible = upcoming if include_all else upcoming[:8]
        for offset, name in enumerate(visible, 1):
            lines.append("{:>3}. {}".format(offset, name))
        if not include_all and len(upcoming) > len(visible):
            lines.append("    … {} more queued; use --all to print the complete list".format(len(upcoming) - len(visible)))
    else:
        lines.append("Nothing else is queued after the current file.")
    if include_command:
        lines.extend(["", "Active FFmpeg command", "-" * 72, payload.get("command") or "Unavailable"])
    lines.extend(["", payload.get("note", "")])
    return "\n".join(lines)


def render_categories(payload, include_all=False):
    lines = ["{} category analyzer".format(DISPLAY_NAME), "=" * 72]
    if payload.get("error") and payload.get("state") == "unknown":
        lines.append(payload["error"])
        return "\n".join(lines)

    state = str(payload.get("state") or "unknown")
    catalog_count = int(number(payload.get("catalog_count")))
    analyzed_count = int(number(payload.get("analyzed_count")))
    pending_count = int(number(payload.get("pending_count")))
    percent = number(payload.get("percent"), 100.0 * analyzed_count / catalog_count if catalog_count else 100.0)
    lines.extend([
        "Status       {}".format(payload.get("phase_label") or state.replace("_", " ").upper()),
        "Progress     {} {:5.1f}% · {} analyzed / {} total · {} pending".format(
            progress_bar(percent), percent,
            analyzed_count, catalog_count, pending_count,
        ),
    ])
    if payload.get("source"):
        lines.append("Current      {}".format(payload.get("source")))
    if payload.get("batch_total"):
        lines.append("Batch        video {} of {}".format(payload.get("batch_position", 0), payload.get("batch_total", 0)))
    if payload.get("frames_total"):
        lines.append("Frames       {} {:5.1f}% · {} of {} cached thumbnails".format(
            progress_bar(payload.get("frame_percent")), number(payload.get("frame_percent")),
            int(number(payload.get("frames_done"))), int(number(payload.get("frames_total"))),
        ))
    lines.extend([
        "Pace         {:.1f} videos/hour · {:.1f}s compute/video · {:.1f}s effective/video".format(
            number(payload.get("videos_per_hour")), number(payload.get("average_seconds_per_video")),
            number(payload.get("effective_seconds_per_video")),
        ),
        "Prediction   {} remaining · finish {}".format(
            "complete" if pending_count == 0 else long_duration(payload.get("eta_seconds")),
            "complete" if pending_count == 0 else finish_time(payload.get("estimated_finish_at")),
        ),
        "Model        {}".format(payload.get("model", "unknown")),
        "Updated      {}".format(payload.get("updated_at", "unknown")),
    ])
    if payload.get("reason"):
        lines.append("Waiting      {}".format(payload.get("reason")))
    if state == "error" and payload.get("error"):
        lines.append("Error        {}".format(payload.get("error")))

    upcoming = payload.get("upcoming") or []
    if upcoming:
        lines.extend(["", "Category queue", "-" * 72])
        visible = upcoming if include_all else upcoming[:8]
        for offset, name in enumerate(visible, 1):
            lines.append("{:>3}. {}".format(offset, name))
        if not include_all and len(upcoming) > len(visible):
            lines.append("    … {} more shown by the web queue; use --all for this published list".format(len(upcoming) - len(visible)))
    return "\n".join(lines)


def render(encoding, categories, include_command=False, include_all=False):
    if not SHOW_CATEGORIES:
        return render_encoding(encoding, include_command, include_all)
    return "{}\n\n{}".format(
        render_encoding(encoding, include_command, include_all),
        render_categories(categories, include_all),
    )


def parse_arguments():
    parser = argparse.ArgumentParser(description="Show the current HLS video gallery status.")
    default_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)))
    parser.add_argument("--watch", action="store_true", help="refresh continuously until Ctrl+C")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--command", action="store_true", help="include the active sanitized FFmpeg command")
    parser.add_argument("--all", action="store_true", help="print every upcoming video instead of the next eight")
    parser.add_argument("--json", action="store_true", help="print the raw telemetry JSON")
    return parser.parse_args()


def main():
    global DISPLAY_NAME, SHOW_CATEGORIES
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    try:
        site_config = json.loads((root / "data" / "site-config.json").read_text(encoding="utf-8"))
        DISPLAY_NAME = str((site_config.get("brand") or {}).get("gallery_name") or DISPLAY_NAME)
        SHOW_CATEGORIES = bool((site_config.get("features") or {}).get("content_analysis", False))
    except (OSError, ValueError, TypeError):
        pass
    while True:
        encoding = read_encoding_payload(root)
        categories = read_category_payload(root) if SHOW_CATEGORIES else {"state": "disabled"}
        payload = {"encoding": encoding, "categories": categories}
        output = json.dumps(payload, ensure_ascii=False, indent=2) if arguments.json else render(
            encoding, categories, arguments.command, arguments.all,
        )
        if arguments.watch:
            sys.stdout.write("\033[2J\033[H")
        print(output, flush=True)
        if not arguments.watch:
            return 0
        time.sleep(max(0.5, arguments.interval))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)
