#!/usr/bin/env bash
set -Eeuo pipefail

tool_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
renderer="$tool_dir/hls-quality-report-renderer"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/hls-quality-renderer-test.XXXXXX")"
cleanup() {
    if [[ "${HLS_QUALITY_TEST_KEEP_TEMP:-0}" == "1" ]]; then
        echo "Kept renderer fixtures at $temporary"
        return
    fi
    rm -rf -- "$temporary"
}
trap cleanup EXIT

if [[ ! -x "$renderer" ]]; then
    echo "Build $renderer first (make)." >&2
    exit 2
fi

cat >"$temporary/report.json" <<'JSON'
{
  "schema_version": 1,
  "analyzer_version": "1.1.1",
  "generated_at": "2026-07-23T12:00:00Z",
  "inputs": {
    "reference": "source </script><img src=x onerror=alert(1)>.mov",
    "distorted": "encoded & comparison.m3u8"
  },
  "summary": {
    "score": 78.5,
    "band": "Good",
    "vmaf_standard": 82,
    "vmaf_phone": 87,
    "ssim": 0.96,
    "ssim_normalized": 96,
    "psnr_y": 38,
    "psnr_normalized": 60,
    "phash_similarity": 91,
    "temporal_consistency": 89
  },
  "video": {"duration_seconds": 12, "frames_analyzed": 4},
  "frames": [
    {"frame": 0, "time_seconds": 0, "scene": 1, "composite": 92, "vmaf_standard": 94, "vmaf_phone": 96, "ssim": 0.99, "ssim_normalized": 99, "psnr_y": 45, "psnr_normalized": 83.33, "phash_similarity": 97, "temporal_consistency": 98},
    {"frame": 1, "time_seconds": 3, "scene": 1, "composite": 84, "vmaf_standard": 87, "vmaf_phone": 91, "ssim": 0.97, "ssim_normalized": 97, "psnr_y": 40, "psnr_normalized": 66.67, "phash_similarity": 94, "temporal_consistency": 93},
    {"frame": 2, "time_seconds": 6, "scene": 2, "composite": 68, "vmaf_standard": 72, "vmaf_phone": 78, "ssim": 0.91, "ssim_normalized": 91, "psnr_y": 32, "psnr_normalized": 40, "phash_similarity": 84, "temporal_consistency": 81},
    {"frame": 3, "time_seconds": 9, "scene": 2, "composite": 55, "vmaf_standard": 60, "vmaf_phone": 67, "ssim": 0.85, "ssim_normalized": 85, "psnr_y": 27, "psnr_normalized": 23.33, "phash_similarity": 76, "temporal_consistency": 70}
  ],
  "scenes": [
    {"index": 1, "start_seconds": 0, "end_seconds": 6, "frame_count": 2, "score": 86, "band": "Very good"},
    {"index": 2, "start_seconds": 6, "end_seconds": 12, "frame_count": 2, "score": 59, "band": "Fair"}
  ],
  "warnings": ["Hostile-looking text stays inert: </script><img src=x>"]
}
JSON

cat >"$temporary/dashboard.json" <<'JSON'
{
  "schema_version": 1,
  "generated_at": "2026-07-23T12:01:00Z",
  "source": {"analyzer_version": "1.1.1", "report_generated_at": "2026-07-23T12:00:00Z"},
  "report_metadata": {
    "analyzer_version": "1.1.1",
    "generated_at": "2026-07-23T12:00:00Z",
    "preprocessing": {"reference_deinterlace": true, "reference_deinterlace_filter": "yadif=deint=interlaced"},
    "warnings": ["Source and output durations differ."]
  },
  "summary": {
    "score": 78.5,
    "band": "Good",
    "vmaf_standard": 82,
    "vmaf_phone": 87,
    "ssim": 0.96,
    "ssim_normalized": 96,
    "psnr_y": 38,
    "psnr_normalized": 60,
    "phash_similarity": 91,
    "temporal_consistency": 89
  },
  "video": {"duration_seconds": 12, "frames_analyzed": 4},
  "overview": {
    "sample_method": "metric_min_max_envelope",
    "points": [
      {"frame": 0, "time_seconds": 0, "scene_index": 1, "segment_index": 0, "composite": 92, "vmaf_standard": 94, "vmaf_phone": 96, "ssim": 0.99, "ssim_normalized": 99, "psnr_y": 45, "psnr_normalized": 83.33, "phash_similarity": 97, "temporal_consistency": 98},
      {"frame": 1, "time_seconds": 3, "scene_index": 1, "segment_index": 0, "composite": 84, "vmaf_standard": 87, "vmaf_phone": 91, "ssim": 0.97, "ssim_normalized": 97, "psnr_y": 40, "psnr_normalized": 66.67, "phash_similarity": 94, "temporal_consistency": 93},
      {"frame": 2, "time_seconds": 6, "scene_index": 2, "segment_index": 1, "composite": 68, "vmaf_standard": 72, "vmaf_phone": 78, "ssim": 0.91, "ssim_normalized": 91, "psnr_y": 32, "psnr_normalized": 40, "phash_similarity": 84, "temporal_consistency": 81},
      {"frame": 3, "time_seconds": 9, "scene_index": 2, "segment_index": 1, "composite": 55, "vmaf_standard": 60, "vmaf_phone": 67, "ssim": 0.85, "ssim_normalized": 85, "psnr_y": 27, "psnr_normalized": 23.33, "phash_similarity": 76, "temporal_consistency": 70}
    ]
  },
  "scenes": [
    {"index": 1, "start_seconds": 0, "end_seconds": 6, "frame_count": 2, "score": 86, "band": "Very good", "metrics": {"composite": {"mean": 88, "worst_decile": 84}, "vmaf_standard": {"mean": 90.5, "worst_decile": 87}, "vmaf_phone": {"mean": 93.5, "worst_decile": 91}, "ssim": {"mean": 0.98, "worst_decile": 0.97}, "ssim_normalized": {"mean": 98, "worst_decile": 97}, "psnr_y": {"mean": 42.5, "worst_decile": 40}, "psnr_normalized": {"mean": 75, "worst_decile": 66.67}, "phash_similarity": {"mean": 95.5, "worst_decile": 94}, "temporal_consistency": {"mean": 95.5, "worst_decile": 93}}},
    {"index": 2, "start_seconds": 6, "end_seconds": 12, "frame_count": 2, "score": 59, "band": "Fair", "metrics": {"composite": {"mean": 61.5, "worst_decile": 55}, "vmaf_standard": {"mean": 66, "worst_decile": 60}, "vmaf_phone": {"mean": 72.5, "worst_decile": 67}, "ssim": {"mean": 0.88, "worst_decile": 0.85}, "ssim_normalized": {"mean": 88, "worst_decile": 85}, "psnr_y": {"mean": 29.5, "worst_decile": 27}, "psnr_normalized": {"mean": 31.665, "worst_decile": 23.33}, "phash_similarity": {"mean": 80, "worst_decile": 76}, "temporal_consistency": {"mean": 75.5, "worst_decile": 70}}}
  ],
  "hls_segments": [
    {"index": 0, "sequence": 20, "uri": "seg-000020.ts", "start_seconds": 0, "end_seconds": 6, "duration_seconds": 6, "size_bytes": 1200000, "bitrate_bps": 1600000, "scene_indexes": [1], "score": 86, "band": "Very good", "metrics": {"composite": {"mean": 88, "worst_decile": 84}, "vmaf_standard": {"mean": 90.5, "worst_decile": 87}}},
    {"index": 1, "sequence": 21, "uri": "seg-000021.ts", "start_seconds": 6, "end_seconds": 12, "duration_seconds": 6, "size_bytes": 900000, "bitrate_bps": 1200000, "scene_indexes": [2], "score": 59, "band": "Fair", "metrics": {"composite": {"mean": 61.5, "worst_decile": 55}, "vmaf_standard": {"mean": 66, "worst_decile": 60}}}
  ]
}
JSON

fingerprint="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
hostile_title='Title </script><img src=x onerror=alert(1)> & U+2028'
"$renderer" \
    --report-json "$temporary/report.json" \
    --dashboard-json "$temporary/dashboard.json" \
    --output "$temporary/report.html" \
    --fingerprint "$fingerprint" \
    --title "$hostile_title" >/dev/null

python3 - \
    "$temporary/report.html" \
    "$temporary/report-script.js" \
    "$tool_dir/../../site/data/.htaccess" <<'PY'
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys

report_path, script_path, htaccess_path = map(Path, sys.argv[1:])
text = report_path.read_text(encoding="utf-8")

data_match = re.search(
    r'<script id="quality-report-data" type="application/json">(.*?)</script>',
    text,
    re.S,
)
assert data_match
payload = json.loads(data_match.group(1))
assert payload["report"] is None
assert payload["title"] == "Title </script><img src=x onerror=alert(1)> & U+2028"
assert len(payload["dashboard"]["scenes"]) == 2
assert len(payload["dashboard"]["hls_segments"]) == 2
assert payload["dashboard"]["hls_segments"][1]["sequence"] == 21
assert payload["dashboard"]["report_metadata"]["preprocessing"]["reference_deinterlace"]
assert payload["dashboard"]["report_metadata"]["warnings"] == [
    "Source and output durations differ."
]
assert "<img src=x onerror=alert(1)>" not in text
assert "</script><img" not in text

scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", text, re.S)
assert len(scripts) == 2
executable = scripts[1]
script_path.write_text(executable, encoding="utf-8")

required = (
    "Standard VMAF",
    "Phone VMAF",
    "SSIM score",
    "PSNR score",
    "pHash",
    "Temporal pHash",
    "Weakest scene",
    "Exact HLS segment details",
    "Scene details",
)
for label in required:
    assert label in text, label
assert 'name="quality-report-renderer" content="2"' in text[:8192]
assert (
    'name="quality-report-fingerprint" '
    'content="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'
) in text[:8192]

class ResourceParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        assert tag not in {"iframe", "object", "embed"}
        if tag == "script":
            assert "src" not in attributes
        if tag == "link":
            raise AssertionError("external stylesheet/link is not self-contained")
        for attribute in ("src", "href", "poster"):
            value = attributes.get(attribute, "")
            assert not value.startswith(("http://", "https://", "//"))

ResourceParser().feed(text)

csp_hash = "sha256-" + base64.b64encode(
    hashlib.sha256(executable.encode("utf-8")).digest()
).decode("ascii")
assert csp_hash in htaccess_path.read_text(encoding="utf-8"), csp_hash
assert 'http-equiv="Content-Security-Policy"' in text
assert csp_hash in text
PY

if command -v node >/dev/null 2>&1; then
    node --check "$temporary/report-script.js"
fi

"$renderer" \
    --report-json "$temporary/report.json" \
    --output "$temporary/direct.html" \
    --fingerprint "$fingerprint" >/dev/null
python3 - "$temporary/direct.html" <<'PY'
import json
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(
    r'<script id="quality-report-data" type="application/json">(.*?)</script>',
    text,
    re.S,
)
payload = json.loads(match.group(1))
assert payload["dashboard"] is None
assert len(payload["report"]["frames"]) == 4
assert "Nominal HLS segment details" in text
PY

printf '{"broken":' >"$temporary/invalid.json"
if "$renderer" \
    --report-json "$temporary/invalid.json" \
    --output "$temporary/invalid.html" >/dev/null 2>&1; then
    echo "Expected invalid JSON rendering to fail" >&2
    exit 1
fi
test ! -e "$temporary/invalid.html"

echo "Detailed report renderer tests passed."
