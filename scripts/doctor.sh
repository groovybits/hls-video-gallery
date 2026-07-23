#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
config_path="$repo_root/config/gallery.json"

if [[ "${1:-}" == "--config" && -n "${2:-}" ]]; then
    config_path="$2"
elif [[ $# -ne 0 ]]; then
    echo "Usage: ./scripts/doctor.sh [--config PATH]" >&2
    exit 2
fi

failed=0
check_command() {
    local command="$1"
    if command -v "$command" >/dev/null 2>&1; then
        printf 'OK   %-18s %s\n' "$command" "$(command -v "$command")"
    else
        printf 'MISS %-18s required\n' "$command"
        failed=1
    fi
}

for command in python3 ffmpeg ffprobe openssl systemctl htpasswd php; do
    check_command "$command"
done

if command -v ffmpeg >/dev/null 2>&1; then
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx264'; then
        echo "OK   FFmpeg libx264 encoder"
    else
        echo "MISS FFmpeg libx264 encoder"
        failed=1
    fi
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -Eq '[[:space:]]aac[[:space:]]'; then
        echo "OK   FFmpeg AAC encoder"
    else
        echo "MISS FFmpeg AAC encoder"
        failed=1
    fi
fi

render_dir="$(mktemp -d "${TMPDIR:-/tmp}/hls-gallery-doctor.XXXXXX")"
trap 'rm -rf -- "$render_dir"' EXIT
if python3 "$script_dir/configure.py" --config "$config_path" --output "$render_dir" >/dev/null; then
    echo "OK   gallery configuration"
else
    echo "FAIL gallery configuration"
    failed=1
fi

if command -v apachectl >/dev/null 2>&1; then
    modules="$(apachectl -M 2>/dev/null || true)"
    for module in rewrite_module headers_module auth_basic_module authn_file_module; do
        if grep -q "$module" <<<"$modules"; then
            echo "OK   Apache $module"
        else
            echo "WARN Apache $module was not reported"
        fi
    done
else
    echo "WARN apachectl was not found; verify Apache and AllowOverride manually"
fi

if ((failed)); then
    echo "Doctor found required items to fix." >&2
    exit 1
fi
echo "Doctor found no blocking dependency or configuration problems."
