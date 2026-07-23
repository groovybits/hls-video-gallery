# HLS quality analyzer

`hls-quality-analyzer` is a cache-neutral, standalone C++17 comparison tool. It
does not read or modify the gallery catalog, HLS cache, or source-media
directory unless those paths are explicitly supplied as inputs or output.

It aligns a selected global reference video stream with the first video stream
from the distorted input at a configurable frame rate (30 fps by default),
normalizes both to the same BT.709 display representation, and reports:

- standard VMAF and the VMAF phone transform when the installed model supports it;
- luma PSNR and SSIM from libvmaf's per-frame feature collectors;
- an internal 64-bit 32×32 DCT perceptual-hash similarity;
- pHash motion-profile temporal consistency;
- FFmpeg `scdet` reference-scene boundaries with short-scene merging; and
- a composite score using 50% standard VMAF, 20% normalized SSIM, 15%
  normalized PSNR, and 15% pHash similarity. The final score is 70% weighted
  mean plus 30% worst-decile mean.

PSNR is normalized with `clamp((dB - 20) / 30 × 100)`, while SSIM is normalized
with `clamp(SSIM × 100)`. Phone VMAF and temporal pHash consistency are
informational diagnostics and do not change the composite.

## Dependencies

- A C++17 compiler and POSIX process APIs (Linux or macOS)
- `ffmpeg` and `ffprobe` in `PATH`
- An FFmpeg build with `libvmaf`, `scdet`, `colorspace`, `zscale`, and `tonemap`

No FFmpeg development headers or third-party C++ libraries are required. The
tool executes FFmpeg directly with `fork`/`execvp`; filenames are never
interpolated into a shell command.

## Build

With Make:

```bash
make
```

Or with CMake:

```bash
cmake -S . -B build
cmake --build build --parallel
```

## Run

```bash
./hls-quality-analyzer \
  --reference /path/to/source.mov \
  --reference-stream-index 0 \
  --distorted /path/to/encoded.mp4 \
  --output-dir /path/to/quality-report \
  --threads 2 \
  --frame-rate 30 \
  --scene-threshold 10 \
  --min-scene-seconds 2
```

`--reference-stream-index` is the zero-based global stream index reported by
`ffprobe`, not the video-only ordinal. If omitted, the standalone tool selects
the first video stream (`v:0`), which preserves the simple command-line
behavior even when stream `0` is audio. The gallery worker supplies the
encoder-selected global index automatically, which keeps files with attached
pictures, proxy tracks, or alternate camera tracks aligned to the stream that
was actually encoded. The selected index is recorded in the JSON report;
standalone automatic selection is recorded as `null`.

For an interlaced source whose encoded comparison is progressive, add:

```bash
  --deinterlace-reference
```

This runs `yadif=deint=interlaced` on the reference only, before frame-rate
alignment. Progressive reference frames pass through unchanged, and the
encoded comparison is never deinterlaced. The JSON settings and preprocessing
sections record whether this option was used.

Optional live progress:

```bash
  --progress-json /path/to/quality-analysis-progress.json
```

The progress file is replaced atomically and includes the current phase,
elapsed and processed time, percent, processing FPS/speed, ETA, frame and scene
counts, and a sanitized FFmpeg argument vector.

Outputs are also replaced atomically. Each artifact is written to a randomized,
exclusively created mode-0600 temporary file in the destination directory,
synced, and renamed over its prior version. A failed probe or analysis leaves
the last completed report artifacts in place. `report.json` is published last
as the report-set commit marker.

- `report.json`: stable schema version 1, summary and quality band, a compact
  minimum-preserving timeline, every aligned frame, settings, capabilities,
  overall metrics, warnings, and per-scene results
- `frames.csv`: aligned per-frame metrics
- `report.html`: self-contained human-readable report

The shorter aligned input determines the analyzed duration. The reference is
scaled to the distorted encode's display dimensions. FFmpeg's normal
autorotation remains active, and ffprobe rotation metadata is used to determine
both display dimensions.

Frame-rate selection is performed while each decoded stream still has its
native input time base. The selected frames are then converted to FFmpeg's
common `AVTB` time base and rebased to a zero origin. Keeping the native time
base through the `fps` filter prevents periodic neighboring-frame comparisons
when, for example, a 60/59.94 fps source is compared with a 30 fps encode.

## Scene and temporal definitions

Every normalized reference and distorted frame is reduced to a 64-bit DCT
pHash. Cross-stream similarity is `100 × (1 - HammingDistance / 64)`.
Scene boundaries come only from FFmpeg `scdet` scores on the normalized
reference. Scenes shorter than `--min-scene-seconds` are repeatedly merged
across their weaker neighboring boundary.

Temporal consistency compares the adjacent pHash-change magnitude in the
reference and distorted streams:

```text
100 - abs(reference change - distorted change)
```

This flags dropped, duplicated, reordered, and structurally inconsistent
motion without borrowing scene boundaries from the distorted video.

## HDR and HLG normalization

SDR is color-managed to BT.709 TV range. For a PQ or HLG reference, its tagged
input interpretation selects one pair-wide transform: both streams are
converted to linear light with `zscale`, tone-mapped with the same Mobius
display transform, and converted to BT.709 TV range. This keeps a tag-stripped
encode from silently taking a different display path from its reference.

The reference is scaled to the distorted encoded display dimensions; the
encoded video is not enlarged back to the source dimensions.

If an HDR/HLG input is supplied to an FFmpeg build without `zscale`, the tool
stops with a clear error rather than silently comparing mismatched transfer
functions.

## Smoke test

```bash
make test
```

The focused smoke test creates a synthetic reference and a real local HLS
master/variant with relative segments in a temporary directory. It verifies
the score math and JSON schema, exact global stream selection, 60-to-30 fps
frame alignment, optional reference-only deinterlacing, and proves
hostile-looking filenames remain inert argv data.
