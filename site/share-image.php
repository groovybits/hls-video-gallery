<?php
declare(strict_types=1);

require __DIR__ . '/.share-common.php';

header('Cache-Control: no-store, max-age=0');
header('Referrer-Policy: no-referrer');
header('X-Content-Type-Options: nosniff');

$item = share_find_item_by_token((string)($_GET['t'] ?? ''));
if ($item === null) {
    http_response_code(404);
    exit;
}

try {
    $access = share_cdn_access($item);
    $poster = share_asset_url($item, $access, (string)($item['poster_url'] ?? ''));
} catch (Throwable $error) {
    http_response_code(503);
    exit;
}

header('Location: ' . $poster, true, 302);
exit;
