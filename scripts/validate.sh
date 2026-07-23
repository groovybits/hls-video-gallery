#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
render_dir="$repo_root/build/validate"
python_cache="${TMPDIR:-/tmp}/hls-video-gallery-pycache"

for command in python3 node; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Validation requires $command" >&2
        exit 1
    }
done

while IFS= read -r file; do
    bash -n "$file"
done < <(find "$repo_root/scripts" -maxdepth 1 -type f -name '*.sh' -print | sort)

while IFS= read -r file; do
    PYTHONPYCACHEPREFIX="$python_cache" python3 -m py_compile "$file"
done < <(find "$repo_root/scripts" "$repo_root/site/_tools" -type f -name '*.py' -print | sort)

while IFS= read -r file; do
    node --check "$file"
done < <(find "$repo_root/site/assets" -maxdepth 1 -type f -name '*.js' ! -name 'hls.min.js' -print | sort)

while IFS= read -r file; do
    python3 -m json.tool "$file" >/dev/null
done < <(find "$repo_root/config" "$repo_root/presets" "$repo_root/site/data" -type f -name '*.json' -print | sort)

python3 "$repo_root/scripts/configure.py" \
    --config "$repo_root/config/gallery.example.json" \
    --output "$render_dir"

if grep -R -E '@@[A-Z0-9_]+@@' "$render_dir" >/dev/null 2>&1; then
    echo "Rendered output still contains a template token" >&2
    exit 1
fi

if command -v php >/dev/null 2>&1; then
    while IFS= read -r file; do
        php -l "$file" >/dev/null
    done < <(find "$render_dir/site" -type f -name '*.php' -print | sort)
fi

echo "Validation passed."
