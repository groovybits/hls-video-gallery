# Configuration

The installer reads `config/gallery.json`. Copy
`config/gallery.example.json` to create it. JSON does not support comments, so
this document describes each section.

## `schema_version`

Must be `1`. The validator rejects unknown schemas rather than guessing.

## `instance_id`

A unique lowercase identifier using letters, numbers, and hyphens. It becomes
part of systemd unit names and status commands. Keep it stable after installation.

## `install`

| Key | Meaning |
|---|---|
| `document_root` | Isolated absolute directory served by Apache. The installer refuses a nonempty unmarked directory. |
| `owner` | Existing Unix account that owns media, cache, data, and encoder work. On control-panel hosting, use the domain account. |
| `private_dir` | Optional secrets/model directory outside the web root. Empty means `/etc/hls-video-gallery/INSTANCE_ID`. |

Paths containing whitespace are deliberately rejected because they are also
rendered into systemd unit directives.

## `site`

| Key | Meaning |
|---|---|
| `public_base_url` | Exact HTTPS gallery URL without a trailing slash, including a path such as `/video` when applicable. |
| `main_site_url` | HTTPS URL used by Back to site links. |
| `language` | HTML language code, such as `en` or `en-US`. |
| `public_landing` | Exposes only the teaser and selected static assets before Basic Auth. The catalog remains protected. |

## `brand`

All visible names and landing-page copy live here. `profile_image` and
`social_image` can be absolute paths or paths relative to the configuration file
or repository. Supported files are JPG, PNG, and WebP.

If `social_image` is empty, the package uses its generic social card. If
`profile_image` is empty, no gallery portrait is shown.

## `theme`

Each value is a six-digit hex color. `accent` and `accent_alt` drive buttons,
meters, focus states, and ambient decoration. The generated `assets/theme.css`
overrides the neutral application defaults without rewriting the main stylesheet.

## `access`

| Key | Meaning |
|---|---|
| `basic_auth` | Protects the gallery with Apache Basic Auth. Recommended. |
| `realm` | Browser login prompt label. |
| `public_share_links` | Enables password-free bearer links for exactly one cached video version. Requires Bunny CDN. |

For initial installation, `config/users.txt` contains one `username:password`
pair per line. It is ignored by Git and converted into bcrypt hashes outside the
web root. Remove it after installation if you prefer, then manage users with
`scripts/add-user.sh`.

## `gallery`

| Key | Meaning |
|---|---|
| `page_size` | Videos per page, 1–100. |
| `autoplay` | Requests playback when a detail page is ready. Browsers may still require a tap. |
| `unmuted` | Requests sound by default. Browser autoplay rules commonly block sound until a user gesture. |
| `show_encoder_status` | Displays the live FFmpeg/queue panel to authenticated viewers. |
| `show_content_analysis` | Displays visual-analysis status and filters. |
| `show_quality_analysis` | Displays objective quality status and cached reports to authenticated viewers. |
| `title_words` | Lowercase filename words that need exact capitalization. |

## `encoding`

| Key | Default | Meaning |
|---|---:|---|
| `max_height` | `1080` | Maximum output height; smaller sources are not enlarged. |
| `preset` | `superfast` | x264 CPU/compression tradeoff: `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, or `medium`. |
| `video_bitrate` | `6500000` | Maximum bits/s cap. The selected height profile may use less. |
| `audio_bitrate` | `160000` | Maximum AAC bits/s cap. |
| `thumbnail_interval` | `10` | Seconds between timeline frames. |
| `thumbnail_width` | `480` | Maximum JPEG width without upscaling. |
| `hls_segment_seconds` | `6` | Target segment and GOP interval, 2–30 seconds. |
| `settle_seconds` | `30` | Minimum unchanged age before a newly uploaded file is processed. |
| `failure_retry_seconds` | `300` | Delay before retrying a failed source. |
| `cache_retention_seconds` | `86400` | Grace period before automatically removing unreferenced cache versions. |

The cache identity includes source relative path, byte size, and nanosecond mtime.
Unchanged files reuse HLS and thumbnails. Replacing or touching a source changes
its version. Renaming changes its stable gallery ID and guest links.

## `content_analysis`

`enabled` controls the systemd timer. `taxonomy` points to a validated tag JSON
file. `items_per_run`, `interval_seconds`, `max_load`, and `threads` constrain
resource use.

Every taxonomy tag needs:

- a unique `key`, display `label`, and `group`;
- a `threshold` from `0.5` to `0.99`;
- `filename_patterns`;
- one or more `positive` and `negative` visual prompts.

Filename patterns power instant filename hints whether or not the optional model
is installed.

## `quality_analysis`

Objective quality analysis compares each completed HLS rendition with its source
using VMAF, SSIM, PSNR, and perceptual hashes. It is independent of
`gallery.show_quality_analysis`: `enabled` controls background work, while the
gallery flag controls authenticated display.

| Key | Default | Meaning |
|---|---:|---|
| `enabled` | `false` | Builds the C++ analyzer and report renderer, then enables the instance's quality timer. |
| `items_per_run` | `1` | Maximum videos measured serially during one timer activation, 1–20. |
| `interval_seconds` | `1` | Delay between completed timer activations, 1–86400 seconds. |
| `max_load` | `0` | Optional one-minute load ceiling; `0` disables this gate while process/lock checks remain active. |
| `threads` | `2` | Analyzer processing threads, restricted to 1–2. |
| `frame_rate` | `30` | Aligned comparison frames per second, 1–120. The standard scoring baseline is 30. |
| `scene_threshold` | `10` | FFmpeg source-scene change threshold, 0.1–100. |
| `min_scene_seconds` | `2` | Shorter detected fragments are merged with an adjacent scene. |
| `failure_retry_seconds` | `30` | Cooldown before retrying one failed source/cache version. |

When `content_analysis.enabled` is true, the rendered quality service waits for a
current content-analysis record for each video. The installer also pins the
expected analyzer/taxonomy version into the quality service, so a self-consistent
but stale category index cannot unlock measurements after an upgrade. Both
optional stages remain separate; quality analysis begins only after that
prerequisite and encoding are complete.

Changing `frame_rate`, `scene_threshold`, or `min_scene_seconds` changes the
measurement signature and queues fresh reports. Scheduling, load, retry,
thread-count, and prerequisite-policy changes reuse valid metric results. None
of these settings rebuilds HLS media. The service is capped at two CPU cores
even if the host has more processors. See
[Objective quality analysis](QUALITY_ANALYSIS.md) for the exact scoring formula,
HDR normalization, standalone CLI, and outputs.

## `cdn`

`provider` is `none` or `bunny`. With Bunny, `config_file` points to an ignored
environment file. See [Bunny CDN setup](BUNNY_CDN.md).

## Render without installing

Use this on a workstation or CI runner:

```bash
python3 scripts/configure.py \
  --config config/gallery.json \
  --output build/preview
```

The rendered directory contains no plaintext passwords or Bunny Storage password.
The real installer separately installs narrowly scoped secret files.
