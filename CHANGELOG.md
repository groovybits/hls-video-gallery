# Changelog

All notable changes are documented here.

## 1.3.0 — 2026-07-23

- Added an optional, low-priority post-encode objective-quality queue.
- Added a standalone C++17 analyzer with Standard VMAF, Phone VMAF, SSIM, PSNR,
  and perceptual-hash measurements from one paired FFmpeg pass.
- Added frame-level CSV/JSON output, scene-aware scoring, a standalone visual
  report, live command-line and browser progress, and per-video quality views.
- Added source- and settings-aware report caching, safe stale-report cleanup,
  failure cooldowns, and shared locks that avoid competing with encoding or
  optional visual categorization.
- Added installation, configuration, operations, security, and validation
  support for the quality-analysis feature.

## 1.2.1 — 2026-07-23

- Fixed macOS SSH and SCP startup failures caused by placing `ControlPath` under
  the space-containing `Library/Application Support` directory.

## 1.2.0 — 2026-07-23

- Added a double-clickable macOS collection manager.
- Added direct Apple Photos selection and album exports using unmodified
  originals, including automatic retrieval of iCloud-backed originals.
- Added atomic SCP uploads, upload-order inventory, live processing status,
  source details, and confirmation-protected source deletion.

## 1.1.0 — 2026-07-23

- Added persistent upload timestamps and upload-order sorting in the gallery.
- Changed the encoding queue to oldest-upload-first order and labeled that order
  in live status.
- Added automatic ownership and permission repair for newly uploaded media,
  including files copied by an administrator account.

## 1.0.1 — 2026-07-23

- Added synchronized pagination controls above and below the video grid.
- Hid pagination controls when the active filters fit on one page.

## 1.0.0 — 2026-07-22

- Initial public release.
- Configurable branding, theme, paths, authentication, HLS settings, and taxonomy.
- Incremental one-decode HLS and thumbnail pipeline with source-aware caching.
- Phone-friendly player, pagination, search, sorting, duration filters, tag filters,
  previous/next navigation, and filtered shuffle playback.
- Live browser and terminal views for queue, FFmpeg progress, FPS, ETA, and commands.
- Optional low-priority visual classification from cached thumbnails.
- Optional Bunny Storage/CDN synchronization, signed playback, and guest share links.
- AlmaLinux/Rocky and Debian/Ubuntu dependency installers.
