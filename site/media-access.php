<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: private, no-store, max-age=0');
header('Pragma: no-cache');
header('X-Content-Type-Options: nosniff');

function fail_response(int $status, string $message): void
{
    http_response_code($status);
    echo json_encode(['error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}

function load_json_file(string $path): array
{
    $raw = @file_get_contents($path);
    if ($raw === false) {
        return [];
    }
    $value = json_decode($raw, true);
    return is_array($value) ? $value : [];
}

function load_env_file(string $path): array
{
    $values = [];
    $lines = @file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if ($lines === false) {
        return $values;
    }
    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#' || strpos($line, '=') === false) {
            continue;
        }
        [$key, $value] = explode('=', $line, 2);
        $key = trim($key);
        $value = trim($value);
        if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $key)) {
            continue;
        }
        if (strlen($value) >= 2 && (($value[0] === '"' && substr($value, -1) === '"') || ($value[0] === "'" && substr($value, -1) === "'"))) {
            $value = substr($value, 1, -1);
        }
        $values[$key] = $value;
    }
    return $values;
}

function sign_directory_url(string $url, string $securityKey, int $expires, string $allowedPath): string
{
    $parsed = parse_url($url);
    if (!is_array($parsed) || empty($parsed['scheme']) || empty($parsed['host']) || empty($parsed['path'])) {
        throw new InvalidArgumentException('Invalid CDN URL');
    }
    $parameters = ['token_path' => $allowedPath];
    ksort($parameters);
    $signingParts = [];
    $urlParts = [];
    foreach ($parameters as $key => $value) {
        $signingParts[] = $key . '=' . $value;
        $urlParts[] = $key . '=' . rawurlencode($value);
    }
    $message = $allowedPath . $expires . implode('&', $signingParts);
    $digest = hash_hmac('sha256', $message, $securityKey, true);
    $token = 'HS256-' . rtrim(strtr(base64_encode($digest), '+/', '-_'), '=');
    $base = $parsed['scheme'] . '://' . $parsed['host'];
    return $base . '/bcdn_token=' . $token . '&' . implode('&', $urlParts) . '&expires=' . $expires . $parsed['path'];
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET') {
    fail_response(405, 'GET required');
}

$id = (string)($_GET['id'] ?? '');
$version = (string)($_GET['version'] ?? '');
if (!preg_match('/^[0-9a-f]{18}$/', $id) || !preg_match('/^[0-9a-f]{14}$/', $version)) {
    fail_response(400, 'Invalid media identifier');
}

$catalog = load_json_file(__DIR__ . '/data/catalog.json');
$item = null;
foreach (($catalog['items'] ?? []) as $candidate) {
    if (is_array($candidate) && ($candidate['id'] ?? '') === $id && ($candidate['version'] ?? '') === $version) {
        $item = $candidate;
        break;
    }
}
if ($item === null) {
    fail_response(404, 'This video is no longer available');
}

$cacheKey = (string)($item['cache_key'] ?? '');
if (!preg_match('/^[0-9a-f]{18}--[0-9a-f]{14}$/', $cacheKey)) {
    fail_response(500, 'The media cache reference is invalid');
}

$originBase = 'cache/' . $cacheKey . '/';
$response = [
    'mode' => 'origin',
    'cache_key' => $cacheKey,
    'base_url' => $originBase,
    'hls_url' => $originBase . 'hls/master.m3u8',
    'expires_at' => time() + 90,
];

$runtime = load_json_file(__DIR__ . '/data/runtime.json');
$privateDir = is_string($runtime['private_dir'] ?? null) ? $runtime['private_dir'] : '';
$config = $privateDir !== '' ? load_env_file($privateDir . '/bunny-signing.env') : [];
$corsReady = in_array(strtolower((string)($config['BUNNY_CORS_READY'] ?? '')), ['1', 'true', 'yes', 'on'], true);
if (!$corsReady) {
    echo json_encode($response, JSON_UNESCAPED_SLASHES);
    exit;
}

$map = load_json_file(__DIR__ . '/data/cdn-map.json');
$record = $map['entries'][$cacheKey] ?? null;
if (!is_array($record)) {
    echo json_encode($response, JSON_UNESCAPED_SLASHES);
    exit;
}

$remotePrefix = (string)($record['remote_prefix'] ?? '');
$revision = (string)($record['revision'] ?? '');
$expectedPrefix = 'hls-video-gallery/v1/cache/' . $cacheKey . '/';
if (strpos($remotePrefix, $expectedPrefix) !== 0 || !preg_match('/^[0-9a-f]{16}$/', $revision) || substr($remotePrefix, -1) !== '/') {
    fail_response(500, 'The CDN media mapping is invalid');
}

$cdnHost = strtolower((string)($config['BUNNY_CDN_HOST'] ?? ''));
$tokenKey = (string)($config['BUNNY_TOKEN_KEY'] ?? '');
if (!preg_match('/^[a-z0-9.-]+$/', $cdnHost) || $tokenKey === '') {
    fail_response(503, 'CDN signing is not configured');
}

$duration = max(0, (int)ceil((float)($item['duration_seconds'] ?? 0)));
$ttl = min(86400, max(7200, $duration + 3600));
$expires = time() + $ttl;
$allowedPath = '/' . $remotePrefix;
$unsignedBase = 'https://' . $cdnHost . $allowedPath;

try {
    $signedBase = sign_directory_url($unsignedBase, $tokenKey, $expires, $allowedPath);
} catch (Throwable $error) {
    fail_response(500, 'CDN signing failed');
}

$response = [
    'mode' => 'cdn',
    'cache_key' => $cacheKey,
    'base_url' => $signedBase,
    'hls_url' => $signedBase . 'hls/master.m3u8',
    'expires_at' => $expires,
];
echo json_encode($response, JSON_UNESCAPED_SLASHES);
