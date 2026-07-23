#!/usr/bin/env python3
"""Verify one CDN-backed gallery item without printing its signed URL."""

import argparse
import base64
import json
import sys
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def request(url, authorization="", origin="", byte_range="", limit=512_000):
    headers = {"User-Agent": "HLSVideoGalleryCDNVerifier/1.0"}
    if authorization:
        headers["Authorization"] = authorization
    if origin:
        headers["Origin"] = origin
    if byte_range:
        headers["Range"] = byte_range
    response = urlopen(Request(url, headers=headers), timeout=30)
    payload = response.read(limit)
    return response.status, response.headers, payload


def checked_request(label, *args, **kwargs):
    try:
        return request(*args, **kwargs)
    except HTTPError as error:
        error.read(1024)
        raise RuntimeError("{} returned HTTP {}".format(label, error.code)) from error


def playlist_entry(payload, suffix):
    for line in payload.decode("utf-8", errors="replace").splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value.lower().endswith(suffix):
            return value
    raise RuntimeError("Playlist contains no {} entry".format(suffix))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, help="public gallery URL, for example https://videos.example.com/videos/")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-stdin", action="store_true")
    arguments = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if arguments.password_stdin else ""
    if not password:
        raise SystemExit("A password must be supplied on stdin")
    authorization = "Basic " + base64.b64encode((arguments.username + ":" + password).encode("utf-8")).decode("ascii")
    site = arguments.site.rstrip("/") + "/"
    origin = "{}://{}".format(urlparse(site).scheme, urlparse(site).netloc)

    status, _headers, payload = checked_request("Catalog", urljoin(site, "data/catalog.json"), authorization=authorization)
    if status != 200:
        raise RuntimeError("Catalog returned HTTP {}".format(status))
    catalog = json.loads(payload.decode("utf-8"))

    selected = None
    access = None
    for item in catalog.get("items", []):
        query = urlencode({"id": item.get("id", ""), "version": item.get("version", "")})
        try:
            status, _headers, payload = checked_request("Media signer", urljoin(site, "media-access.php") + "?" + query, authorization=authorization)
        except RuntimeError:
            continue
        if status != 200:
            continue
        candidate = json.loads(payload.decode("utf-8"))
        if candidate.get("mode") == "cdn":
            selected = item
            access = candidate
            break
    if selected is None or access is None:
        raise RuntimeError("No completed CDN-backed item is available yet")

    master_status, master_headers, master = checked_request("CDN master playlist", access["hls_url"], origin=origin)
    variant_url = urljoin(access["hls_url"], playlist_entry(master, ".m3u8"))
    variant_status, variant_headers, variant = checked_request("CDN variant playlist", variant_url, origin=origin)
    segment_url = urljoin(variant_url, playlist_entry(variant, ".ts"))
    segment_status, segment_headers, segment = checked_request("CDN segment", segment_url, origin=origin, byte_range="bytes=0-1023", limit=1024)

    prefix = "cache/" + selected["cache_key"] + "/"
    poster_relative = str(selected.get("poster_url") or "")
    if not poster_relative.startswith(prefix):
        raise RuntimeError("Poster path does not match the selected cache")
    poster_url = access["base_url"] + poster_relative[len(prefix):]
    poster_status, poster_headers, poster = checked_request("CDN poster", poster_url, origin=origin, byte_range="bytes=0-1023", limit=1024)

    for label, headers in (("master", master_headers), ("variant", variant_headers), ("segment", segment_headers), ("poster", poster_headers)):
        allowed_origin = headers.get("Access-Control-Allow-Origin", "")
        if allowed_origin not in {"*", origin}:
            raise RuntimeError("CDN {} response is missing a usable CORS header".format(label))

    print("CDN item: {}".format(selected.get("title", selected["cache_key"])))
    print("CDN host: {}".format(urlparse(access["hls_url"]).hostname))
    print("Master: HTTP {} | {} | CORS {}".format(master_status, master_headers.get_content_type(), master_headers.get("Access-Control-Allow-Origin", "missing")))
    print("Variant: HTTP {} | {} | CORS {}".format(variant_status, variant_headers.get_content_type(), variant_headers.get("Access-Control-Allow-Origin", "missing")))
    print("Segment: HTTP {} | {} | CORS {} | {} bytes sampled".format(segment_status, segment_headers.get_content_type(), segment_headers.get("Access-Control-Allow-Origin"), len(segment)))
    print("Poster: HTTP {} | {} | CORS {} | {} bytes sampled".format(poster_status, poster_headers.get_content_type(), poster_headers.get("Access-Control-Allow-Origin"), len(poster)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("Verification failed: {}".format(error), file=sys.stderr)
        sys.exit(2)
