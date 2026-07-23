#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"
version="$(tr -d '[:space:]' <"$repo_root/VERSION")"
destination="$repo_root/dist"
archive="$destination/hls-video-gallery-${version}.tar.gz"
bundle="$destination/hls-video-gallery-${version}.bundle"

"$script_dir/validate.sh"
mkdir -p "$destination"
git -C "$repo_root" archive \
    --format=tar.gz \
    --prefix="hls-video-gallery-${version}/" \
    -o "$archive" \
    HEAD
shasum -a 256 "$archive" >"$archive.sha256"
bundle_temporary="$destination/.hls-video-gallery-${version}.bundle.building"
git -C "$repo_root" bundle create "$bundle_temporary" --all
mv -f -- "$bundle_temporary" "$bundle"
shasum -a 256 "$bundle" >"$bundle.sha256"
echo "Release archive: $archive"
echo "Cloneable Git bundle: $bundle"
