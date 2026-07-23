#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
config_path="$repo_root/config/gallery.json"
username=""

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/add-user.sh [--config PATH] USERNAME

Adds or replaces one Basic Auth user without storing the password in shell
history or config/users.txt.
USAGE
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
        -*)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
        *)
            [[ -z "$username" ]] || { echo "Only one username may be supplied" >&2; exit 2; }
            username="$1"
            shift
            ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Run this command as root." >&2; exit 1; }
[[ "$username" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || {
    echo "Username must use 1-64 letters, numbers, dots, underscores, or hyphens." >&2
    exit 2
}
command -v htpasswd >/dev/null 2>&1 || {
    echo "htpasswd is missing; run scripts/install-dependencies.sh." >&2
    exit 1
}

render_dir="$(mktemp -d "${TMPDIR:-/tmp}/hls-gallery-user.XXXXXX")"
cleanup() {
    rm -rf -- "$render_dir"
}
trap cleanup EXIT
python3 "$script_dir/configure.py" --config "$config_path" --output "$render_dir" >/dev/null

private_dir="$(python3 "$script_dir/json-value.py" "$render_dir/install.json" private_dir)"
site_owner="$(python3 "$script_dir/json-value.py" "$render_dir/install.json" owner)"
site_group="$(id -gn "$site_owner")"
password_file="$private_dir/users.htpasswd"

install -d -m 0750 -o root -g "$site_group" "$private_dir"
read -r -s -p "Password for ${username}: " first_password
printf '\n'
read -r -s -p "Repeat password: " second_password
printf '\n'
[[ -n "$first_password" && "$first_password" == "$second_password" ]] || {
    unset first_password second_password
    echo "Passwords were empty or did not match." >&2
    exit 1
}

temporary="$(mktemp "$private_dir/.users.XXXXXX")"
if [[ -f "$password_file" ]]; then
    cp -- "$password_file" "$temporary"
    printf '%s\n' "$first_password" | htpasswd -iB "$temporary" "$username" >/dev/null
else
    printf '%s\n' "$first_password" | htpasswd -ciB "$temporary" "$username" >/dev/null
fi
unset first_password second_password
install -m 0640 -o root -g "$site_group" "$temporary" "$password_file"
rm -f -- "$temporary"
echo "Basic Auth user '$username' is ready."
