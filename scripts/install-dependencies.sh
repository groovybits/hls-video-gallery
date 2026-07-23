#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "Dependency installation failed at line $LINENO" >&2' ERR

if [[ $EUID -ne 0 ]]; then
    echo "Run this dependency installer as root." >&2
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "Cannot identify this Linux distribution. Install FFmpeg, Python 3, OpenSSL, PHP, and htpasswd manually." >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
distribution="${ID:-unknown}"
major="${VERSION_ID%%.*}"

case "$distribution" in
    almalinux|rocky)
        if [[ "$major" != "9" && "$major" != "10" ]]; then
            echo "Automatic RPM setup supports AlmaLinux/Rocky 9 and 10; detected $distribution ${VERSION_ID:-unknown}." >&2
            exit 1
        fi
        dnf install -y dnf-plugins-core
        dnf config-manager --set-enabled crb
        dnf install -y epel-release
        dnf install -y "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm"
        dnf install -y ffmpeg ffmpeg-libs httpd-tools python3 php-cli openssl gcc-c++ make
        ;;
    debian|ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y ffmpeg python3 php-cli apache2-utils openssl ca-certificates build-essential
        ;;
    *)
        echo "Automatic dependency setup does not support '$distribution'." >&2
        echo "Install FFmpeg with libx264 and AAC, Python 3, PHP, OpenSSL, and htpasswd, then run install.sh." >&2
        exit 1
        ;;
esac

ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx264' || {
    echo "Installed FFmpeg does not expose the libx264 encoder." >&2
    exit 1
}
ffmpeg -hide_banner -encoders 2>/dev/null | grep -Eq '[[:space:]]aac[[:space:]]' || {
    echo "Installed FFmpeg does not expose the AAC encoder." >&2
    exit 1
}
ffmpeg -hide_banner -filters 2>/dev/null | grep -Eq '[[:space:]]scale[[:space:]]' || {
    echo "Installed FFmpeg does not expose the scale filter." >&2
    exit 1
}

echo "Dependencies are ready."
ffmpeg -version | sed -n '1p'
