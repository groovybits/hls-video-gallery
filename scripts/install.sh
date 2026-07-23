#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "Installation failed at line $LINENO" >&2' ERR

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
config_path="$repo_root/config/gallery.json"
render_only=false
start_services=true

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/install.sh [options]

Options:
  --config PATH      Gallery JSON file (default: config/gallery.json)
  --render-only      Validate and render build/rendered without installing
  --no-start         Install files and units without starting services
  -h, --help         Show this help
USAGE
}

while (($#)); do
    case "$1" in
        --config)
            [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
            config_path="$2"
            shift 2
            ;;
        --render-only)
            render_only=true
            shift
            ;;
        --no-start)
            start_services=false
            shift
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

config_path="$(cd -- "$(dirname -- "$config_path")" 2>/dev/null && pwd)/$(basename -- "$config_path")"
build_root="$repo_root/build/rendered"
python3 "$script_dir/configure.py" --config "$config_path" --output "$build_root"

if $render_only; then
    echo "Rendered site: $build_root/site"
    echo "Rendered units: $build_root/systemd"
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "Run the installer as root, or use --render-only for a local configuration check." >&2
    exit 1
fi

for command in python3 ffmpeg ffprobe openssl systemctl install id getent; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command is missing: $command" >&2
        echo "See scripts/install-dependencies.sh and README.md." >&2
        exit 1
    }
done

value() {
    python3 "$script_dir/json-value.py" "$build_root/install.json" "$1"
}

instance_id="$(value instance_id)"
target="$(value document_root)"
site_owner="$(value owner)"
private_dir="$(value private_dir)"
basic_auth="$(value basic_auth)"
share_links="$(value public_share_links)"
cdn_provider="$(value cdn_provider)"
cdn_config="$(value cdn_config)"
analysis_enabled="$(value content_analysis_enabled)"
app_version="$(value app_version)"
pipeline_version="$(value pipeline_version)"
config_sha="$(value config_sha256)"
encoding_sha="$(value encoding_sha256)"

if ! id "$site_owner" >/dev/null 2>&1; then
    echo "Configured install.owner does not exist: $site_owner" >&2
    exit 1
fi
site_group="$(id -gn "$site_owner")"
marker="$target/.hls-video-gallery"

if [[ -d "$target" ]] && [[ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] && [[ ! -f "$marker" ]]; then
    echo "Refusing to overwrite a nonempty directory not created by HLS Video Gallery:" >&2
    echo "$target" >&2
    exit 1
fi

previous_encoding_sha=""
if [[ -f "$marker" ]]; then
    previous_instance="$(awk -F': ' '$1 == "Instance-ID" { print $2; exit }' "$marker" 2>/dev/null || true)"
    previous_encoding_sha="$(awk -F': ' '$1 == "Encoding-SHA256" { print $2; exit }' "$marker" 2>/dev/null || true)"
    if [[ -n "$previous_instance" && "$previous_instance" != "$instance_id" ]]; then
        echo "The target belongs to instance '$previous_instance', not '$instance_id'." >&2
        exit 1
    fi
fi

install -d -m 0755 -o "$site_owner" -g "$site_group" "$target"
install -d -m 0750 -o root -g "$site_group" "$private_dir"

users_file="$(dirname -- "$config_path")/users.txt"
htpasswd_path="$private_dir/users.htpasswd"
if [[ "$basic_auth" == "true" ]]; then
    command -v htpasswd >/dev/null 2>&1 || {
        echo "Basic Auth is enabled but htpasswd is missing (httpd-tools or apache2-utils)." >&2
        exit 1
    }
    if [[ -f "$users_file" ]]; then
        htpasswd_tmp="$(mktemp)"
        built_user=false
        cleanup_htpasswd() {
            rm -f -- "$htpasswd_tmp"
        }
        trap cleanup_htpasswd RETURN
        while IFS=: read -r username password; do
            [[ -n "$username$password" ]] || continue
            [[ "$username" == \#* ]] && continue
            if [[ ! "$username" =~ ^[A-Za-z0-9_.-]{1,64}$ || -z "$password" ]]; then
                echo "Invalid entry in $users_file: usernames use letters/numbers/._- and passwords cannot be empty." >&2
                exit 1
            fi
            if $built_user; then
                printf '%s\n' "$password" | htpasswd -iB "$htpasswd_tmp" "$username" >/dev/null
            else
                printf '%s\n' "$password" | htpasswd -ciB "$htpasswd_tmp" "$username" >/dev/null
                built_user=true
            fi
        done <"$users_file"
        if ! $built_user; then
            echo "No usable users were found in $users_file" >&2
            exit 1
        fi
        install -m 0640 -o root -g "$site_group" "$htpasswd_tmp" "$htpasswd_path"
        rm -f -- "$htpasswd_tmp"
        trap - RETURN
    elif [[ ! -f "$htpasswd_path" ]]; then
        echo "Basic Auth is enabled, but neither $users_file nor an existing $htpasswd_path exists." >&2
        echo "Copy config/users.example to config/users.txt and set at least one strong password." >&2
        exit 1
    fi
fi

if [[ "$share_links" == "true" && ! -f "$private_dir/share.key" ]]; then
    share_key_tmp="$(mktemp)"
    openssl rand -hex 32 >"$share_key_tmp"
    install -m 0640 -o root -g "$site_group" "$share_key_tmp" "$private_dir/share.key"
    rm -f -- "$share_key_tmp"
fi

if [[ "$cdn_provider" == "bunny" ]]; then
    [[ -f "$cdn_config" ]] || { echo "Bunny configuration is missing: $cdn_config" >&2; exit 1; }
    for key in BUNNY_STORAGE_ZONE BUNNY_STORAGE_PASSWORD BUNNY_STORAGE_ENDPOINT BUNNY_CDN_HOST BUNNY_TOKEN_KEY BUNNY_CORS_READY; do
        grep -q "^${key}=" "$cdn_config" || { echo "Bunny configuration is missing $key" >&2; exit 1; }
    done
    install -m 0600 -o root -g root "$cdn_config" "$private_dir/bunny-sync.env"
    signing_tmp="$(mktemp)"
    awk -F= '/^(BUNNY_CDN_HOST|BUNNY_TOKEN_KEY|BUNNY_CORS_READY)=/ { print }' "$cdn_config" >"$signing_tmp"
    install -m 0640 -o root -g "$site_group" "$signing_tmp" "$private_dir/bunny-signing.env"
    rm -f -- "$signing_tmp"
else
    rm -f -- "$private_dir/bunny-sync.env" "$private_dir/bunny-signing.env"
fi

python3 "$script_dir/deploy-files.py" \
    --source "$build_root/site" \
    --target "$target" \
    --owner "$site_owner"

install -d -m 0755 -o root -g root /usr/local/libexec/hls-video-gallery
install -m 0755 -o root -g root \
    "$script_dir/prepare-media-permissions.py" \
    /usr/local/libexec/hls-video-gallery/prepare-media-permissions.py

marker_tmp="$(mktemp)"
printf 'HLS Video Gallery\nVersion: %s\nPipeline-Version: %s\nInstance-ID: %s\nConfig-SHA256: %s\nEncoding-SHA256: %s\nInstalled: %s\nTarget: %s\n' \
    "$app_version" "$pipeline_version" "$instance_id" "$config_sha" "$encoding_sha" "$(date -Is)" "$target" >"$marker_tmp"
install -m 0644 -o "$site_owner" -g "$site_group" "$marker_tmp" "$marker"
rm -f -- "$marker_tmp"

if [[ -n "$previous_encoding_sha" && "$previous_encoding_sha" != "$encoding_sha" ]]; then
    install -m 0644 -o "$site_owner" -g "$site_group" /dev/null "$target/data/force-rebuild"
    echo "Encoding settings changed; the next scanner pass will rebuild each video once."
fi

install_unit() {
    local source_name="$1"
    local destination_name="$2"
    install -m 0644 -o root -g root "$build_root/systemd/$source_name" "/etc/systemd/system/$destination_name"
}

scan_service="hls-gallery-${instance_id}-scan.service"
scan_timer="hls-gallery-${instance_id}-scan.timer"
monitor_service="hls-gallery-${instance_id}-monitor.service"
analyzer_service="hls-gallery-${instance_id}-analyzer.service"
analyzer_timer="hls-gallery-${instance_id}-analyzer.timer"
bunny_service="hls-gallery-${instance_id}-bunny.service"
media_permission_service="hls-gallery-${instance_id}-media-permissions.service"
media_permission_path="hls-gallery-${instance_id}-media-permissions.path"
media_permission_timer="hls-gallery-${instance_id}-media-permissions.timer"

install_unit hls-gallery-scan.service "$scan_service"
install_unit hls-gallery-scan.timer "$scan_timer"
install_unit hls-gallery-monitor.service "$monitor_service"
install_unit hls-gallery-analyzer.service "$analyzer_service"
install_unit hls-gallery-analyzer.timer "$analyzer_timer"
install_unit hls-gallery-media-permissions.service "$media_permission_service"
install_unit hls-gallery-media-permissions.path "$media_permission_path"
install_unit hls-gallery-media-permissions.timer "$media_permission_timer"

if [[ "$cdn_provider" == "bunny" ]]; then
    install_unit hls-gallery-bunny.service "$bunny_service"
elif systemctl list-unit-files "$bunny_service" --no-legend 2>/dev/null | grep -q "$bunny_service"; then
    systemctl disable --now "$bunny_service" >/dev/null 2>&1 || true
fi

ln -sfn "$target/_tools/status_cli.py" "/usr/local/bin/hls-gallery-status-${instance_id}"
if [[ "$cdn_provider" == "bunny" ]]; then
    ln -sfn "$target/_tools/bunny_sync.py" "/usr/local/bin/hls-gallery-bunny-status-${instance_id}"
else
    status_link="/usr/local/bin/hls-gallery-bunny-status-${instance_id}"
    if [[ -L "$status_link" && "$(readlink "$status_link")" == "$target/_tools/bunny_sync.py" ]]; then
        rm -f -- "$status_link"
    fi
fi

if command -v restorecon >/dev/null 2>&1; then
    restorecon -RF "$target" "$private_dir" >/dev/null 2>&1 || true
fi

systemctl daemon-reload
if $start_services; then
    systemctl enable --now "$media_permission_path" "$media_permission_timer"
    systemctl start "$media_permission_service"
    systemctl enable --now "$monitor_service" "$scan_timer"
    systemctl start --no-block "$scan_service" || true
    if [[ "$cdn_provider" == "bunny" ]]; then
        systemctl enable --now "$bunny_service"
    fi
    if [[ "$analysis_enabled" == "true" ]]; then
        if [[ -x /opt/hls-video-gallery-analyzer/bin/python ]]; then
            systemctl enable --now "$analyzer_timer"
        else
            echo "Visual tags are configured but the optional model runtime is not installed."
            echo "Run: sudo $repo_root/scripts/install-analyzer.sh --config $config_path"
        fi
    else
        systemctl disable --now "$analyzer_timer" >/dev/null 2>&1 || true
    fi
fi

echo
echo "HLS Video Gallery installed."
echo "Gallery: $(python3 "$script_dir/json-value.py" "$build_root/site/data/site-config.json" site.public_base_url)/"
echo "Media directory: $target/media"
echo "Status: hls-gallery-status-${instance_id} --watch"
echo "Encoder log: journalctl -fu $scan_service"
