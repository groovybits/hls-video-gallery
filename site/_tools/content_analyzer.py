#!/usr/bin/env python3
"""Lazy, low-priority visual tagging for completed HLS gallery entries.

The analyzer deliberately reuses cached 10-second thumbnails instead of decoding
source video again.  It publishes only a curated tag vocabulary; it never emits
open-ended object labels or captions.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ANALYZER_VERSION = "mobileclip2-s0-configurable-v1"
MODEL_NAME = "MobileCLIP2-S0"
MODEL_PRETRAINED = "dfndr2b"
_MODEL_RUNTIME = None
TAG_DEFINITIONS = []


def configure_taxonomy(root):
    """Load the installer-validated tag vocabulary and bind cache identity to it."""
    global ANALYZER_VERSION, TAG_DEFINITIONS
    configured = os.environ.get("VIDEO_ANALYZER_TAGS", "").strip()
    path = Path(configured).expanduser() if configured else root / "data" / "content-tags.json"
    payload = load_json(path, {})
    tags = payload.get("tags") if isinstance(payload, dict) else None
    if not isinstance(tags, list) or not tags:
        raise RuntimeError("content tag taxonomy is missing or empty: {}".format(path))
    required = {"key", "label", "group", "threshold", "positive", "negative"}
    for index, tag in enumerate(tags):
        if not isinstance(tag, dict) or not required.issubset(tag):
            raise RuntimeError("content tag {} is incomplete in {}".format(index, path))
    canonical = json.dumps(tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    TAG_DEFINITIONS = tags
    ANALYZER_VERSION = "mobileclip2-s0-configurable-v1-" + hashlib.sha256(canonical).hexdigest()[:12]


def utc_iso(timestamp=None):
    moment = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp_percent(value):
    return round(min(100.0, max(0.0, float(value))), 1)


def analysis_average(records, default=12.0):
    samples = []
    for record in records.values():
        try:
            seconds = float(record.get("analysis_seconds", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if 0.1 <= seconds <= 86400:
            samples.append(seconds)
    if not samples:
        return float(default)
    # Recent work best reflects the current host, while the cap prevents one old
    # outlier from making the library ETA useless.
    samples = samples[-40:]
    samples.sort()
    if len(samples) >= 5:
        trim = max(1, len(samples) // 10)
        samples = samples[trim:-trim]
    return sum(samples) / len(samples)


def progress_forecast(records, pending_count, run_batch_size):
    compute_seconds = analysis_average(
        records, float(os.environ.get("VIDEO_ANALYZER_DEFAULT_SECONDS", "12"))
    )
    interval_seconds = max(0.0, float(os.environ.get("VIDEO_ANALYZER_RUN_INTERVAL_SECONDS", "210")))
    batch_size = max(1, int(run_batch_size))
    effective_seconds = compute_seconds + interval_seconds / batch_size
    eta_seconds = max(0.0, float(pending_count) * effective_seconds)
    return {
        "average_seconds_per_video": round(compute_seconds, 1),
        "effective_seconds_per_video": round(effective_seconds, 1),
        "videos_per_hour": round(3600.0 / effective_seconds, 1) if effective_seconds else 0,
        "eta_seconds": round(eta_seconds),
        "estimated_finish_at": utc_iso(time.time() + eta_seconds) if pending_count else utc_iso(),
    }


def progress_payload(state, catalog_items, records, pending_items, run_batch_size, **details):
    catalog_count = len(catalog_items)
    analyzed_count = len(records)
    pending_count = len(pending_items)
    payload = {
        "schema_version": 2,
        "state": state,
        "phase": details.pop("phase", state),
        "phase_label": details.pop("phase_label", state.replace("_", " ").title()),
        "model": MODEL_NAME,
        "analyzer_version": ANALYZER_VERSION,
        "updated_at": utc_iso(),
        "catalog_count": catalog_count,
        "analyzed_count": analyzed_count,
        "pending_count": pending_count,
        "percent": clamp_percent(100.0 * analyzed_count / catalog_count) if catalog_count else 100.0,
        "upcoming": [
            item.get("source_relative") or item.get("title") or item.get("id")
            for item in pending_items
        ],
    }
    payload.update(progress_forecast(records, pending_count, run_batch_size))
    payload.update(details)
    return payload


def load_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return fallback


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), prefix="." + path.name + ".", delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    finally:
        try:
            handle.close()
        except Exception:
            pass
        if temporary.exists():
            temporary.unlink()


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


def encoder_active(root):
    root_text = str(root)
    for command in process_cmdlines():
        if "ffmpeg" in command and (root_text in command or "/cache/.building-" in command):
            return True
    return False


def item_signature(item):
    return str(item.get("cache_key") or "{}--{}".format(item.get("id", ""), item.get("version", "")))


def public_url_to_path(root, url):
    clean = str(url or "").split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if clean.startswith("video/"):
        clean = clean[6:]
    candidate = (root / clean).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def sample_thumbnail_paths(root, item, stride, maximum):
    candidates = []
    for thumbnail in item.get("thumbnails") or []:
        path = public_url_to_path(root, thumbnail.get("url"))
        if path and path.is_file():
            candidates.append((float(thumbnail.get("time_seconds") or 0), path))
    candidates = candidates[::max(1, stride)]
    if len(candidates) > maximum:
        indexes = sorted(set(round(index * (len(candidates) - 1) / (maximum - 1)) for index in range(maximum)))
        candidates = [candidates[index] for index in indexes]
    return candidates


def load_model():
    global _MODEL_RUNTIME
    if _MODEL_RUNTIME is not None:
        return _MODEL_RUNTIME
    try:
        import open_clip
        import torch
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("content-analysis environment is incomplete: {}".format(error))

    thread_count = max(1, int(os.environ.get("VIDEO_ANALYZER_THREADS", "1")))
    torch.set_num_threads(thread_count)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=MODEL_PRETRAINED, device="cpu",
    )
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    model.eval()
    text_features = build_text_features(torch, model, tokenizer)
    _MODEL_RUNTIME = (torch, Image, model, preprocess, text_features)
    return _MODEL_RUNTIME


def build_text_features(torch, model, tokenizer):
    pairs = []
    with torch.inference_mode():
        for definition in TAG_DEFINITIONS:
            sides = []
            for prompt_kind in ("positive", "negative"):
                prompts = ["a photo showing " + prompt for prompt in definition[prompt_kind]]
                tokens = tokenizer(prompts)
                features = model.encode_text(tokens)
                features = features / features.norm(dim=-1, keepdim=True)
                centroid = features.mean(dim=0)
                centroid = centroid / centroid.norm()
                sides.append(centroid)
            pairs.append(torch.stack(sides))
    return torch.stack(pairs)


def aggregate_tags(frame_scores, frame_times):
    published = []
    frame_count = len(frame_times)
    if not frame_count:
        return published
    for index, definition in enumerate(TAG_DEFINITIONS):
        scores = [float(row[index]) for row in frame_scores]
        threshold = definition["threshold"]
        evidence = [(frame_times[position], score) for position, score in enumerate(scores) if score >= threshold]
        minimum_hits = 1 if frame_count <= 2 else max(2, int(math.ceil(frame_count * 0.08)))
        top_count = max(1, int(math.ceil(frame_count * 0.15)))
        top_mean = sum(sorted(scores, reverse=True)[:top_count]) / top_count
        if len(evidence) < minimum_hits or top_mean < threshold:
            continue
        evidence_mean = sum(score for _time, score in evidence) / len(evidence)
        confidence = min(0.99, 0.6 * top_mean + 0.4 * evidence_mean)
        published.append({
            "key": definition["key"],
            "label": definition["label"],
            "group": definition["group"],
            "confidence": round(confidence, 3),
            "coverage": round(len(evidence) / frame_count, 3),
            "evidence_frames": len(evidence),
            "evidence_seconds": [round(value[0], 1) for value in sorted(evidence, key=lambda row: row[1], reverse=True)[:6]],
            "source": "visual",
        })

    # A configurable People group normally contains mutually exclusive counts.
    people = [tag for tag in published if tag["group"].casefold() == "people"]
    if len(people) > 1:
        winner = max(people, key=lambda tag: (tag["confidence"], tag["coverage"]))
        published = [tag for tag in published if tag["group"].casefold() != "people" or tag is winner]
    return sorted(published, key=lambda tag: (tag["group"], -tag["confidence"], tag["label"]))


def analyze_item(root, item, stride, maximum, image_batch_size, progress_callback=None):
    frames = sample_thumbnail_paths(root, item, stride, maximum)
    if not frames:
        raise RuntimeError("no readable cached thumbnails")
    if progress_callback:
        progress_callback(0, len(frames), "loading_model")
    torch, Image, model, preprocess, text_features = load_model()
    if progress_callback:
        progress_callback(0, len(frames), "analyzing_frames")
    frame_scores = []
    frame_times = []
    with torch.inference_mode():
        for offset in range(0, len(frames), image_batch_size):
            portion = frames[offset:offset + image_batch_size]
            images = []
            for _timestamp, path in portion:
                with Image.open(str(path)) as source:
                    images.append(preprocess(source.convert("RGB")))
            image_features = model.encode_image(torch.stack(images))
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            # [frames, tags, positive/negative]; pairwise softmax prevents every
            # frame from being forced into one of the public tags.
            logits = 100.0 * torch.einsum("bd,tkd->btk", image_features, text_features)
            probabilities = logits.softmax(dim=-1)[:, :, 0]
            frame_scores.extend(probabilities.cpu().tolist())
            frame_times.extend(timestamp for timestamp, _path in portion)
            if progress_callback:
                progress_callback(len(frame_times), len(frames), "analyzing_frames")
    return aggregate_tags(frame_scores, frame_times), len(frames)


def parse_arguments():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Analyze completed cached video thumbnails lazily.")
    parser.add_argument("--root", default=os.environ.get("VIDEO_LIBRARY_ROOT", str(default_root)))
    parser.add_argument("--items", type=int, default=1, help="Maximum videos to analyze this run")
    parser.add_argument("--force", action="store_true", help="Reanalyze even if a matching result is cached")
    parser.add_argument("--ignore-busy", action="store_true", help="Ignore encoder/load checks (manual use only)")
    parser.add_argument("--prune-only", action="store_true", help="Remove stale records without loading the model")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    root = Path(arguments.root).expanduser().resolve()
    configure_taxonomy(root)
    data_root = root / "data"
    index_path = data_root / "content-index.json"
    progress_path = data_root / "content-analysis-progress.json"
    lock_path = data_root / "content-analysis.lock"
    data_root.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another content-analysis pass is already running; exiting cleanly")
            return 0

        # Quality measurement and visual tagging are both deliberately
        # low-priority post-processing jobs. Sharing this lock prevents them
        # from competing with each other and keeps timing/quality telemetry
        # representative of one job at a time.
        post_lock_handle = (data_root / "post-process.lock").open("a+")
        try:
            fcntl.flock(post_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            post_lock_handle.close()
            print("Quality analysis is using the post-processing slot; exiting cleanly")
            return 0

        catalog = load_json(data_root / "catalog.json", {"items": []})
        catalog_items = [item for item in catalog.get("items", []) if isinstance(item, dict) and item.get("id")]
        current_signatures = {item["id"]: item_signature(item) for item in catalog_items}
        index = load_json(index_path, {})
        if not isinstance(index, dict):
            index = {}
        records = index.get("items") if isinstance(index.get("items"), dict) else {}
        records = {
            item_id: record for item_id, record in records.items()
            if item_id in current_signatures and isinstance(record, dict)
            and record.get("cache_key") == current_signatures[item_id]
        }

        overrides = load_json(data_root / "content-overrides.json", {})
        if not isinstance(overrides, dict):
            overrides = {}

        pending = [
            item for item in catalog_items
            if arguments.force
            or item.get("id") not in records
            or records[item["id"]].get("analyzer_version") != ANALYZER_VERSION
            or records[item["id"]].get("override_signature") != hashlib.sha256(
                json.dumps(overrides.get(item["id"], {}), sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16]
        ]
        index = {
            "schema_version": 1,
            "analyzer_version": ANALYZER_VERSION,
            "updated_at": utc_iso(),
            "catalog_count": len(catalog_items),
            "analyzed_count": len(records),
            "pending_count": len(pending),
            "items": records,
        }
        atomic_write_json(index_path, index)
        run_batch_size = max(1, arguments.items)

        if arguments.prune_only or not pending:
            state = "complete" if not pending else "pruned"
            atomic_write_json(progress_path, progress_payload(
                state, catalog_items, records, pending, run_batch_size,
                phase_label="Category analysis complete" if not pending else "Category index cleaned",
            ))
            print("Content index: {} analyzed, {} pending".format(len(records), len(pending)))
            return 0

        maximum_load = float(os.environ.get("VIDEO_ANALYZER_MAX_LOAD", "1.50"))
        if not arguments.ignore_busy:
            if encoder_active(root):
                reason = "video encoding is active"
            elif os.getloadavg()[0] > maximum_load:
                reason = "one-minute load {:.2f} exceeds {:.2f}".format(os.getloadavg()[0], maximum_load)
            else:
                reason = ""
            if reason:
                atomic_write_json(progress_path, progress_payload(
                    "waiting", catalog_items, records, pending, run_batch_size,
                    phase="waiting_for_resources", phase_label="Category analysis is waiting", reason=reason,
                ))
                print("DEFER: {} ({} videos pending)".format(reason, len(pending)))
                return 0

        stride = max(1, int(os.environ.get("VIDEO_ANALYZER_THUMB_STRIDE", "3")))
        maximum = max(2, int(os.environ.get("VIDEO_ANALYZER_MAX_FRAMES", "72")))
        image_batch_size = max(1, int(os.environ.get("VIDEO_ANALYZER_IMAGE_BATCH", "8")))
        processed = 0
        run_started_epoch = time.time()
        run_started_at = utc_iso(run_started_epoch)
        batch_items = pending[:run_batch_size]
        for batch_offset, item in enumerate(batch_items):
            if processed and not arguments.ignore_busy and encoder_active(root):
                print("DEFER: video encoding started; leaving the remaining analysis queue untouched")
                break
            item_id = item["id"]
            item_started_epoch = time.time()
            item_started_at = utc_iso(item_started_epoch)
            print("ANALYZE {} ({})".format(item.get("source_relative") or item.get("title"), item_id))
            current_pending = pending[processed:]

            def publish_item_progress(frames_done, frames_total, phase):
                if phase == "loading_thumbnails":
                    phase_label = "Preparing cached thumbnails"
                elif phase == "loading_model":
                    phase_label = "Loading category model"
                else:
                    phase_label = "Analyzing thumbnail frames"
                atomic_write_json(progress_path, progress_payload(
                    "analyzing", catalog_items, records, current_pending, run_batch_size,
                    phase=phase, phase_label=phase_label,
                    source=item.get("source_relative") or item.get("title"), video_id=item_id,
                    run_started_at=run_started_at, item_started_at=item_started_at,
                    elapsed_seconds=round(time.time() - run_started_epoch, 1),
                    item_elapsed_seconds=round(time.time() - item_started_epoch, 1),
                    frames_done=frames_done, frames_total=frames_total,
                    frame_percent=clamp_percent(100.0 * frames_done / frames_total) if frames_total else 0,
                    batch_position=batch_offset + 1, batch_total=len(batch_items),
                ))

            publish_item_progress(0, 0, "loading_thumbnails")
            try:
                tags, frame_count = analyze_item(
                    root, item, stride, maximum, image_batch_size, publish_item_progress,
                )
                manual = overrides.get(item_id) if isinstance(overrides.get(item_id), dict) else {}
                excluded = set(manual.get("remove") or [])
                tags = [tag for tag in tags if tag["key"] not in excluded]
                existing_keys = {tag["key"] for tag in tags}
                for tag in manual.get("add") or []:
                    if isinstance(tag, dict) and tag.get("key") and tag["key"] not in existing_keys:
                        corrected = dict(tag)
                        corrected["source"] = "manual"
                        corrected.setdefault("confidence", 1.0)
                        corrected.setdefault("coverage", 1.0)
                        corrected.setdefault("group", "Activity")
                        corrected.setdefault("label", corrected["key"].replace("_", " ").title())
                        tags.append(corrected)
                records[item_id] = {
                    "cache_key": item_signature(item), "analyzer_version": ANALYZER_VERSION,
                    "override_signature": hashlib.sha256(
                        json.dumps(manual, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()[:16],
                    "analyzed_at": utc_iso(), "sampled_frames": frame_count, "tags": tags,
                    "analysis_seconds": round(time.time() - item_started_epoch, 1),
                }
                processed += 1
                remaining = max(0, len(pending) - processed)
                index.update({
                    "updated_at": utc_iso(), "analyzed_count": len(records),
                    "pending_count": remaining, "items": records,
                })
                atomic_write_json(index_path, index)
                print("TAGGED {} with {} tag{} from {} frames".format(
                    item.get("source_relative"), len(tags), "" if len(tags) == 1 else "s", frame_count,
                ))
            except Exception as error:
                atomic_write_json(progress_path, progress_payload(
                    "error", catalog_items, records, pending[processed:], run_batch_size,
                    phase_label="Category analysis stopped", source=item.get("source_relative") or item.get("title"),
                    video_id=item_id, error=str(error)[-2000:], run_started_at=run_started_at,
                    item_started_at=item_started_at, elapsed_seconds=round(time.time() - run_started_epoch, 1),
                    item_elapsed_seconds=round(time.time() - item_started_epoch, 1),
                    batch_position=batch_offset + 1, batch_total=len(batch_items),
                ))
                raise

        remaining_items = pending[processed:]
        final_state = "complete" if not remaining_items else "idle"
        atomic_write_json(progress_path, progress_payload(
            final_state, catalog_items, records, remaining_items, run_batch_size,
            phase="complete" if not remaining_items else "waiting_for_next_batch",
            phase_label="Category analysis complete" if not remaining_items else "Waiting for the next category batch",
            run_started_at=run_started_at, elapsed_seconds=round(time.time() - run_started_epoch, 1),
            last_processed=processed, batch_total=len(batch_items),
        ))
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
