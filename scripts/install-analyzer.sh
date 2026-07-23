#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "Analyzer installation failed at line $LINENO" >&2' ERR

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
config_path="$repo_root/config/gallery.json"

usage() {
    echo "Usage: sudo ./scripts/install-analyzer.sh [--config PATH]"
}

while (($#)); do
    case "$1" in
        --config)
            [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
            config_path="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer as root." >&2
    exit 1
fi

config_path="$(cd -- "$(dirname -- "$config_path")" && pwd)/$(basename -- "$config_path")"
build_root="$repo_root/build/analyzer"
python3 "$script_dir/configure.py" --config "$config_path" --output "$build_root"
value() {
    python3 "$script_dir/json-value.py" "$build_root/install.json" "$1"
}

instance_id="$(value instance_id)"
target="$(value document_root)"
site_owner="$(value owner)"
private_dir="$(value private_dir)"
site_group="$(id -gn "$site_owner")"
venv="/opt/hls-video-gallery-analyzer"
cache="$private_dir/model-cache"

[[ -f "$target/.hls-video-gallery" ]] || {
    echo "Install the gallery before installing its optional analyzer: $target" >&2
    exit 1
}

if [[ ! -x "$venv/bin/python" ]]; then
    python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$venv/bin/python" -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
"$venv/bin/python" -m pip install open-clip-torch==3.3.0 pillow

install -d -m 0750 -o "$site_owner" -g "$site_group" "$cache"
runuser -u "$site_owner" -- env HF_HOME="$cache" \
    "$venv/bin/python" -c 'import open_clip; open_clip.create_model_and_transforms("MobileCLIP2-S0", pretrained="dfndr2b", device="cpu"); print("MobileCLIP2-S0 is ready")'

service="hls-gallery-${instance_id}-analyzer.service"
timer="hls-gallery-${instance_id}-analyzer.timer"
install -m 0644 -o root -g root "$build_root/systemd/hls-gallery-analyzer.service" "/etc/systemd/system/$service"
install -m 0644 -o root -g root "$build_root/systemd/hls-gallery-analyzer.timer" "/etc/systemd/system/$timer"
systemctl daemon-reload
systemctl enable --now "$timer"

echo "Optional visual tagging is installed for $instance_id."
echo "Monitor it with: journalctl -fu $service"
