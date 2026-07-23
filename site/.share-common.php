<?php
declare(strict_types=1);

const HLS_GALLERY_PRIVATE_DIR = '@@PRIVATE_DIR_PHP@@';

function share_load_json(string $path): array
{
    $raw = @file_get_contents($path);
    if ($raw === false) {
        return [];
    }
    $value = json_decode($raw, true);
    return is_array($value) ? $value : [];
}

function share_load_env(string $path): array
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

function share_site_config(): array
{
    static $config = null;
    if ($config === null) {
        $config = share_load_json(__DIR__ . '/data/site-config.json');
    }
    return is_array($config) ? $config : [];
}

function share_config_value(string $section, string $key, string $fallback = ''): string
{
    $config = share_site_config();
    $value = $config[$section][$key] ?? $fallback;
    return is_scalar($value) ? (string)$value : $fallback;
}

function share_signing_key(): string
{
    $path = HLS_GALLERY_PRIVATE_DIR . '/share.key';
    $key = trim((string)@file_get_contents($path));
    if (!preg_match('/^[0-9a-f]{64}$/', $key)) {
        throw new RuntimeException('Video sharing is not configured');
    }
    return $key;
}

function share_base64url(string $value): string
{
    return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
}

function share_token_for_cache(string $cacheKey): string
{
    if (!preg_match('/^[0-9a-f]{18}--[0-9a-f]{14}$/', $cacheKey)) {
        throw new InvalidArgumentException('Invalid cache key');
    }
    $binaryKey = hex2bin(share_signing_key());
    if ($binaryKey === false) {
        throw new RuntimeException('Invalid sharing key');
    }
    return share_base64url(hash_hmac('sha256', "hls-video-gallery-share-v1\n" . $cacheKey, $binaryKey, true));
}

function share_catalog(): array
{
    return share_load_json(__DIR__ . '/data/catalog.json');
}

function share_find_item(string $id, string $version): ?array
{
    if (!preg_match('/^[0-9a-f]{18}$/', $id) || !preg_match('/^[0-9a-f]{14}$/', $version)) {
        return null;
    }
    foreach ((share_catalog()['items'] ?? []) as $item) {
        if (is_array($item) && ($item['id'] ?? '') === $id && ($item['version'] ?? '') === $version) {
            return $item;
        }
    }
    return null;
}

function share_find_item_by_token(string $token): ?array
{
    if (!preg_match('/^[A-Za-z0-9_-]{43}$/', $token)) {
        return null;
    }
    foreach ((share_catalog()['items'] ?? []) as $item) {
        if (!is_array($item)) {
            continue;
        }
        $cacheKey = (string)($item['cache_key'] ?? '');
        if (!preg_match('/^[0-9a-f]{18}--[0-9a-f]{14}$/', $cacheKey)) {
            continue;
        }
        try {
            $expected = share_token_for_cache($cacheKey);
        } catch (Throwable $error) {
            return null;
        }
        if (hash_equals($expected, $token)) {
            return $item;
        }
    }
    return null;
}

function share_pretty_title(array $item): string
{
    $title = (string)($item['title'] ?? 'Shared video');
    $title = (string)preg_replace('/\.[A-Za-z0-9]{2,5}$/', '', basename($title));
    $title = (string)preg_replace('/[_-]+/', ' ', $title);
    $title = trim((string)preg_replace('/\s+/', ' ', $title));
    $words = preg_split('/\s+/', strtolower($title)) ?: [];
    $config = share_site_config();
    $special = is_array($config['gallery']['title_words'] ?? null) ? $config['gallery']['title_words'] : [
        '4k' => '4K', '8k' => '8K', 'hd' => 'HD', 'hdr' => 'HDR',
        'hls' => 'HLS', 'pov' => 'POV', 'uhd' => 'UHD',
    ];
    foreach ($words as $index => $word) {
        if (isset($special[$word])) {
            $words[$index] = $special[$word];
        } elseif (preg_match('/^\d/', $word)) {
            $words[$index] = $word;
        } else {
            $words[$index] = ucfirst($word);
        }
    }
    return $words ? implode(' ', $words) : 'Shared video';
}

function share_sign_directory_url(string $url, string $securityKey, int $expires, string $allowedPath): string
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
    $token = 'HS256-' . share_base64url($digest);
    $base = $parsed['scheme'] . '://' . $parsed['host'];
    return $base . '/bcdn_token=' . $token . '&' . implode('&', $urlParts) . '&expires=' . $expires . $parsed['path'];
}

function share_cdn_access(array $item): array
{
    $cacheKey = (string)($item['cache_key'] ?? '');
    if (!preg_match('/^[0-9a-f]{18}--[0-9a-f]{14}$/', $cacheKey)) {
        throw new RuntimeException('Invalid media cache');
    }

    $config = share_load_env(HLS_GALLERY_PRIVATE_DIR . '/bunny-signing.env');
    $corsReady = in_array(strtolower((string)($config['BUNNY_CORS_READY'] ?? '')), ['1', 'true', 'yes', 'on'], true);
    $cdnHost = strtolower((string)($config['BUNNY_CDN_HOST'] ?? ''));
    $tokenKey = (string)($config['BUNNY_TOKEN_KEY'] ?? '');
    if (!$corsReady || !preg_match('/^[a-z0-9.-]+$/', $cdnHost) || $tokenKey === '') {
        throw new RuntimeException('CDN sharing is unavailable');
    }

    $map = share_load_json(__DIR__ . '/data/cdn-map.json');
    $record = $map['entries'][$cacheKey] ?? null;
    if (!is_array($record)) {
        throw new RuntimeException('This video is still uploading to the CDN');
    }
    $remotePrefix = (string)($record['remote_prefix'] ?? '');
    $revision = (string)($record['revision'] ?? '');
    $expectedPrefix = 'hls-video-gallery/v1/cache/' . $cacheKey . '/';
    if (strpos($remotePrefix, $expectedPrefix) !== 0 || !preg_match('/^[0-9a-f]{16}$/', $revision) || substr($remotePrefix, -1) !== '/') {
        throw new RuntimeException('Invalid CDN mapping');
    }

    $duration = max(0, (int)ceil((float)($item['duration_seconds'] ?? 0)));
    $ttl = min(86400, max(7200, $duration + 3600));
    $expires = time() + $ttl;
    $allowedPath = '/' . $remotePrefix;
    $unsignedBase = 'https://' . $cdnHost . $allowedPath;
    $signedBase = share_sign_directory_url($unsignedBase, $tokenKey, $expires, $allowedPath);
    return [
        'cache_key' => $cacheKey,
        'base_url' => $signedBase,
        'hls_url' => $signedBase . 'hls/master.m3u8',
        'expires_at' => $expires,
    ];
}

function share_asset_url(array $item, array $access, string $catalogPath): string
{
    $prefix = 'cache/' . (string)($item['cache_key'] ?? '') . '/';
    if (strpos($catalogPath, $prefix) !== 0) {
        throw new RuntimeException('Invalid media asset');
    }
    return (string)$access['base_url'] . substr($catalogPath, strlen($prefix));
}

function share_public_url(string $token): string
{
    $base = rtrim(share_config_value('site', 'public_base_url'), '/');
    if ($base === '') {
        throw new RuntimeException('The public gallery URL is not configured');
    }
    return $base . '/watch/' . rawurlencode($token);
}

function share_json_error(int $status, string $message): void
{
    http_response_code($status);
    echo json_encode(['error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}
