# Objective quality analysis

Objective quality analysis is an optional, low-priority stage that compares each
finished HLS encode with its source. It runs after encoding and, when visual
content analysis is enabled, after that video's current content-analysis record
exists. It does not alter the source or HLS output.

The feature has two parts:

- `hls-quality-analyzer`, a standalone C++ command-line program that measures one
  reference/distorted pair and creates portable reports;
- `_tools/quality_analyzer.py`, the gallery worker that queues videos, reuses
  current reports, publishes status, and invokes the C++ program one video at a
  time.

Objective scores are useful for finding regressions and unusually weak scenes.
They are not a substitute for watching representative material. Creative grain,
very dark images, animation, and tone mapping can all affect a metric differently
from a human viewer.

## Enable it

FFmpeg must include the `libvmaf`, `scdet`, `colorspace`, `zscale`, and `tonemap`
filters.
The supported dependency installer installs build tools and a suitable FFmpeg
combination where the host distribution provides one.

Set both the worker and its authenticated interface in `config/gallery.json`:

```json
{
  "gallery": {
    "show_quality_analysis": true
  },
  "quality_analysis": {
    "enabled": true,
    "items_per_run": 1,
    "interval_seconds": 1,
    "max_load": 0,
    "threads": 2,
    "frame_rate": 30,
    "scene_threshold": 10,
    "min_scene_seconds": 2,
    "failure_retry_seconds": 30
  }
}
```

Then run the normal installer:

```bash
sudo ./scripts/install-dependencies.sh
sudo ./scripts/install.sh
```

The installer builds and installs the C++ program, renders an instance-specific
service and timer, and starts the queue. Existing current encodes are eligible;
the source videos are not re-encoded.

Confirm that FFmpeg has the required filters:

```bash
ffmpeg -hide_banner -filters | grep -E 'libvmaf|scdet|colorspace|zscale|tonemap'
```

## Measurement method

The defaults deliberately favor repeatable full-reference comparisons over fast
sampling:

1. The exact global video stream selected by the encoder from the original file
   is the reference. Attached pictures and unselected proxy or alternate tracks
   are not compared. The completed HLS rendition is the distorted input.
2. Timestamps are reset and both inputs are aligned at 30 frames per second.
3. The reference is scaled to the encoded display dimensions before comparison.
4. Both sides enter the same display-referred BT.709 comparison domain. SDR pairs
   receive matching normalization. HLG/BT.2020 pairs receive the same BT.709
   display transform on both the source and encode, and the report is labeled
   `HDR normalized`. This prevents mismatched transfer functions from being
   mistaken for compression damage.
5. Every aligned frame is measured. The default does not select a sparse sample.
6. Scene changes are detected from the normalized source with threshold `10`.
   Fragments shorter than two seconds are merged with an adjacent scene.

The normal path uses one paired FFmpeg decode pass and fans the normalized frames
out to libvmaf, source-scene detection, and the C++ pHash reader. If an installed
libvmaf rejects the optional Phone model, the analyzer retries without that
informational model; official scoring is unchanged.

New catalog entries record `primary_video_stream_index`. Legacy cached entries
derive the same default-first, then highest-resolution selection from their
stored stream metadata, so adding this field does not require rebuilding HLS.
The gallery passes that global index explicitly to the standalone analyzer, and
the report records it under both `inputs` and `settings`.

Changing frame rate or scene settings creates a new settings signature, so the
gallery does not silently mix unlike reports.

### Metrics

Four primary metrics contribute to the score:

| Metric | Meaning | Score conversion | Weight |
|---|---|---:|---:|
| Standard VMAF | Netflix's general 1080p viewing model | Model score, clamped to 0–100 | 50% |
| SSIM | Structural similarity | `SSIM × 100`, clamped to 0–100 | 20% |
| PSNR | Pixel-domain signal-to-noise ratio | `(PSNR dB - 20) / 30 × 100`, clamped to 0–100 | 15% |
| pHash | 64-bit perceptual-hash similarity between the aligned source and encode frame | `100 × (1 - Hamming distance / 64)` | 15% |

The primary composite for a frame or aggregate is:

```text
0.50 × standard VMAF
+ 0.20 × normalized SSIM
+ 0.15 × normalized PSNR
+ 0.15 × normalized pHash
```

The weighted formula above is the official displayed composite. The Phone VMAF
model is recorded only as an informational view of phone-distance perception; it
never enters that composite. A temporal pHash diagnostic compares how
frame-to-frame perceptual change survives the encode; it is reported separately
and also does not enter the composite.

### Overall and scene scores

Each scene receives metric averages and its composite score. The overall score
adds sensitivity to short weak regions:

```text
70% × duration-weighted mean composite
+ 30% × mean of the lowest-scoring 10% of aligned frames
```

The displayed assessment bands are:

| Overall score | Assessment |
|---:|---|
| 90–100 | Excellent |
| 80–89.99 | Very good |
| 70–79.99 | Good |
| 55–69.99 | Fair |
| Below 55 | Poor |

These labels assess the encode against its own source. They do not rate the
source's focus, lighting, camera noise, composition, or audio quality.

## Resource and queue behavior

Quality analysis is intentionally serialized:

- one video is measured at a time;
- the worker and C++ analyzer use at most two processing threads by default;
- the systemd service has a 200% CPU quota, equivalent to two fully occupied CPU
  cores;
- the worker defers while encoding or visual content analysis is active;
- it also defers when one-minute load exceeds a nonzero `max_load`; set the
  value to `0` to rely on the explicit media-process checks and service limits;
- shared post-processing and scanner locks prevent either job from starting
  halfway through a measurement.

If visual content analysis is enabled, quality work waits until the matching
source/cache version has been categorized by the content index's current analyzer
version. If visual content analysis is disabled, quality work begins after the
encode is ready.

The queue follows persistent upload order. `items_per_run` limits how many
videos one timer activation measures. Keeping it at `1` gives the scanner,
encoding, and other work a scheduling opportunity between long measurements.
The next queue check normally occurs one second after a run. Systemd still
serializes the service, and the worker lock also protects manual invocations.
When the queue is complete, failed work is cooling down, or another media job
owns the resources, the worker adaptively polls every 30 seconds without
holding gallery locks. Active backlogs retain the one-second handoff.

## Cache behavior and deletion

A report is current only when all of these match:

- gallery video ID;
- source-aware encode cache key;
- quality worker version;
- SHA-256 of the installed C++ analyzer binary;
- measurement-settings signature.

An unchanged source with unchanged settings reuses its report indefinitely.
Replacing the source, touching it, changing analysis frame rate or scene
settings, or installing a different analyzer binary queues a new report.
Scheduling, load, retry-cooldown, CPU-thread, and prerequisite policy changes do
not invalidate metric results. A failure cooldown is tied to the measurement
signature, so a fixed or upgraded analyzer can retry immediately. Each cached
record also stores the generated artifacts' size and nanosecond modification
time; a missing or changed report is queued again. Deleting a source removes its
record from the quality index, and its generated report directory is pruned.

Pruning is deliberately narrow. It removes only report directories without a
current validated record and abandoned generated build directories older than 24
hours. Names must match the application's exact cache/build formats. Unknown
directories, files, symlinks, and recent in-progress builds are left alone.

## Standalone command-line use

The installed binary can assess any source/encode pair independently of the
gallery:

```bash
hls-quality-analyzer \
  --reference /path/to/source.mov \
  --distorted /path/to/encoded/master.m3u8 \
  --output-dir /path/to/quality-report \
  --threads 2 \
  --frame-rate 30 \
  --scene-threshold 10 \
  --min-scene-seconds 2
```

`--distorted` may be a directly decodable encoded video or an HLS master
playlist. Use `--progress-json /path/to/progress.json` when another process needs
machine-readable live progress. Run `hls-quality-analyzer --help` for the exact
options in the installed version.

The command prints a compact terminal summary and writes:

| File | Contents |
|---|---|
| `report.json` | Complete machine-readable summary, normalization details, metrics, every-frame values, and scenes |
| `frames.csv` | One row per aligned frame for spreadsheets and further analysis |
| `report.html` | Standalone visual timeline, scene table, metric summary, and assessment |

The HTML report has no server-side dependency. Treat copied reports as private:
they can identify source files and disclose detailed information about a video.

## Gallery status and reports

The authenticated library keeps quality status visible below visual-analysis
status even between timer runs. The main video cards show the overall score plus
Standard VMAF, SSIM, PSNR, and pHash when a report is ready. Each video detail
page adds a detailed quality explorer:

- overall score, Standard VMAF, normalized SSIM, normalized PSNR, pHash, Phone
  VMAF, and temporal consistency can be compared on the same 0–100 chart;
- the chart includes quality-band backgrounds, complete scene bands, and exact
  HLS-segment boundaries;
- hover, pointer, and keyboard controls inspect every individual value at one
  aligned frame and can start playback at that moment;
- range controls zoom to any scene or segment, or directly to the weakest scene;
- complete scene and HLS-segment tables can be ordered chronologically or by
  weakest score and include averages and worst deciles for every metric.

The browser first requests `dashboard.json`, a compact presentation cache beside
the normal report. The worker derives it from the existing per-frame
`report.json` and the selected media playlist. This is an inexpensive file-only
operation: it does not decode either video, rerun VMAF, change the encoded HLS,
or modify the three measurement artifacts used to validate the quality cache.
Existing reports are backfilled during normal worker runs, including idle runs.
Until that projection exists, the browser falls back to the complete report and
uses the configured nominal HLS duration for segment boundaries.

Use the instance-specific terminal command printed by the installer:

```bash
hls-gallery-quality-status-my-video-gallery --watch
hls-gallery-quality-status-my-video-gallery --watch --all --command
hls-gallery-quality-status-my-video-gallery --json
```

The service log and timer are also available directly:

```bash
journalctl -fu hls-gallery-my-video-gallery-quality.service
systemctl status hls-gallery-my-video-gallery-quality.timer
systemctl start hls-gallery-my-video-gallery-quality.service
```

Reports live below `data/quality/CACHE-KEY/`. Apache restricts that tree to the
generated dashboard, JSON, CSV, and HTML filenames. The compact
`data/quality-cards.json` projection feeds listing cards with one request while
the full `data/quality-index.json` remains private worker state. The gallery's
normal authentication still applies to the card projection and reports.

## Troubleshooting

### The installer says `libvmaf` is missing

The installed FFmpeg build does not expose the `libvmaf` filter. Run the supported
dependency installer, then check `ffmpeg -filters`. A different FFmpeg earlier in
`PATH` can also hide the packaged build.

### Status says it is waiting

This is normally resource coordination, not a failure. Check the status reason,
then inspect encoder and content-analysis services. Quality work will resume
after those jobs release their locks. If `max_load` is nonzero, the one-minute
load must also fall below that ceiling.

### A video remains in the category wait

When `content_analysis.enabled` is true, the worker requires a content record for
the exact current cache key and the analyzer/taxonomy version rendered during
installation. Check the content-analysis status and its service log. Replacing a
source or changing the taxonomy correctly makes the prior prerequisite stale;
quality work resumes after the current category analyzer publishes its new
record.

### A measurement failed

The worker records a short error and makes that cache version eligible again
after `failure_retry_seconds` (30 seconds by default). Other eligible videos
remain available to subsequent queue runs, and never-attempted videos take
priority over expired retries so a repeatedly failing file cannot block them.
Inspect:

```bash
journalctl -u hls-gallery-my-video-gallery-quality.service
hls-gallery-quality-status-my-video-gallery --json
```

Confirm both inputs are decodable with `ffprobe`, that the HLS playlist and
segments are complete, and that free space is available for temporary reports.
Use `--force` on the gallery worker only for an intentional manual remeasurement.

### Scores look unexpectedly low for HDR

Confirm the report says `HDR normalized` and that both streams have usable color
metadata. Incorrect or missing transfer/primaries metadata can make any
display-referred full-reference metric misleading. Compare the standalone HTML
timeline with a visual inspection before changing encoding settings.

### Analysis is slow

Every aligned frame is decoded and evaluated with several full-reference metrics.
Long, high-frame-rate, or HDR videos can therefore take longer than their source
duration even with a two-core allowance. Increasing `items_per_run` does not run
videos in parallel; it only keeps one timer activation busy for more videos.
