#!/usr/bin/env bash
set -Eeuo pipefail

tool_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
analyzer="$tool_dir/hls-quality-analyzer"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/hls-quality-analyzer-test.XXXXXX")"
cleanup() {
    rm -rf -- "$temporary"
}
trap cleanup EXIT

if [[ ! -x "$analyzer" ]]; then
    echo "Build $analyzer first (make)." >&2
    exit 2
fi
if ! quality_filter_list="$(ffmpeg -hide_banner -filters 2>&1)"; then
    echo "Cannot inspect the installed FFmpeg filters" >&2
    exit 1
fi
if ! grep -q ' libvmaf ' <<<"$quality_filter_list"; then
    echo "SKIP: installed FFmpeg has no libvmaf filter"
    exit 0
fi

ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=320x180:rate=30:duration=2" \
    -f lavfi -i "smptebars=size=320x180:rate=30:duration=2" \
    -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]" \
    -map "[v]" -c:v ffv1 "$temporary/reference.mkv"

mkdir -p "$temporary/hls/v0"
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$temporary/reference.mkv" \
    -vf "scale=256:144,eq=contrast=0.96:saturation=0.95" \
    -c:v libx264 -preset veryfast -crf 31 -b:v 500k -maxrate 500k \
    -bufsize 1000k -pix_fmt yuv420p \
    -an -f hls -hls_time 1 -hls_playlist_type vod -hls_flags independent_segments \
    -hls_segment_filename "$temporary/hls/v%v/seg-%03d.ts" \
    -master_pl_name master.m3u8 -var_stream_map "v:0" \
    "$temporary/hls/v%v/index.m3u8"

"$analyzer" \
    --reference "$temporary/reference.mkv" \
    --distorted "$temporary/hls/master.m3u8" \
    --output-dir "$temporary/report" \
    --progress-json "$temporary/progress.json" \
    --threads 2 \
    --scene-threshold 8 \
    --min-scene-seconds 1

test -s "$temporary/report/report.json"
test -s "$temporary/report/frames.csv"
test -s "$temporary/report/report.html"
grep -q '"schema_version": 1' "$temporary/report/report.json"
grep -q '"config_version": "quality-composite-v1"' "$temporary/report/report.json"
grep -q '"summary": {' "$temporary/report/report.json"
grep -q '"timeline": \[' "$temporary/report/report.json"
grep -q '"frames": \[' "$temporary/report/report.json"
grep -q '"scenes": \[' "$temporary/report/report.json"
grep -q '^frame,time_seconds,scene,vmaf_standard' "$temporary/report/frames.csv"
grep -q '"active": false' "$temporary/progress.json"
grep -q '"phase": "complete"' "$temporary/progress.json"
grep -q 'name="quality-report-renderer" content="2"' "$temporary/report/report.html"
grep -q 'id="quality-report-data"' "$temporary/report/report.html"
grep -q 'Quality explorer' "$temporary/report/report.html"
python3 -m json.tool "$temporary/report/report.json" >/dev/null
python3 -m json.tool "$temporary/progress.json" >/dev/null
python3 - "$temporary/report/report.json" <<'PY'
import json
import math
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)

frames = report["frames"]
assert len(frames) == report["video"]["frames_analyzed"] == 120
assert 1 <= len(report["timeline"]) <= 1000
scene_frames = sum(scene["frame_count"] for scene in report["scenes"])
assert scene_frames == len(frames), (scene_frames, len(frames), report["scenes"])
assert report["video"]["width"] == 256 and report["video"]["height"] == 144
assert report["summary"]["band"] in {"Excellent", "Very good", "Good", "Fair", "Poor"}
assert report["settings"]["deinterlace_reference"] is False
assert report["preprocessing"] == {
    "frame_alignment": "fps_on_native_time_base_then_avtb_zero_origin",
    "reference_deinterlace": False,
    "reference_deinterlace_filter": None,
    "distorted_deinterlace": False,
}

scores = []
for frame in frames:
    expected = (
        0.50 * frame["vmaf_standard"]
        + 0.20 * frame["ssim_normalized"]
        + 0.15 * frame["psnr_normalized"]
        + 0.15 * frame["phash_similarity"]
    )
    assert abs(frame["composite"] - expected) < 1e-4
    scores.append(frame["composite"])

weighted_mean = sum(scores) / len(scores)
worst_count = max(1, math.ceil(len(scores) * 0.10))
worst = sum(sorted(scores)[:worst_count]) / worst_count
expected_overall = 0.70 * weighted_mean + 0.30 * worst
assert abs(report["summary"]["score"] - expected_overall) < 1e-3
PY

"$analyzer" --version | grep -q '^hls-quality-analyzer 1\.1\.1$'

# Preserve each decoder's native time base until fps has selected its samples.
# This specifically covers a 60 fps source compared with its 30 fps derivative,
# where premature AVTB conversion can periodically select the neighboring
# reference frame.
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=320x180:rate=60:duration=2" \
    -c:v ffv1 "$temporary/alignment-reference.mkv"
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$temporary/alignment-reference.mkv" \
    -vf "fps=fps=30:round=near" -c:v ffv1 \
    "$temporary/alignment-distorted.mkv"
"$analyzer" \
    --reference "$temporary/alignment-reference.mkv" \
    --distorted "$temporary/alignment-distorted.mkv" \
    --output-dir "$temporary/alignment-report" \
    --threads 2 \
    --frame-rate 30 \
    --scene-threshold 10 \
    --min-scene-seconds 1 >/dev/null
python3 - "$temporary/alignment-report/report.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["video"]["reference_source_fps"] == 60
assert report["video"]["distorted_source_fps"] == 30
assert report["video"]["frames_analyzed"] == 60
assert report["summary"]["score"] > 99
assert min(frame["phash_similarity"] for frame in report["frames"]) > 99.9
assert (
    report["preprocessing"]["frame_alignment"]
    == "fps_on_native_time_base_then_avtb_zero_origin"
)
PY

# Select the same global input-stream index used by the encoder. Stream 0 is
# deliberately unlike the encoded stream so ignoring the option fails loudly.
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=red:size=320x180:rate=10:duration=1" \
    -f lavfi -i "testsrc2=size=320x180:rate=10:duration=1" \
    -map 0:v -map 1:v -c:v ffv1 \
    -disposition:v:0 0 -disposition:v:1 default \
    "$temporary/multistream-reference.mkv"
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$temporary/multistream-reference.mkv" \
    -map 0:1 -c:v ffv1 "$temporary/multistream-distorted.mkv"
"$analyzer" \
    --reference "$temporary/multistream-reference.mkv" \
    --reference-stream-index 1 \
    --distorted "$temporary/multistream-distorted.mkv" \
    --output-dir "$temporary/multistream-report" \
    --threads 2 \
    --frame-rate 10 \
    --scene-threshold 10 \
    --min-scene-seconds 1 >/dev/null
python3 - "$temporary/multistream-report/report.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["inputs"]["reference_stream_index"] == 1
assert report["settings"]["reference_stream_index"] == 1
assert report["summary"]["score"] > 99
assert min(frame["phash_similarity"] for frame in report["frames"]) > 99.9
PY

# Omitting the option remains video-relative for standalone use, even when
# global stream 0 is audio.
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=1" \
    -f lavfi -i "testsrc2=size=320x180:rate=10:duration=1" \
    -map 0:a -map 1:v -c:a pcm_s16le -c:v ffv1 \
    "$temporary/audio-first-reference.mkv"
ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$temporary/audio-first-reference.mkv" \
    -map 0:v:0 -c:v ffv1 "$temporary/audio-first-distorted.mkv"
"$analyzer" \
    --reference "$temporary/audio-first-reference.mkv" \
    --distorted "$temporary/audio-first-distorted.mkv" \
    --output-dir "$temporary/audio-first-report" \
    --threads 2 \
    --frame-rate 10 \
    --scene-threshold 10 \
    --min-scene-seconds 1 >/dev/null
python3 - "$temporary/audio-first-report/report.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["inputs"]["reference_stream_index"] is None
assert report["settings"]["reference_stream_index"] is None
assert report["summary"]["score"] > 99
PY

# Interlaced references can be conditionally deinterlaced before fps alignment
# without filtering the already-progressive encoded comparison.
if grep -q ' yadif ' <<<"$quality_filter_list" &&
   grep -q ' tinterlace ' <<<"$quality_filter_list"; then
    ffmpeg -nostdin -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=size=320x180:rate=60:duration=2" \
        -vf "tinterlace=mode=interleave_top" -c:v ffv1 \
        "$temporary/interlaced-reference.mkv"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
        -i "$temporary/interlaced-reference.mkv" \
        -vf "yadif=deint=interlaced,fps=fps=30:round=near" -c:v ffv1 \
        "$temporary/deinterlaced-distorted.mkv"
    "$analyzer" \
        --reference "$temporary/interlaced-reference.mkv" \
        --distorted "$temporary/deinterlaced-distorted.mkv" \
        --output-dir "$temporary/deinterlace-report" \
        --threads 2 \
        --frame-rate 30 \
        --scene-threshold 10 \
        --min-scene-seconds 1 \
        --deinterlace-reference >/dev/null
    python3 - "$temporary/deinterlace-report/report.json" <<'PY'
import json
import re
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["settings"]["deinterlace_reference"] is True
assert report["preprocessing"]["reference_deinterlace"] is True
assert report["preprocessing"]["reference_deinterlace_filter"] == "yadif=deint=interlaced"
assert report["preprocessing"]["distorted_deinterlace"] is False
assert report["video"]["frames_analyzed"] == 60
assert report["summary"]["score"] > 99
with open(
    sys.argv[1].replace("report.json", "report.html"), encoding="utf-8"
) as handle:
    html = handle.read()
payload = re.search(
    r'<script id="quality-report-data" type="application/json">(.*?)</script>',
    html,
    re.S,
)
assert payload
embedded = json.loads(payload.group(1))
assert embedded["report"]["preprocessing"]["reference_deinterlace"] is True
PY
else
    echo "SKIP: installed FFmpeg lacks yadif or tinterlace for the interlace smoke"
fi

# Prove that hostile-looking paths remain argv data and are never evaluated by
# a shell. A lower configured frame rate also exercises that optional contract.
hostile_reference="$temporary/weird \$(touch INJECTED) ; ' reference.mkv"
cp "$temporary/reference.mkv" "$hostile_reference"
mkdir -p "$temporary/injection-report"
printf 'symlink target must remain untouched\n' >"$temporary/symlink-victim"
(
    cd "$temporary"
    exec "$analyzer" \
        --reference "$hostile_reference" \
        --distorted "$temporary/hls/master.m3u8" \
        --output-dir "$temporary/injection-report" \
        --threads 2 \
        --frame-rate 15 \
        --scene-threshold 8 \
        --min-scene-seconds 1 >/dev/null
) &
analyzer_pid=$!
ln -s "$temporary/symlink-victim" \
    "$temporary/injection-report/.report.json.${analyzer_pid}.tmp"
ln -s "$temporary/symlink-victim" \
    "$temporary/injection-report/.frames.csv.${analyzer_pid}.tmp"
ln -s "$temporary/symlink-victim" \
    "$temporary/injection-report/.report.html.${analyzer_pid}.tmp"
wait "$analyzer_pid"
test ! -e "$temporary/INJECTED"
test "$(cat "$temporary/symlink-victim")" = 'symlink target must remain untouched'
python3 - "$temporary/injection-report/report.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["settings"]["fps"] == 15
assert report["video"]["frames_analyzed"] == 60
assert "/" not in report["inputs"]["reference"]
PY

# A failed replacement must preserve the last complete report rather than
# deleting it before input probing or analysis succeeds.
mkdir -p "$temporary/retained-report"
printf 'previous report json\n' >"$temporary/retained-report/report.json"
printf 'previous frames csv\n' >"$temporary/retained-report/frames.csv"
printf 'previous report html\n' >"$temporary/retained-report/report.html"
printf 'not a video\n' >"$temporary/invalid-video"
if "$analyzer" \
    --reference "$temporary/reference.mkv" \
    --distorted "$temporary/invalid-video" \
    --output-dir "$temporary/retained-report" >/dev/null 2>&1; then
    echo "Expected invalid input analysis to fail" >&2
    exit 1
fi
grep -qx 'previous report json' "$temporary/retained-report/report.json"
grep -qx 'previous frames csv' "$temporary/retained-report/frames.csv"
grep -qx 'previous report html' "$temporary/retained-report/report.html"

if grep -q ' zscale ' <<<"$quality_filter_list" &&
   grep -q ' tonemap ' <<<"$quality_filter_list"; then
    mkdir -p "$temporary/hdr-hls/v0"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=size=128x72:rate=5:duration=1" \
        -vf "setparams=range=tv:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc" \
        -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
        -bsf:v "h264_metadata=colour_primaries=9:transfer_characteristics=18:matrix_coefficients=9" \
        "$temporary/hdr-reference.mkv"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
        -i "$temporary/hdr-reference.mkv" \
        -vf "eq=contrast=0.98,setparams=range=tv:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc" \
        -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p \
        -bsf:v "h264_metadata=colour_primaries=9:transfer_characteristics=18:matrix_coefficients=9" \
        -an -f hls -hls_time 1 -hls_playlist_type vod \
        -hls_segment_filename "$temporary/hdr-hls/v0/seg-%03d.ts" \
        -master_pl_name master.m3u8 -var_stream_map "v:0" \
        "$temporary/hdr-hls/v0/index.m3u8"
    "$analyzer" \
        --reference "$temporary/hdr-reference.mkv" \
        --distorted "$temporary/hdr-hls/v0/index.m3u8" \
        --output-dir "$temporary/hdr-report" \
        --threads 2 \
        --frame-rate 5 \
        --scene-threshold 10 \
        --min-scene-seconds 1 >/dev/null
    python3 - "$temporary/hdr-report/report.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["hdr_normalized"] is True
assert report["video"]["frames_analyzed"] == 5
assert "HDR/HLG" in report["normalization"]["reference"]
PY
else
    echo "SKIP: installed FFmpeg lacks zscale or tonemap for the HDR smoke"
fi

echo "quality analyzer smoke test passed"
