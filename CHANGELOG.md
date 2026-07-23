# Changelog

All notable changes are documented here.

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
