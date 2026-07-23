<?php
declare(strict_types=1);

require __DIR__ . '/.share-common.php';

header('Cache-Control: no-store, max-age=0');
header('Pragma: no-cache');
header('Referrer-Policy: no-referrer');
header('X-Content-Type-Options: nosniff');
header('X-Robots-Tag: noindex, nofollow, noarchive');

$token = (string)($_GET['t'] ?? '');
$item = share_find_item_by_token($token);
$available = $item !== null;
if (!$available) {
    http_response_code(404);
}
$ownerName = share_config_value('brand', 'owner_name', 'Gallery owner');
$galleryName = share_config_value('brand', 'gallery_name', 'Video Gallery');
$shareMessage = share_config_value('brand', 'share_message', 'A private video was shared with you.');
$mainSiteUrl = share_config_value('site', 'main_site_url', '/');
$publicBaseUrl = rtrim(share_config_value('site', 'public_base_url', ''), '/');
$title = $available ? share_pretty_title($item) : 'Shared video unavailable';
$shareUrl = $available ? share_public_url($token) : $publicBaseUrl . '/';
$posterUrl = $available ? $publicBaseUrl . '/share-image.php?t=' . rawurlencode($token) : '';
$description = $available ? $shareMessage : 'This private video link is no longer available.';

function share_escape(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
?><!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#0d1117">
  <meta name="referrer" content="no-referrer">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta name="description" content="<?= share_escape($description) ?>">
  <meta property="og:type" content="video.other">
  <meta property="og:site_name" content="<?= share_escape($galleryName) ?>">
  <meta property="og:title" content="<?= share_escape($title) ?>">
  <meta property="og:description" content="<?= share_escape($description) ?>">
  <meta property="og:url" content="<?= share_escape($shareUrl) ?>">
<?php if ($posterUrl !== ''): ?>
  <meta property="og:image" content="<?= share_escape($posterUrl) ?>">
  <meta property="og:image:alt" content="Preview for <?= share_escape($title) ?>">
<?php endif; ?>
  <title><?= share_escape($title) ?> · <?= share_escape($galleryName) ?></title>
  <link rel="canonical" href="<?= share_escape($shareUrl) ?>">
  <link rel="stylesheet" href="<?= share_escape($publicBaseUrl) ?>/assets/share.css?v=1.0.0">
  <link rel="stylesheet" href="<?= share_escape($publicBaseUrl) ?>/assets/theme.css?v=1.0.0">
</head>
<body>
  <header class="share-header">
    <a class="share-brand" href="<?= share_escape($mainSiteUrl) ?>" aria-label="Visit <?= share_escape($ownerName) ?>'s main site">
      <span class="share-brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
      <span><strong><?= share_escape($ownerName) ?></strong><small>A private video share</small></span>
    </a>
    <span class="share-pill">Anyone with this link can watch</span>
  </header>

  <main class="share-main">
<?php if ($available): ?>
    <section class="share-card" id="share-app" data-media-endpoint="<?= share_escape($publicBaseUrl) ?>/share-media.php?t=<?= share_escape(rawurlencode($token)) ?>">
      <div class="share-intro">
        <p class="share-eyebrow"><?= share_escape($ownerName) ?> shared a video with you</p>
        <h1><?= share_escape($title) ?></h1>
      </div>
      <div class="share-player">
        <video controls autoplay playsinline preload="auto" poster="<?= share_escape($posterUrl) ?>" aria-label="Play <?= share_escape($title) ?>"></video>
        <div class="share-message is-visible" role="status" aria-live="polite">Preparing your private stream…</div>
      </div>
      <div class="share-meta">
        <span class="share-status">Loading HLS…</span>
        <span class="share-quality">Phone-ready video</span>
      </div>
    </section>
<?php else: ?>
    <section class="share-card share-unavailable">
      <p class="share-eyebrow">Private video link</p>
      <h1>This shared video is no longer available.</h1>
      <p>The source may have been replaced, removed, or the link may be incomplete.</p>
      <a href="<?= share_escape($mainSiteUrl) ?>">Visit <?= share_escape($ownerName) ?></a>
    </section>
<?php endif; ?>
  </main>

  <footer class="share-footer">
    <span>Shared securely by link</span>
    <a href="<?= share_escape($mainSiteUrl) ?>"><?= share_escape($ownerName) ?></a>
  </footer>
<?php if ($available): ?>
  <script src="<?= share_escape($publicBaseUrl) ?>/assets/hls.min.js?v=1.6.16"></script>
  <script src="<?= share_escape($publicBaseUrl) ?>/assets/share.js?v=1.0.0"></script>
<?php endif; ?>
</body>
</html>
