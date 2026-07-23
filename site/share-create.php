<?php
declare(strict_types=1);

require __DIR__ . '/.share-common.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: private, no-store, max-age=0');
header('Pragma: no-cache');
header('X-Content-Type-Options: nosniff');

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET') {
    share_json_error(405, 'GET required');
}

$item = share_find_item((string)($_GET['id'] ?? ''), (string)($_GET['version'] ?? ''));
if ($item === null) {
    share_json_error(404, 'This video is no longer available');
}

try {
    share_cdn_access($item);
    $token = share_token_for_cache((string)$item['cache_key']);
} catch (Throwable $error) {
    share_json_error(409, 'This video is still being prepared for sharing. Please try again shortly.');
}

echo json_encode([
    'url' => share_public_url($token),
    'title' => share_pretty_title($item),
    'access' => 'anyone_with_link',
    'invalidates_when_source_changes' => true,
], JSON_UNESCAPED_SLASHES);
