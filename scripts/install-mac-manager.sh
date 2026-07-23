#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
host=""
ssh_user=""
remote_root=""
identity_file=""

usage() {
    cat <<'USAGE'
Usage: ./scripts/install-mac-manager.sh [options]

Installs a double-clickable HLS Gallery Manager in ~/Applications.
Without connection options, the manager asks for them on first launch.

Options:
  --host HOST
  --user USER
  --remote-root PATH
  --identity-file PATH
  -h, --help
USAGE
}

while (($#)); do
    case "$1" in
        --host)
            [[ $# -ge 2 ]] || { echo "--host requires a value" >&2; exit 2; }
            host="$2"
            shift 2
            ;;
        --user)
            [[ $# -ge 2 ]] || { echo "--user requires a value" >&2; exit 2; }
            ssh_user="$2"
            shift 2
            ;;
        --remote-root)
            [[ $# -ge 2 ]] || { echo "--remote-root requires a value" >&2; exit 2; }
            remote_root="$2"
            shift 2
            ;;
        --identity-file)
            [[ $# -ge 2 ]] || { echo "--identity-file requires a value" >&2; exit 2; }
            identity_file="$2"
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

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The Photos-enabled manager installer requires macOS." >&2
    exit 1
fi

for command in python3 ssh scp osascript install; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command is missing: $command" >&2
        exit 1
    }
done

support_dir="$HOME/Library/Application Support/HLS Video Gallery"
applications_dir="$HOME/Applications"
installed_script="$support_dir/hls-gallery-manager.py"
launcher="$applications_dir/HLS Gallery Manager.command"

install -d -m 0700 "$support_dir"
install -d -m 0755 "$applications_dir"
install -m 0755 "$repo_root/tools/hls-gallery-manager.py" "$installed_script"

launcher_tmp="$(mktemp)"
printf '#!/bin/zsh\nexec /usr/bin/env python3 %q "$@"\n' "$installed_script" >"$launcher_tmp"
install -m 0755 "$launcher_tmp" "$launcher"
rm -f -- "$launcher_tmp"

if [[ -n "$host$ssh_user$remote_root$identity_file" ]]; then
    [[ -n "$host" && -n "$ssh_user" && -n "$remote_root" ]] || {
        echo "--host, --user, and --remote-root must be supplied together." >&2
        exit 2
    }
    configure_args=(
        configure
        --non-interactive
        --host "$host"
        --user "$ssh_user"
        --remote-root "$remote_root"
    )
    if [[ -n "$identity_file" ]]; then
        configure_args+=(--identity-file "$identity_file")
    fi
    python3 "$installed_script" "${configure_args[@]}"
fi

echo
echo "HLS Gallery Manager installed."
echo "Open: $launcher"
echo "The first Photos action will ask for permission to control Photos."
