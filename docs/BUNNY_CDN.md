# Bunny CDN setup

The integration uses a Bunny Storage Zone as durable replicated output and a Pull
Zone for delivery. The origin continues to encode; the sync worker uploads only
completed cache versions.

## 1. Create the zones

1. Create a Bunny Storage Zone.
2. Create or attach a Pull Zone whose origin is that Storage Zone.
3. Enable token authentication on the Pull Zone.
4. Add your gallery origin to the Pull Zone CORS allowed origins.
5. Confirm `.m3u8`, `.ts`, and image files retain their expected content types.

## 2. Copy credentials

```bash
cp config/bunny.example.env config/bunny.env
chmod 600 config/bunny.env
```

Fill:

| Variable | Bunny value |
|---|---|
| `BUNNY_STORAGE_ZONE` | Storage Zone name. |
| `BUNNY_STORAGE_PASSWORD` | Storage Zone API/HTTP password, not the account API key. |
| `BUNNY_STORAGE_ENDPOINT` | Regional Storage endpoint; the default global endpoint is valid when appropriate. |
| `BUNNY_CDN_HOST` | Pull Zone hostname without `https://`. |
| `BUNNY_TOKEN_KEY` | Pull Zone token-authentication key. |
| `BUNNY_CORS_READY` | Set to `1` only after testing CORS. |

The Storage Zone access page labels the first value `Password`; that is the
storage API key this worker needs. The Pull Zone’s token-authentication key is a
different secret found in the Pull Zone security/token settings.

## 3. Enable it

In `config/gallery.json`:

```json
"cdn": {
  "provider": "bunny",
  "config_file": "config/bunny.env"
}
```

For password-free one-video links, also set:

```json
"public_share_links": true
```

Then rerun:

```bash
sudo ./scripts/install.sh
```

## 4. Verify delivery

Wait for a video to finish and the sync worker to publish its mapping:

```bash
journalctl -fu hls-gallery-my-video-gallery-bunny.service
cat /var/www/html/videos/data/bunny-sync-status.json
hls-gallery-bunny-status-my-video-gallery
```

Open the authenticated gallery and inspect the JSON response from
`media-access.php`. A synchronized item returns:

```json
{
  "mode": "cdn",
  "hls_url": "https://YOUR-PULL-ZONE/.../master.m3u8"
}
```

An item not uploaded yet safely returns `"mode": "origin"`. Seeing the gallery
page itself on the origin hostname is normal; large thumbnails and HLS media are
the resources served through Bunny.

## Security model

- The root-only sync environment contains the Storage password.
- PHP receives a separate group-readable file containing only delivery hostname,
  token key, and the CORS-ready flag.
- The browser receives expiring signed directory URLs, never either secret.
- Public share tokens are bound to the source-derived cache key. Changing,
  renaming, or deleting the source invalidates the old link.

Guest links are bearer credentials. Send them only to intended recipients.
