# Operations

Examples below use the instance ID `my-video-gallery` and document root
`/var/www/html/videos`. Substitute your configured values.

## Services

| Unit | Purpose |
|---|---|
| `hls-gallery-my-video-gallery-scan.timer` | Checks for stable changed sources after each scan completes. |
| `hls-gallery-my-video-gallery-scan.service` | One locked, one-at-a-time FFmpeg/catalog pass. |
| `hls-gallery-my-video-gallery-monitor.service` | Publishes live FFmpeg and queue telemetry. |
| `hls-gallery-my-video-gallery-analyzer.timer` | Optional low-priority visual-tag batches. |
| `hls-gallery-my-video-gallery-analyzer.service` | Optional cached-thumbnail model run. |
| `hls-gallery-my-video-gallery-bunny.service` | Optional continuous Bunny upload/prune worker. |

Useful commands:

```bash
systemctl status hls-gallery-my-video-gallery-scan.timer
systemctl list-timers 'hls-gallery-*'
journalctl -fu hls-gallery-my-video-gallery-scan.service
journalctl -fu hls-gallery-my-video-gallery-analyzer.service
journalctl -fu hls-gallery-my-video-gallery-bunny.service
```

The scan log is also written to `data/scan.log` for control panels and ordinary
SSH sessions.

## Terminal status

```bash
hls-gallery-status-my-video-gallery --watch
hls-gallery-status-my-video-gallery --watch --command --all
hls-gallery-status-my-video-gallery --json
```

Queue position and total describe the current scan run, not the all-time catalog.
The same telemetry feeds the authenticated web status panel.

## Adding, replacing, and deleting media

- Add: finish copying a file into `media/`. It is processed after `settle_seconds`.
- Replace: replace the source while preserving its relative path. The changed size
  or mtime creates a new cache version and invalidates its prior guest link.
- Rename: treated as deleting one gallery ID and adding another.
- Delete: the next scan removes it from the catalog immediately. After
  `cache_retention_seconds`, generated cache output is removed.

Supported extensions include 3GP, AVI, FLV, M2TS, M4V, MKV, MOV, MP4, MPEG/MPG,
MTS, MXF, OGV, TS, WebM, and WMV. Actual decodability depends on the installed
FFmpeg build.

Hidden files/directories and symlinks under `media/` are ignored.

## Cache cleanup

The scanner retains unchanged output indefinitely and never re-encodes it merely
because time passed. Old versions are subject to the configured grace period.

For an immediate, explicit cleanup:

```bash
sudo -u www-data /var/www/html/videos/_tools/cleanup_cache.py --dry-run
sudo -u www-data /var/www/html/videos/_tools/cleanup_cache.py
```

The cleanup tool requires the application marker and a valid live catalog, takes
the scan lock, and only removes directory names matching the generated cache
format. Unknown directories are ignored.

## Backups

Back up:

- source `media/`;
- `config/gallery.json`;
- the private directory, especially `users.htpasswd` and `share.key`;
- `data/content-overrides.json` if you use manual tag corrections.

The `cache/` directory can be recreated from sources, so it is optional. Catalog
and visual-analysis JSON can also be rebuilt, though saving them avoids work.

Never publish a backup containing Bunny credentials or the Basic Auth file.

## Updating encoding settings

Edit `config/gallery.json`, then rerun:

```bash
sudo ./scripts/install.sh
```

If the encoding hash changed, `data/force-rebuild` is created. The next complete
scan rebuilds every source once, then removes the marker. If the scan is
interrupted, it safely resumes and reuses already completed current-version
output where appropriate.

## Troubleshooting

### The player is blank

1. Open browser developer tools and check requests for `master.m3u8` and `.ts`.
2. Confirm `.htaccess` is honored and MIME types are present.
3. Confirm completed cache directories are traversable (`0755`) and files readable.
4. Run `scripts/doctor.sh`.
5. Check `data/scan.log` for build errors.

### A newly uploaded file is not visible

- Verify its extension is supported.
- Wait through `settle_seconds`.
- Confirm the upload has stopped changing the file.
- Run a scan manually and watch the scan log.
- Use `ffprobe` on the source; incomplete browser-recorded WebM files may require
  the scanner’s packet-timestamp fallback and therefore take longer to probe.

### Browser keeps asking for a password

Confirm the site remains on one HTTPS hostname, there is no redirect between
hostnames, the realm is stable, and a proxy is not stripping `Authorization`.
Browser Basic Auth caching is browser-controlled.

### SELinux

The installer runs `restorecon` when available. Custom document roots may still
need an administrator-defined persistent file-context rule. Do not solve this by
disabling SELinux globally.

### DirectAdmin or other control panels

Set `install.owner` to the domain account and `document_root` to an isolated
subdirectory such as `.../public_html/video`. Confirm the panel’s Apache template
permits `.htaccess` overrides for that path.
