# Operations

Examples below use the instance ID `my-video-gallery` and document root
`/var/www/html/videos`. Substitute your configured values.

## Services

| Unit | Purpose |
|---|---|
| `hls-gallery-my-video-gallery-scan.timer` | Checks for stable changed sources after each scan completes. |
| `hls-gallery-my-video-gallery-scan.service` | One locked, one-at-a-time FFmpeg/catalog pass. |
| `hls-gallery-my-video-gallery-media-permissions.path` | Notices newly copied source files. |
| `hls-gallery-my-video-gallery-media-permissions.timer` | Fallback permission check for replaced files. |
| `hls-gallery-my-video-gallery-media-permissions.service` | Makes supported sources readable by the configured site account. |
| `hls-gallery-my-video-gallery-monitor.service` | Publishes live FFmpeg and queue telemetry. |
| `hls-gallery-my-video-gallery-analyzer.timer` | Optional low-priority visual-tag batches. |
| `hls-gallery-my-video-gallery-analyzer.service` | Optional cached-thumbnail model run. |
| `hls-gallery-my-video-gallery-quality.timer` | Optional post-encode objective-quality batches. |
| `hls-gallery-my-video-gallery-quality.service` | Serial VMAF/SSIM/PSNR/pHash measurement worker. |
| `hls-gallery-my-video-gallery-bunny.service` | Optional continuous Bunny upload/prune worker. |

Useful commands:

```bash
systemctl status hls-gallery-my-video-gallery-scan.timer
systemctl list-timers 'hls-gallery-*'
journalctl -fu hls-gallery-my-video-gallery-scan.service
journalctl -fu hls-gallery-my-video-gallery-analyzer.service
journalctl -fu hls-gallery-my-video-gallery-quality.service
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
Pending sources are processed oldest upload first, and the same telemetry feeds
the authenticated web status panel.

Optional quality analysis has a separate status command:

```bash
hls-gallery-quality-status-my-video-gallery --watch
hls-gallery-quality-status-my-video-gallery --watch --all --command
hls-gallery-quality-status-my-video-gallery --json
```

Its queue contains only current catalog versions that do not yet have a current
quality report. It processes one video at a time, defers to encoding and visual
analysis, and uses shared locks so those jobs cannot start during a measurement.
The displayed forecast learns from completed analysis-time/source-duration
ratios; before enough history exists it intentionally uses a conservative
estimate.

To refresh only the compact dashboard and self-contained standalone HTML for
completed measurements, without decoding video or running VMAF, use:

```bash
hls-gallery-quality-status-my-video-gallery --render-reports-only
```

## Adding, replacing, and deleting media

- Add: finish copying a file into `media/`. It is processed after `settle_seconds`.
- Administrator uploads are automatically reassigned to the configured site
  account and made readable; the scanner itself remains unprivileged.
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
Objective quality output under `data/quality/` and `data/quality-index.json` can
also be regenerated, but backing it up can avoid many hours of full-frame
measurement. `data/quality-cards.json` is a small derived browser projection and
does not need to be backed up separately.

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
- Check `systemctl status hls-gallery-my-video-gallery-media-permissions.service`
  if the file was copied by another account.
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

### Quality analysis is waiting or missing

- Confirm both `quality_analysis.enabled` and
  `gallery.show_quality_analysis` have the intended values; the first runs work,
  while the second displays it.
- Run `ffmpeg -hide_banner -filters` and confirm `libvmaf`, `scdet`,
  `colorspace`, `zscale`, and `tonemap` are present.
- Check the quality status reason. Active encoding, visual analysis, the
  one-minute load ceiling, or another quality worker can legitimately defer it.
- If visual analysis is enabled, the exact current cache version must finish that
  stage before entering the quality queue.
- Check `systemctl status hls-gallery-my-video-gallery-quality.timer` and the
  quality service journal.

Quality reports are source-aware and settings-aware. An unchanged video is not
remeasured merely because time passed. Replacing or touching a source, or
changing a quality setting, makes the prior report stale without rebuilding HLS
solely for the quality stage. See
[Objective quality analysis](QUALITY_ANALYSIS.md) for scoring and standalone use.
