# Mac Gallery Manager

The Mac Gallery Manager uploads and manages source videos without requiring you
to remember SSH paths. It can:

- export the current selection or an entire user album from Apple Photos;
- retrieve iCloud-backed originals through Photos when optimized storage is on;
- use a private temporary directory and delete the export after upload;
- upload local videos selected in Finder;
- browse paged source videos in upload order, including queued and encoding items;
- show source, processing, and stream details;
- show current queue and FFmpeg status; and
- permanently delete a source after an explicit confirmation.

The utility uses macOS `osascript`, Python 3, SSH, and SCP. It stores connection
settings, but no SSH or gallery passwords, in:

```text
~/Library/Application Support/HLS Video Gallery/manager.json
```

SSH connection sharing remains active for ten minutes, so password-based
accounts normally prompt once per management session rather than once per file.
SSH keys continue to work without a password prompt.

## Install

From a repository checkout:

```bash
./scripts/install-mac-manager.sh
```

Open `HLS Gallery Manager.command` from your home `Applications` folder. The
first launch asks for:

1. server hostname or IP;
2. SSH user;
3. remote gallery root; and
4. an optional SSH private-key path.

An administrator can preconfigure those non-secret values:

```bash
./scripts/install-mac-manager.sh \
  --host videos.example.com \
  --user gallery-owner \
  --remote-root /var/www/html/videos
```

The configured SSH user needs write access to the gallery's `media` directory.
Uploads made by an administrator account are also supported when the server's
media-permission watcher is enabled.

## Photos and iCloud

Open Photos, select one or more videos, and choose “Upload current Photos
selection” in the manager. To send an album, choose “Upload a Photos album” and
pick it from the numbered list.

Photos exports the unmodified originals. When an original is only in iCloud,
Photos downloads it during export. The manager needs temporary local space equal
to the selected originals, but cleans that temporary export after the SCP
transfer finishes.

On first use, macOS asks whether Terminal may control Photos. If it was denied,
enable Terminal under **System Settings → Privacy & Security → Automation**.

Apple documents that an optimized Photos library can export items without a
separate manual iCloud download:

<https://support.apple.com/guide/photos/phtfa50fd1ec/mac>

## Command line

The installed Python file also supports direct commands:

```bash
python3 tools/hls-gallery-manager.py photos-selection
python3 tools/hls-gallery-manager.py photos-album
python3 tools/hls-gallery-manager.py upload ~/Movies/example.mov
python3 tools/hls-gallery-manager.py list --sort upload-newest --page 1
python3 tools/hls-gallery-manager.py details 1
python3 tools/hls-gallery-manager.py status
python3 tools/hls-gallery-manager.py delete 1
```

Uploads use an unrecognized temporary suffix and an atomic final rename, so the
gallery scanner never processes a partially transferred file. Existing
filenames are skipped unless replacement is confirmed.

Deletion removes only the source video. The gallery scanner then removes the
listing and retires its generated cache according to the configured retention
period.
