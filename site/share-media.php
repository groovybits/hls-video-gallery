<?php
declare(strict_types=1);

require __DIR__ . '/.share-common.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, max-age=0');
header('Pragma: no-cache');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET') {
    share_json_error(405, 'GET required');
}

$item = share_find_item_by_token((string)($_GET['t'] ?? ''));
if ($item === null) {
    share_json_error(404, 'This shared video is unavailable');
}

try {
    $access = share_cdn_access($item);
    $poster = share_asset_url($item, $access, (string)($item['poster_url'] ?? ''));
} catch (Throwable $error) {
    share_json_error(503, 'This shared video is temporarily unavailable');
}

$variant = is_array(($item['hls_variants'] ?? [])[0] ?? null) ? $item['hls_variants'][0] : [];
echo json_encode([
    'title' => share_pretty_title($item),
    'duration_seconds' => (float)($item['duration_seconds'] ?? 0),
    'quality' => (string)($variant['name'] ?? 'HLS'),
    'poster_url' => $poster,
    'hls_url' => $access['hls_url'],
    'expires_at' => $access['expires_at'],
], JSON_UNESCAPED_SLASHES);
