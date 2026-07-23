#!/usr/bin/env python3
"""Validate gallery.json and render an installable, secret-free build tree."""

import argparse
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlparse


APP_VERSION = "1.4.1"
PIPELINE_VERSION = 6
CONTENT_ANALYZER_BASE_VERSION = "mobileclip2-s0-configurable-v1"
PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
INSTANCE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


class ConfigError(RuntimeError):
    pass


def load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("Configuration file does not exist: {}".format(path))
    except (OSError, ValueError) as error:
        raise ConfigError("Cannot read valid JSON from {}: {}".format(path, error))
    if not isinstance(value, dict):
        raise ConfigError("The top level of gallery.json must be an object")
    return value


def get(config, dotted, default=None):
    value = config
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def required_text(config, dotted):
    value = get(config, dotted)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("{} must be a non-empty string".format(dotted))
    value = value.strip()
    if any(ord(character) < 32 for character in value):
        raise ConfigError("{} contains a control character".format(dotted))
    return value


def bool_value(config, dotted, default=False):
    value = get(config, dotted, default)
    if not isinstance(value, bool):
        raise ConfigError("{} must be true or false".format(dotted))
    return value


def int_value(config, dotted, default, minimum, maximum):
    value = get(config, dotted, default)
    if isinstance(value, bool):
        raise ConfigError("{} must be a number".format(dotted))
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError("{} must be a whole number".format(dotted))
    if not minimum <= value <= maximum:
        raise ConfigError("{} must be between {} and {}".format(dotted, minimum, maximum))
    return value


def float_value(config, dotted, default, minimum, maximum):
    value = get(config, dotted, default)
    if isinstance(value, bool):
        raise ConfigError("{} must be a number".format(dotted))
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError("{} must be a number".format(dotted))
    if not minimum <= value <= maximum:
        raise ConfigError("{} must be between {} and {}".format(dotted, minimum, maximum))
    return value


def normalized_url(value, field, trailing_slash=False):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError("{} must be an absolute https:// URL without credentials".format(field))
    if parsed.query or parsed.fragment:
        raise ConfigError("{} cannot contain a query string or fragment".format(field))
    path = re.sub(r"/+", "/", parsed.path or "/")
    if trailing_slash:
        path = path.rstrip("/") + "/"
    else:
        path = path.rstrip("/")
    port = ":{}".format(parsed.port) if parsed.port else ""
    return "https://{}{}{}".format(parsed.hostname.lower(), port, path)


def color(config, name, default):
    value = str(get(config, "theme." + name, default)).strip()
    if not HEX_COLOR.fullmatch(value):
        raise ConfigError("theme.{} must be a six-digit hex color such as #2f81f7".format(name))
    return value.lower()


def color_rgb(value):
    return ", ".join(str(int(value[index:index + 2], 16)) for index in (1, 3, 5))


def php_single_quote(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def apache_quote(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def resolve_input_path(repo_root, config_path, configured, field, optional=False):
    if not configured:
        if optional:
            return None
        raise ConfigError("{} is required".format(field))
    candidate = Path(str(configured)).expanduser()
    if not candidate.is_absolute():
        from_config = (config_path.parent / candidate).resolve()
        from_repo = (repo_root / candidate).resolve()
        candidate = from_config if from_config.exists() else from_repo
    else:
        candidate = candidate.resolve()
    if not candidate.is_file():
        raise ConfigError("{} does not point to a readable file: {}".format(field, candidate))
    return candidate


def load_taxonomy(path):
    payload = load_json(path)
    tags = payload.get("tags")
    if not isinstance(tags, list) or not tags:
        raise ConfigError("The content taxonomy must contain a non-empty tags array")
    validated = []
    seen = set()
    for index, tag in enumerate(tags):
        prefix = "taxonomy.tags[{}]".format(index)
        if not isinstance(tag, dict):
            raise ConfigError("{} must be an object".format(prefix))
        key = str(tag.get("key") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", key) or key in seen or key == "uncategorized":
            raise ConfigError("{}.key is invalid, duplicated, or reserved".format(prefix))
        seen.add(key)
        label = str(tag.get("label") or "").strip()
        group = str(tag.get("group") or "").strip()
        positive = tag.get("positive")
        negative = tag.get("negative")
        patterns = tag.get("filename_patterns", [])
        threshold = tag.get("threshold", 0.76)
        if not label or not group:
            raise ConfigError("{} needs label and group".format(prefix))
        if not isinstance(positive, list) or not positive or not all(isinstance(item, str) and item.strip() for item in positive):
            raise ConfigError("{}.positive must contain one or more prompts".format(prefix))
        if not isinstance(negative, list) or not negative or not all(isinstance(item, str) and item.strip() for item in negative):
            raise ConfigError("{}.negative must contain one or more prompts".format(prefix))
        if not isinstance(patterns, list) or not all(isinstance(item, str) and item.strip() for item in patterns):
            raise ConfigError("{}.filename_patterns must be an array of phrases".format(prefix))
        try:
            threshold = float(threshold)
        except (TypeError, ValueError, OverflowError):
            raise ConfigError("{}.threshold must be a number".format(prefix))
        if not 0.5 <= threshold <= 0.99:
            raise ConfigError("{}.threshold must be between 0.5 and 0.99".format(prefix))
        validated.append({
            "key": key,
            "label": label,
            "group": group,
            "threshold": threshold,
            "filename_patterns": [item.strip() for item in patterns],
            "positive": [item.strip() for item in positive],
            "negative": [item.strip() for item in negative],
        })
    return validated


def content_analyzer_version(tags):
    canonical = json.dumps(
        tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return CONTENT_ANALYZER_BASE_VERSION + "-" + hashlib.sha256(canonical).hexdigest()[:12]


def validate(repo_root, config_path, config):
    if get(config, "schema_version") != 1:
        raise ConfigError("gallery.json schema_version must be 1")
    instance_id = required_text(config, "instance_id").lower()
    if not INSTANCE_ID.fullmatch(instance_id):
        raise ConfigError("instance_id must be 1-40 lowercase letters, numbers, or hyphens")

    document_root = Path(required_text(config, "install.document_root")).expanduser()
    if not document_root.is_absolute():
        raise ConfigError("install.document_root must be an absolute path")
    document_root = Path(os.path.normpath(str(document_root)))
    if any(character.isspace() for character in str(document_root)):
        raise ConfigError("install.document_root cannot contain whitespace")
    if str(document_root) in {"/", "/home", "/var", "/var/www", "/srv", "/srv/www"}:
        raise ConfigError("install.document_root is too broad; choose an isolated gallery directory")
    owner = required_text(config, "install.owner")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", owner):
        raise ConfigError("install.owner is not a valid Unix account name")
    private_configured = str(get(config, "install.private_dir", "") or "").strip()
    private_dir = Path(private_configured).expanduser() if private_configured else Path("/etc/hls-video-gallery") / instance_id
    if not private_dir.is_absolute() or str(private_dir) in {"/", "/etc", "/home", "/var"}:
        raise ConfigError("install.private_dir must be a specific absolute directory")
    private_dir = Path(os.path.normpath(str(private_dir)))
    if any(character.isspace() for character in str(private_dir)):
        raise ConfigError("install.private_dir cannot contain whitespace")
    try:
        private_dir.relative_to(document_root)
    except ValueError:
        pass
    else:
        raise ConfigError("install.private_dir must be outside the public document root")

    public_base_url = normalized_url(required_text(config, "site.public_base_url"), "site.public_base_url")
    main_site_url = normalized_url(required_text(config, "site.main_site_url"), "site.main_site_url", trailing_slash=True)
    parsed_base = urlparse(public_base_url)
    base_path = parsed_base.path.rstrip("/") or ""
    language = required_text(config, "site.language")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise ConfigError("site.language is invalid")

    brand_keys = (
        "owner_name", "gallery_name", "header_subtitle", "main_site_label",
        "hero_eyebrow", "hero_title", "hero_body", "loading_title",
        "loading_body", "empty_title", "empty_body", "video_singular",
        "video_plural", "share_message",
    )
    brand = {key: required_text(config, "brand." + key) for key in brand_keys}

    theme_defaults = {
        "background": "#0d1117", "panel": "#161b22", "panel_alt": "#21262d",
        "line": "#30363d", "text": "#f0f6fc", "muted": "#9da7b3",
        "accent": "#2f81f7", "accent_alt": "#79c0ff", "success": "#56d364",
        "danger": "#ff7b72",
    }
    theme = {name: color(config, name, default) for name, default in theme_defaults.items()}

    basic_auth = bool_value(config, "access.basic_auth", True)
    public_landing = bool_value(config, "site.public_landing", True)
    public_share_links = bool_value(config, "access.public_share_links", False)
    cdn_provider = str(get(config, "cdn.provider", "none") or "none").strip().lower()
    if cdn_provider not in {"none", "bunny"}:
        raise ConfigError("cdn.provider must be either none or bunny")
    if public_share_links and cdn_provider != "bunny":
        raise ConfigError("Password-free public share links require cdn.provider to be bunny")

    preset = str(get(config, "encoding.preset", "superfast")).strip().lower()
    if preset not in PRESETS:
        raise ConfigError("encoding.preset must be one of {}".format(", ".join(sorted(PRESETS))))
    encoding = {
        "max_height": int_value(config, "encoding.max_height", 1080, 144, 4320),
        "preset": preset,
        "video_bitrate": int_value(config, "encoding.video_bitrate", 6_500_000, 250_000, 100_000_000),
        "audio_bitrate": int_value(config, "encoding.audio_bitrate", 160_000, 32_000, 512_000),
        "thumbnail_interval": int_value(config, "encoding.thumbnail_interval", 10, 1, 3600),
        "thumbnail_width": int_value(config, "encoding.thumbnail_width", 480, 160, 3840),
        "hls_segment_seconds": int_value(config, "encoding.hls_segment_seconds", 6, 2, 30),
        "settle_seconds": int_value(config, "encoding.settle_seconds", 30, 0, 86400),
        "failure_retry_seconds": int_value(config, "encoding.failure_retry_seconds", 300, 60, 604800),
        "cache_retention_seconds": int_value(config, "encoding.cache_retention_seconds", 86400, 3600, 31536000),
    }
    gallery = {
        "page_size": int_value(config, "gallery.page_size", 10, 1, 100),
        "autoplay": bool_value(config, "gallery.autoplay", True),
        "unmuted": bool_value(config, "gallery.unmuted", True),
        "show_encoder_status": bool_value(config, "gallery.show_encoder_status", True),
        "show_content_analysis": bool_value(config, "gallery.show_content_analysis", False),
        "show_quality_analysis": bool_value(config, "gallery.show_quality_analysis", False),
    }
    title_words = get(config, "gallery.title_words", {})
    if not isinstance(title_words, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
        for key, value in title_words.items()
    ):
        raise ConfigError("gallery.title_words must be an object of input-to-display strings")
    gallery["title_words"] = {str(key).lower(): str(value) for key, value in title_words.items()}

    analysis_enabled = bool_value(config, "content_analysis.enabled", False)
    taxonomy_path = resolve_input_path(
        repo_root, config_path, required_text(config, "content_analysis.taxonomy"),
        "content_analysis.taxonomy",
    )
    taxonomy = load_taxonomy(taxonomy_path)
    content_analysis = {
        "enabled": analysis_enabled,
        "items_per_run": int_value(config, "content_analysis.items_per_run", 4, 1, 100),
        "interval_seconds": int_value(config, "content_analysis.interval_seconds", 210, 30, 86400),
        "max_load": float_value(config, "content_analysis.max_load", 1.5, 0.1, 100.0),
        "threads": int_value(config, "content_analysis.threads", 1, 1, 64),
        "taxonomy_path": taxonomy_path,
        "tags": taxonomy,
        "analyzer_version": content_analyzer_version(taxonomy),
    }
    quality_analysis = {
        "enabled": bool_value(config, "quality_analysis.enabled", False),
        "items_per_run": int_value(config, "quality_analysis.items_per_run", 1, 1, 20),
        "interval_seconds": int_value(config, "quality_analysis.interval_seconds", 1, 1, 86400),
        "max_load": float_value(config, "quality_analysis.max_load", 0.0, 0.0, 100.0),
        "threads": int_value(config, "quality_analysis.threads", 2, 1, 2),
        "frame_rate": int_value(config, "quality_analysis.frame_rate", 30, 1, 120),
        "scene_threshold": float_value(config, "quality_analysis.scene_threshold", 10.0, 0.1, 100.0),
        "min_scene_seconds": float_value(config, "quality_analysis.min_scene_seconds", 2.0, 0.1, 120.0),
        "failure_retry_seconds": int_value(
            config, "quality_analysis.failure_retry_seconds", 30, 1, 604800,
        ),
    }

    profile_path = resolve_input_path(
        repo_root, config_path, str(get(config, "brand.profile_image", "") or "").strip(),
        "brand.profile_image", optional=True,
    )
    social_path = resolve_input_path(
        repo_root, config_path, str(get(config, "brand.social_image", "") or "").strip(),
        "brand.social_image", optional=True,
    )
    for path, field in ((profile_path, "brand.profile_image"), (social_path, "brand.social_image")):
        if path and path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ConfigError("{} must be JPG, PNG, or WebP".format(field))

    cdn_config = None
    if cdn_provider == "bunny":
        cdn_config = resolve_input_path(
            repo_root, config_path, required_text(config, "cdn.config_file"),
            "cdn.config_file",
        )

    return {
        "instance_id": instance_id,
        "document_root": document_root,
        "owner": owner,
        "private_dir": private_dir,
        "public_base_url": public_base_url,
        "public_origin": "{}://{}".format(parsed_base.scheme, parsed_base.netloc),
        "base_path": base_path,
        "main_site_url": main_site_url,
        "language": language,
        "public_landing": public_landing,
        "brand": brand,
        "theme": theme,
        "basic_auth": basic_auth,
        "public_share_links": public_share_links,
        "realm": required_text(config, "access.realm"),
        "gallery": gallery,
        "encoding": encoding,
        "content_analysis": content_analysis,
        "quality_analysis": quality_analysis,
        "cdn_provider": cdn_provider,
        "cdn_config": cdn_config,
        "profile_path": profile_path,
        "social_path": social_path,
    }


def replace_tokens(path, tokens):
    text = path.read_text(encoding="utf-8")
    for token, value in tokens.items():
        text = text.replace("@@" + token + "@@", str(value))
    unresolved = sorted(set(re.findall(r"@@([A-Z0-9_]+)@@", text)))
    if unresolved:
        raise ConfigError("{} has unresolved template tokens: {}".format(path, ", ".join(unresolved)))
    path.write_text(text, encoding="utf-8")


def copy_brand_asset(source, assets_dir, stem):
    if source is None:
        return ""
    destination = assets_dir / (stem + source.suffix.lower())
    shutil.copyfile(str(source), str(destination))
    return "assets/" + destination.name


def render(repo_root, config_path, output_dir):
    config = load_json(config_path)
    values = validate(repo_root, config_path, config)
    if output_dir.exists():
        shutil.rmtree(str(output_dir))
    output_dir.mkdir(parents=True)
    site_output = output_dir / "site"
    shutil.copytree(str(repo_root / "site"), str(site_output))
    systemd_output = output_dir / "systemd"
    shutil.copytree(str(repo_root / "systemd"), str(systemd_output))

    profile_url = copy_brand_asset(values["profile_path"], site_output / "assets", "brand-profile")
    social_url = copy_brand_asset(values["social_path"], site_output / "assets", "social-card")
    if not social_url and (site_output / "assets" / "default-social-card.png").is_file():
        social_url = "assets/default-social-card.png"

    public_config = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "site": {
            "public_base_url": values["public_base_url"],
            "base_path": values["base_path"],
            "main_site_url": values["main_site_url"],
            "language": values["language"],
        },
        "brand": copy.deepcopy(values["brand"]),
        "theme": values["theme"],
        "features": {
            "public_landing": values["public_landing"],
            "basic_auth": values["basic_auth"],
            "share_links": values["public_share_links"],
            "encoder_status": values["gallery"]["show_encoder_status"],
            "content_analysis": values["gallery"]["show_content_analysis"],
            "quality_analysis": values["gallery"]["show_quality_analysis"],
            "autoplay": values["gallery"]["autoplay"],
            "unmuted": values["gallery"]["unmuted"],
        },
        "gallery": {
            "page_size": values["gallery"]["page_size"],
            "title_words": values["gallery"]["title_words"],
        },
        "profile_image": profile_url,
        "social_image": social_url,
        "content_tags": [
            {
                "key": tag["key"], "label": tag["label"], "group": tag["group"],
                "filename_patterns": tag["filename_patterns"],
            }
            for tag in values["content_analysis"]["tags"]
        ],
    }
    public_config["content_tags"].append({
        "key": "uncategorized", "label": "Uncategorized", "group": "Other",
        "filename_patterns": [],
    })
    config_json = json.dumps(public_config, ensure_ascii=False, indent=2)
    (site_output / "data" / "site-config.json").write_text(config_json + "\n", encoding="utf-8")
    (site_output / "data" / "runtime.json").write_text(
        json.dumps({"private_dir": str(values["private_dir"])}, indent=2) + "\n",
        encoding="utf-8",
    )
    (site_output / "assets" / "config.js").write_text(
        "window.HLS_GALLERY_CONFIG = {};\n".format(config_json), encoding="utf-8",
    )
    taxonomy_output = {
        "schema_version": 1,
        "tags": values["content_analysis"]["tags"],
    }
    (site_output / "data" / "content-tags.json").write_text(
        json.dumps(taxonomy_output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    theme = values["theme"]
    theme_css = """:root {{
  --bg: {background};
  --panel: {panel};
  --panel-2: {panel_alt};
  --line: {line};
  --text: {text};
  --muted: {muted};
  --accent: {accent};
  --accent-2: {accent_alt};
  --success: {success};
  --danger: {danger};
  --ink: {text};
  --accent-soft: {accent_alt};
  --accent-rgb: {accent_rgb};
  --accent-2-rgb: {accent_alt_rgb};
}}
""".format(
        **theme,
        accent_rgb=color_rgb(theme["accent"]),
        accent_alt_rgb=color_rgb(theme["accent_alt"]),
    )
    (site_output / "assets" / "theme.css").write_text(theme_css, encoding="utf-8")

    brand = values["brand"]
    social_absolute = values["public_base_url"] + "/" + social_url if social_url else ""
    profile_absolute = values["public_base_url"] + "/" + profile_url if profile_url else ""
    preview_image = social_absolute or profile_absolute
    base_path_regex = re.escape(values["base_path"] or "")
    public_landing_rules = ""
    public_file_pattern = ""
    if values["public_landing"]:
        public_landing_rules = """<If \"%{{REQUEST_URI}} =~ m#^{}/?$#\">\n  AuthType None\n  Require all granted\n</If>""".format(base_path_regex)
        public_file_pattern = "|preview\\.html|preview\\.css|preview\\.js|default-social-card\\.png|brand-profile\\.(?:png|jpe?g|webp)|social-card\\.(?:png|jpe?g|webp)"
    public_share_rules = ""
    public_php_pattern = "a^"
    if values["public_share_links"]:
        public_share_rules = """<If \"%{{REQUEST_URI}} =~ m#^{}/watch/[^/]+/?$#\">\n  AuthType None\n  Require all granted\n</If>""".format(base_path_regex)
        public_php_pattern = "(?:share|share-media|share-image)"
    auth_block = ""
    if values["basic_auth"]:
        auth_block = """AuthType Basic\nAuthBasicProvider file\nAuthName \"{}\"\nAuthUserFile {}\nRequire valid-user""".format(
            apache_quote(values["realm"]), values["private_dir"] / "users.htpasswd",
        )
    else:
        auth_block = "Require all granted"

    cdn_host = ""
    if values["cdn_provider"] == "bunny" and values["cdn_config"]:
        for line in values["cdn_config"].read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("BUNNY_CDN_HOST="):
                cdn_host = line.split("=", 1)[1].strip().strip("\"'").lower()
                break
        if cdn_host and not re.fullmatch(r"[a-z0-9.-]+", cdn_host):
            raise ConfigError("BUNNY_CDN_HOST in bunny.env is invalid")
    csp_cdn = " https://" + cdn_host if cdn_host else ""

    tokens = {
        "APP_VERSION": APP_VERSION,
        "LANGUAGE": html.escape(values["language"], quote=True),
        "THEME_COLOR": theme["background"],
        "GALLERY_NAME": html.escape(brand["gallery_name"], quote=True),
        "OWNER_NAME": html.escape(brand["owner_name"], quote=True),
        "HEADER_SUBTITLE": html.escape(brand["header_subtitle"], quote=True),
        "MAIN_SITE_URL": html.escape(values["main_site_url"], quote=True),
        "MAIN_SITE_LABEL": html.escape(brand["main_site_label"], quote=True),
        "LOADING_TITLE": html.escape(brand["loading_title"], quote=True),
        "LOADING_BODY": html.escape(brand["loading_body"], quote=True),
        "PUBLIC_BASE_URL": html.escape(values["public_base_url"], quote=True),
        "PUBLIC_ORIGIN": html.escape(values["public_origin"], quote=True),
        "BASE_PATH": html.escape(values["base_path"], quote=True),
        "BASE_PATH_REGEX": base_path_regex,
        "PROFILE_URL": html.escape(profile_url, quote=True),
        "PREVIEW_IMAGE_URL": html.escape(preview_image, quote=True),
        "DESCRIPTION": html.escape(brand["hero_body"], quote=True),
        "HERO_EYEBROW": html.escape(brand["hero_eyebrow"], quote=True),
        "HERO_TITLE": html.escape(brand["hero_title"], quote=True),
        "HERO_BODY": html.escape(brand["hero_body"], quote=True),
        "AUTH_BLOCK": auth_block,
        "PUBLIC_LANDING_RULES": public_landing_rules,
        "PUBLIC_SHARE_RULES": public_share_rules,
        "PUBLIC_FILE_PATTERN": public_file_pattern,
        "PUBLIC_PHP_PATTERN": public_php_pattern,
        "DIRECTORY_INDEX": "preview.html" if values["public_landing"] else "index.html",
        "HTTPS_HOST": parsed_host(values["public_base_url"]),
        "CSP_CDN": csp_cdn,
        "PRIVATE_DIR_PHP": php_single_quote(str(values["private_dir"])),
    }
    for relative in ("index.html", "preview.html", ".htaccess", ".share-common.php", "share.php"):
        replace_tokens(site_output / relative, tokens)
    for path in systemd_output.iterdir():
        if path.is_file():
            replace_tokens(path, {
                "INSTANCE_ID": values["instance_id"],
                "OWNER": values["owner"],
                "DOCUMENT_ROOT": str(values["document_root"]),
                "PRIVATE_DIR": str(values["private_dir"]),
                "ANALYZER_CACHE": str(values["private_dir"] / "model-cache"),
                "ANALYZER_ITEMS": values["content_analysis"]["items_per_run"],
                "ANALYZER_INTERVAL": values["content_analysis"]["interval_seconds"],
                "ANALYZER_MAX_LOAD": values["content_analysis"]["max_load"],
                "ANALYZER_THREADS": values["content_analysis"]["threads"],
                "QUALITY_ITEMS": values["quality_analysis"]["items_per_run"],
                "QUALITY_INTERVAL": values["quality_analysis"]["interval_seconds"],
                "QUALITY_MAX_LOAD": values["quality_analysis"]["max_load"],
                "QUALITY_THREADS": values["quality_analysis"]["threads"],
                "QUALITY_FRAME_RATE": values["quality_analysis"]["frame_rate"],
                "QUALITY_SCENE_THRESHOLD": values["quality_analysis"]["scene_threshold"],
                "QUALITY_MIN_SCENE_SECONDS": values["quality_analysis"]["min_scene_seconds"],
                "QUALITY_FAILURE_RETRY_SECONDS": values["quality_analysis"]["failure_retry_seconds"],
                "QUALITY_REQUIRE_CONTENT": "true" if values["content_analysis"]["enabled"] else "false",
                "QUALITY_EXPECTED_CONTENT_VERSION": values["content_analysis"]["analyzer_version"],
                "VIDEO_MAX_HEIGHT": values["encoding"]["max_height"],
                "VIDEO_PRESET": values["encoding"]["preset"],
                "VIDEO_BITRATE": values["encoding"]["video_bitrate"],
                "AUDIO_BITRATE": values["encoding"]["audio_bitrate"],
                "THUMB_INTERVAL": values["encoding"]["thumbnail_interval"],
                "THUMB_WIDTH": values["encoding"]["thumbnail_width"],
                "SEGMENT_SECONDS": values["encoding"]["hls_segment_seconds"],
                "SETTLE_SECONDS": values["encoding"]["settle_seconds"],
                "FAILURE_RETRY_SECONDS": values["encoding"]["failure_retry_seconds"],
                "CACHE_RETENTION_SECONDS": values["encoding"]["cache_retention_seconds"],
            })

    install_payload = {
        "app_version": APP_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "instance_id": values["instance_id"],
        "document_root": str(values["document_root"]),
        "owner": values["owner"],
        "private_dir": str(values["private_dir"]),
        "basic_auth": values["basic_auth"],
        "public_landing": values["public_landing"],
        "public_share_links": values["public_share_links"],
        "cdn_provider": values["cdn_provider"],
        "cdn_config": str(values["cdn_config"]) if values["cdn_config"] else "",
        "content_analysis_enabled": values["content_analysis"]["enabled"],
        "quality_analysis_enabled": values["quality_analysis"]["enabled"],
        "encoding": values["encoding"],
        "encoding_sha256": hashlib.sha256(json.dumps(
            values["encoding"], sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }
    (output_dir / "install.json").write_text(
        json.dumps(install_payload, indent=2) + "\n", encoding="utf-8",
    )
    return install_payload


def parsed_host(url):
    return urlparse(url).netloc


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to gallery.json")
    parser.add_argument("--output", required=True, help="empty/generated build directory")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    repo_root = Path(arguments.repo_root).expanduser().resolve()
    config_path = Path(arguments.config).expanduser().resolve()
    output_dir = Path(arguments.output).expanduser().resolve()
    try:
        payload = render(repo_root, config_path, output_dir)
    except ConfigError as error:
        print("Configuration error: {}".format(error), file=sys.stderr)
        return 2
    print("Rendered {} {} for {}".format(payload["instance_id"], payload["app_version"], payload["document_root"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
