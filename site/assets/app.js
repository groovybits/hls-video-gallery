(function () {
  "use strict";

  var app = document.getElementById("app");
  var headerStatus = document.getElementById("header-status");
  var updatedNode = document.getElementById("catalog-updated");
  var catalog = null;
  var contentIndex = { items: {}, analyzed_count: 0, pending_count: 0 };
  var activeHls = null;
  var mediaAccessCache = {};
  var shareLinkCache = {};
  var encodeProgress = null;
  var categoryProgress = null;
  var qualityProgress = null;
  var telemetryTimer = null;
  var categoryTimer = null;
  var qualityTimer = null;
  var catalogTimer = null;
  var contentIndexTimer = null;
  var CONFIG = window.HLS_GALLERY_CONFIG || {};
  var SITE = CONFIG.site || {};
  var BRAND = CONFIG.brand || {};
  var FEATURES = CONFIG.features || {};
  var GALLERY = CONFIG.gallery || {};
  var PAGE_SIZE = Math.max(1, Number(GALLERY.page_size) || 10);
  var CONTENT_TAGS = Array.isArray(CONFIG.content_tags) ? CONFIG.content_tags : [
    { key: "uncategorized", label: "Uncategorized", group: "Other", filename_patterns: [] }
  ];
  function escapePattern(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }
  var ACTIVITY_RULES = CONTENT_TAGS.filter(function (tag) {
    return tag.key !== "uncategorized" && Array.isArray(tag.filename_patterns) && tag.filename_patterns.length;
  }).map(function (tag) {
    return {
      key: tag.key,
      label: tag.label,
      pattern: new RegExp(tag.filename_patterns.map(escapePattern).join("|"), "i")
    };
  });
  var libraryState = { query: "", sort: "newest", tags: [], tagMode: "all", tagSource: "all", length: "all", page: 1 };
  var shuffleState = { active: false, order: [], position: 0, signature: "" };
  var tagDrawerOpen = false;
  var monitorUiState = {
    queueOpen: false,
    queueScrollTop: 0,
    commandOpen: false,
    commandScrollTop: 0,
    copiedUntil: 0
  };
  var categoryUiState = { queueOpen: false, queueScrollTop: 0 };
  var qualityUiState = {
    queueOpen: false,
    queueScrollTop: 0,
    commandOpen: false,
    commandScrollTop: 0,
    detailsOpen: false,
    copiedUntil: 0
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i += 1) {
      if (arguments[i]) parent.appendChild(arguments[i]);
    }
    return parent;
  }

  function brandText(key, fallback) {
    var value = BRAND[key];
    return typeof value === "string" && value.trim() ? value.trim() : fallback;
  }

  function videoNoun(count) {
    return Number(count) === 1
      ? brandText("video_singular", "video")
      : brandText("video_plural", "videos");
  }

  function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(value);
    }
    return new Promise(function (resolve, reject) {
      var field = document.createElement("textarea");
      field.value = value;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      try {
        if (!document.execCommand("copy")) throw new Error("Copy command was rejected");
        resolve();
      } catch (error) {
        reject(error);
      } finally {
        field.remove();
      }
    });
  }

  function formatDuration(value) {
    var seconds = Math.max(0, Math.round(Number(value) || 0));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var remainder = seconds % 60;
    if (hours) return hours + ":" + String(minutes).padStart(2, "0") + ":" + String(remainder).padStart(2, "0");
    return minutes + ":" + String(remainder).padStart(2, "0");
  }

  function formatNumber(value, decimals) {
    var amount = Number(value);
    if (!Number.isFinite(amount)) amount = 0;
    return amount.toFixed(decimals === undefined ? 1 : decimals);
  }

  function formatPercent(value) {
    return formatNumber(Math.min(100, Math.max(0, Number(value) || 0)), 1) + "%";
  }

  function formatLongDuration(value) {
    var seconds = Math.max(0, Math.round(Number(value) || 0));
    if (!seconds) return "Calculating…";
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var parts = [];
    if (days) parts.push(days + "d");
    if (hours || days) parts.push(hours + "h");
    parts.push(minutes + "m");
    return parts.join(" ");
  }

  function formatFinishTime(value) {
    if (!value) return "Calculating…";
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Calculating…";
    return new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit"
    }).format(date);
  }

  function formatBytes(value) {
    var bytes = Number(value) || 0;
    if (!bytes) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    var amount = bytes / Math.pow(1024, index);
    return amount.toFixed(amount >= 10 || index === 0 ? 0 : 1) + " " + units[index];
  }

  function formatBitrate(value) {
    var bits = Number(value) || 0;
    if (!bits) return "Unknown";
    if (bits >= 1000000) return (bits / 1000000).toFixed(bits >= 10000000 ? 0 : 1) + " Mb/s";
    return Math.round(bits / 1000) + " kb/s";
  }

  function formatDate(value, includeTime) {
    if (!value) return "Unknown";
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Unknown";
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: includeTime ? "numeric" : undefined,
      minute: includeTime ? "2-digit" : undefined
    }).format(date);
  }

  function titleCaseCodec(value) {
    if (!value) return "Unknown";
    var known = { h264: "H.264", hevc: "H.265 / HEVC", vp9: "VP9", av1: "AV1", aac: "AAC", mp3: "MP3", opus: "Opus", prores: "ProRes" };
    return known[String(value).toLowerCase()] || String(value).toUpperCase();
  }

  function prettyVideoTitle(value) {
    var text = String(value || "Untitled video").split(/[\\/]/).pop().replace(/\.[a-z0-9]{2,5}$/i, "");
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    function readableDate(year, month, day) {
      var monthIndex = Number(month) - 1;
      var dayNumber = Number(day);
      if (monthIndex < 0 || monthIndex > 11 || dayNumber < 1 || dayNumber > 31) return null;
      return months[monthIndex] + " " + dayNumber + ", " + year;
    }
    text = text.replace(/\b(20\d{2})[_-]?(\d{2})[_-]?(\d{2})\b/g, function (match, year, month, day) {
      return readableDate(year, month, day) || match;
    });
    text = text.replace(/\b(\d{1,2})[_-](\d{1,2})[_-](20\d{2})\b/g, function (match, month, day, year) {
      return readableDate(year, month, day) || match;
    });
    text = text
      .replace(/[_-]+/g, " ")
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/\s+/g, " ")
      .trim();

    var special = GALLERY.title_words || { "4k": "4K", "8k": "8K", hls: "HLS", hdr: "HDR", pov: "POV", hd: "HD", uhd: "UHD" };
    var minor = { a: true, an: true, and: true, at: true, for: true, in: true, of: true, on: true, the: true, to: true, with: true };
    return text.split(" ").map(function (word, index) {
      if (/^[A-Z][a-z]{2} \d{1,2},$/.test(word)) return word;
      var lower = word.toLocaleLowerCase();
      if (special[lower]) return special[lower];
      if (index && minor[lower]) return lower;
      if (/^\d/.test(word)) return word;
      return lower.charAt(0).toLocaleUpperCase() + lower.slice(1);
    }).join(" ");
  }

  function resolution(item) {
    var stream = item.video_streams && item.video_streams[0];
    return stream && stream.width && stream.height ? stream.width + "×" + stream.height : "Unknown";
  }

  function filenameWords(item) {
    return (String(item.title || "") + " " + String(item.source_relative || ""))
      .replace(/[_./\\-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function visualRecordFor(item) {
    var record = (contentIndex.items || {})[item.id];
    if (!record || record.cache_key !== item.cache_key || !Array.isArray(record.tags)) return null;
    return record;
  }

  function filenameActivitiesFor(item) {
    var words = filenameWords(item);
    return ACTIVITY_RULES.filter(function (rule) { return rule.pattern.test(words); }).map(function (rule) {
      return { key: rule.key, label: rule.label, group: "Activity", source: "filename" };
    });
  }

  function analyzedActivitiesFor(item) {
    var record = visualRecordFor(item);
    return (record ? record.tags : []).filter(function (tag) {
      return tag && tag.key;
    }).map(function (tag) {
      var normalized = Object.assign({}, tag);
      normalized.source = normalized.source || "visual";
      return normalized;
    });
  }

  function activitiesFor(item) {
    var activities = analyzedActivitiesFor(item);
    var keys = activities.map(function (tag) { return tag.key; });
    filenameActivitiesFor(item).forEach(function (tag) {
      if (keys.indexOf(tag.key) !== -1) return;
      activities.push(tag);
      keys.push(tag.key);
    });
    return activities.length ? activities : [{ key: "uncategorized", label: "Uncategorized", group: "Other", source: "none" }];
  }

  function activitiesForSource(item, source) {
    if (source === "visual") return analyzedActivitiesFor(item);
    if (source === "filename") return filenameActivitiesFor(item);
    return activitiesFor(item);
  }

  function confidenceText(tag) {
    var confidence = Number(tag && tag.confidence);
    return Number.isFinite(confidence) ? Math.round(confidence * 100) + "%" : "";
  }

  function analysisSourceLabel(tag) {
    if (tag.source === "manual") return "Manually curated";
    if (tag.source === "derived") return "Analysis inferred";
    return "Visually detected";
  }

  function durationClass(item) {
    var seconds = Number(item.duration_seconds) || 0;
    if (seconds < 300) return { key: "short", label: "Short", description: "under 5 min" };
    if (seconds < 1200) return { key: "medium", label: "Medium", description: "5–20 min" };
    return { key: "long", label: "Long", description: "20+ min" };
  }

  function loadLibraryStateFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var validSorts = ["newest", "oldest", "uploaded-newest", "uploaded-oldest", "name", "activity", "shortest", "longest", "size"];
    var tagKeys = CONTENT_TAGS.map(function (tag) { return tag.key; });
    var sort = params.get("sort") || "newest";
    if (sort === "duration") sort = "longest";
    libraryState.query = params.get("q") || "";
    libraryState.sort = validSorts.indexOf(sort) === -1 ? "newest" : sort;
    libraryState.tags = (params.get("tags") || "").split(",").filter(function (key, index, values) {
      return tagKeys.indexOf(key) !== -1 && values.indexOf(key) === index;
    });
    var legacyActivity = params.get("activity");
    if (!libraryState.tags.length && tagKeys.indexOf(legacyActivity) !== -1) libraryState.tags = [legacyActivity];
    libraryState.tagMode = params.get("tagmode") === "any" ? "any" : "all";
    libraryState.tagSource = ["visual", "filename"].indexOf(params.get("tagsource")) !== -1 ? params.get("tagsource") : "all";
    libraryState.length = ["all", "short", "medium", "long"].indexOf(params.get("length")) === -1 ? "all" : params.get("length");
    libraryState.page = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
  }

  function syncLibraryUrl() {
    var url = new URL(window.location.href);
    function setOptional(name, value, defaultValue) {
      if (value && value !== defaultValue) url.searchParams.set(name, value);
      else url.searchParams.delete(name);
    }
    setOptional("q", libraryState.query.trim(), "");
    setOptional("sort", libraryState.sort, "newest");
    setOptional("tags", libraryState.tags.join(","), "");
    setOptional("tagmode", libraryState.tagMode, "all");
    setOptional("tagsource", libraryState.tagSource, "all");
    url.searchParams.delete("activity");
    setOptional("length", libraryState.length, "all");
    setOptional("page", String(libraryState.page), "1");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
  }

  function shuffleRequested() {
    return new URLSearchParams(window.location.search).get("shuffle") === "1";
  }

  function resetShuffleState() {
    shuffleState.active = false;
    shuffleState.order = [];
    shuffleState.position = 0;
    shuffleState.signature = "";
  }

  function routeHref(videoId, shuffleMode) {
    var url = new URL(window.location.href);
    if (videoId) url.searchParams.set("video", videoId);
    else url.searchParams.delete("video");
    if (shuffleMode === true) url.searchParams.set("shuffle", "1");
    if (shuffleMode === false) url.searchParams.delete("shuffle");
    return url.pathname + url.search + url.hash;
  }

  function currentVideoId() {
    return new URLSearchParams(window.location.search).get("video");
  }

  function mediaAssetSuffix(item, catalogUrl) {
    var prefix = "cache/" + item.cache_key + "/";
    var value = String(catalogUrl || "");
    if (value.indexOf(prefix) !== 0) throw new Error("Unexpected media asset path");
    return value.slice(prefix.length);
  }

  function originMediaAccess(item) {
    var base = "cache/" + item.cache_key + "/";
    return {
      mode: "origin",
      cache_key: item.cache_key,
      base_url: base,
      hls_url: base + "hls/master.m3u8",
      expires_at: Math.floor(Date.now() / 1000) + 300
    };
  }

  function mediaAssetUrl(item, access, catalogUrl) {
    return access.base_url + mediaAssetSuffix(item, catalogUrl);
  }

  function loadMediaAccess(item) {
    var cached = mediaAccessCache[item.cache_key];
    var now = Math.floor(Date.now() / 1000);
    if (cached && cached.value && Number(cached.value.expires_at || 0) > now + 60) {
      return Promise.resolve(cached.value);
    }
    if (cached && cached.promise) return cached.promise;

    var endpoint = "media-access.php?id=" + encodeURIComponent(item.id) + "&version=" + encodeURIComponent(item.version);
    var promise = fetch(endpoint, { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Media access returned " + response.status);
        return response.json();
      })
      .then(function (access) {
        if (!access || access.cache_key !== item.cache_key || !access.base_url || !access.hls_url) {
          throw new Error("Media access response is invalid");
        }
        mediaAccessCache[item.cache_key] = { value: access };
        return access;
      })
      .catch(function () {
        var fallback = originMediaAccess(item);
        mediaAccessCache[item.cache_key] = { value: fallback };
        return fallback;
      });
    mediaAccessCache[item.cache_key] = { promise: promise };
    return promise;
  }

  function loadShareLink(item) {
    if (shareLinkCache[item.cache_key]) return Promise.resolve(shareLinkCache[item.cache_key]);
    var endpoint = "share-create.php?id=" + encodeURIComponent(item.id) + "&version=" + encodeURIComponent(item.version);
    return fetch(endpoint, { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.error || "Share link could not be prepared");
          return data;
        });
      })
      .then(function (data) {
        var candidate;
        var expected;
        try {
          candidate = new URL(String(data && data.url || ""));
          expected = new URL(String(SITE.public_base_url || window.location.href));
        } catch (_error) {
          throw new Error("Share link response is invalid");
        }
        var expectedPrefix = expected.pathname.replace(/\/+$/, "") + "/watch/";
        if (
          !data ||
          candidate.protocol !== "https:" ||
          candidate.origin !== expected.origin ||
          candidate.pathname.indexOf(expectedPrefix) !== 0 ||
          !/^[A-Za-z0-9_-]{43}$/.test(candidate.pathname.slice(expectedPrefix.length))
        ) {
          throw new Error("Share link response is invalid");
        }
        shareLinkCache[item.cache_key] = data;
        return data;
      });
  }

  function buildShareButton(item, className, defaultLabel) {
    if (!FEATURES.share_links) return null;
    var button = el("button", className, defaultLabel);
    button.type = "button";
    button.title = "Copy a password-free link that opens only this video";
    button.setAttribute("aria-label", "Copy password-free share link for " + prettyVideoTitle(item.title || item.source_relative));
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      button.disabled = true;
      button.textContent = "Preparing…";
      loadShareLink(item).then(function (share) {
        return copyText(share.url).then(function () {
          button.textContent = "Link copied ✓";
        }).catch(function () {
          window.prompt("Copy this password-free video link:", share.url);
          button.textContent = "Link ready ✓";
        });
      }).catch(function (error) {
        button.textContent = error && error.message ? error.message : "Try again shortly";
      }).finally(function () {
        window.setTimeout(function () {
          if (!button.isConnected) return;
          button.textContent = defaultLabel;
          button.disabled = false;
        }, 3200);
      });
    });
    return button;
  }

  function navigate(videoId, options) {
    options = options || {};
    var url = new URL(window.location.href);
    if (videoId) url.searchParams.set("video", videoId);
    else url.searchParams.delete("video");
    if (options.shuffle === true) url.searchParams.set("shuffle", "1");
    if (options.shuffle === false || !videoId) {
      url.searchParams.delete("shuffle");
      if (!videoId || options.shuffle === false) resetShuffleState();
    }
    window.history.pushState({}, "", url.pathname + url.search + url.hash);
    renderRoute();
    if (!videoId) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      if (FEATURES.encoder_status) loadEncodingProgress();
      if (FEATURES.content_analysis) loadCategoryProgress();
      if (FEATURES.quality_analysis !== false) loadQualityProgress();
    }
  }

  function destroyPlayer() {
    if (activeHls) {
      activeHls.destroy();
      activeHls = null;
    }
  }

  function showError(title, message) {
    destroyPlayer();
    app.replaceChildren();
    var section = el("section", "error-state");
    append(section, el("p", "eyebrow", "Library unavailable"), el("h1", "", title), el("p", "", message));
    app.appendChild(section);
    headerStatus.textContent = "Unable to load catalog";
  }

  function buildHero(items) {
    var hero = el("section", "library-hero");
    var copy = el("div", "hero-copy");
    append(copy,
      el("p", "eyebrow", brandText("hero_eyebrow", "Personal collection")),
      el("h1", "", brandText("hero_title", "A private place for my videos.")),
      el("p", "", brandText("hero_body", "Browse a phone-friendly collection with smooth HLS playback."))
    );
    var portrait = null;
    if (CONFIG.profile_image) {
      portrait = el("div", "hero-profile");
      var portraitImage = el("img");
      portraitImage.src = CONFIG.profile_image;
      portraitImage.alt = brandText("owner_name", "Gallery owner");
      portraitImage.loading = "eager";
      portraitImage.addEventListener("error", function () {
        portrait.remove();
        hero.classList.add("no-profile");
      });
      append(portrait, portraitImage, el("span", "profile-label", brandText("owner_name", "Gallery owner")));
    } else {
      hero.classList.add("no-profile");
    }
    var count = el("div", "hero-count");
    append(count, el("strong", "", items.length), el("span", "", videoNoun(items.length)));
    return append(hero, copy, portrait, count);
  }

  function buildEncodingMonitor() {
    var section = el("section", "encoding-monitor");
    section.classList.add("library-monitor");
    section.id = "encode-monitor";
    section.setAttribute("aria-label", "Video encoding status");

    if (!encodeProgress) {
      section.classList.add("telemetry-loading");
      append(section, el("span", "monitor-pulse"), el("p", "", "Checking the encoder…"));
      return section;
    }

    var active = Boolean(encodeProgress.active);
    section.classList.toggle("is-idle", !active);
    var top = el("div", "monitor-top");
    var heading = el("div", "monitor-heading");
    var badgeRow = el("div", "monitor-badge-row");
    var badge = el("span", "monitor-badge " + (active ? "is-active" : "is-idle"), active ? "Live encode" : "Encoder idle");
    badgeRow.appendChild(badge);
    if (active) badgeRow.appendChild(el("span", "monitor-pass-badge", encodeProgress.pass_label || "Processing"));
    append(heading,
      badgeRow,
      el("h2", "", active ? encodeProgress.phase_label : "Everything is caught up"),
      el("p", "monitor-source", active ? prettyVideoTitle(encodeProgress.source_name || encodeProgress.source) : (encodeProgress.note || "No FFmpeg job is running."))
    );
    var queue = encodeProgress.queue || {};
    var libraryTotal = queue.library_total != null ? queue.library_total : queue.total;
    var libraryReady = queue.library_ready != null ? queue.library_ready : (queue.ready || queue.completed || 0);
    var libraryPublished = queue.library_published != null ? queue.library_published : (queue.published || 0);
    var queueTag = el("div", "monitor-queue");
    append(queueTag,
      el("strong", "", active ? (queue.position || 0) + " / " + (queue.total || 0) : String(libraryTotal || 0)),
      el("span", "", active ? "current queue" : "source videos"),
      el("small", "", active ? (queue.completed || 0) + " processed · " + (queue.remaining_after_current || 0) + " after this" : libraryPublished + " live · " + libraryReady + " ready")
    );
    append(top, heading, queueTag);
    section.appendChild(top);

    if (!active) return section;

    var metrics = el("div", "monitor-metrics");
    [
      [formatNumber(encodeProgress.processing_fps, 1), "processing fps"],
      [formatNumber(encodeProgress.speed, 2) + "×", "real-time speed"],
      [formatPercent(encodeProgress.percent), encodeProgress.pass_total === 1 ? "single-pass progress" : "current pass"],
      [formatDuration(encodeProgress.eta_seconds), "phase ETA"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-metric");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      metrics.appendChild(card);
    });
    section.appendChild(metrics);

    var progressWrap = el("div", "monitor-progress-wrap");
    var progressLabels = el("div", "monitor-progress-labels");
    append(progressLabels,
      el("span", "", (encodeProgress.pass_label || "Current pass") + " · " + formatDuration(encodeProgress.position_seconds) + " / " + formatDuration(encodeProgress.duration_seconds)),
      el("span", "", formatPercent(encodeProgress.percent) + " of this pass")
    );
    var track = el("div", "monitor-progress");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Number(encodeProgress.percent) || 0));
    var fill = el("span");
    fill.style.width = formatPercent(encodeProgress.percent);
    track.appendChild(fill);
    append(progressWrap, progressLabels, track);
    section.appendChild(progressWrap);

    var overallWrap = el("div", "monitor-progress-wrap monitor-overall-progress");
    var overallLabels = el("div", "monitor-progress-labels");
    append(overallLabels,
      el("span", "", "Full library · " + libraryReady + " ready of " + libraryTotal),
      el("span", "", formatPercent(encodeProgress.overall_percent) + " complete")
    );
    var overallTrack = el("div", "monitor-progress is-overall");
    overallTrack.setAttribute("role", "progressbar");
    overallTrack.setAttribute("aria-label", "Full library scan progress");
    overallTrack.setAttribute("aria-valuemin", "0");
    overallTrack.setAttribute("aria-valuemax", "100");
    overallTrack.setAttribute("aria-valuenow", String(Number(encodeProgress.overall_percent) || 0));
    var overallFill = el("span");
    overallFill.style.width = formatPercent(encodeProgress.overall_percent);
    overallTrack.appendChild(overallFill);
    append(overallWrap, overallLabels, overallTrack);
    section.appendChild(overallWrap);

    var forecast = el("div", "monitor-forecast");
    var pace = el("div", "monitor-pace");
    var paceRing = el("div", "monitor-pace-ring");
    paceRing.style.setProperty("--pace", Math.min(100, Math.max(0, Number(encodeProgress.speed) || 0) * 100) + "%");
    var paceCore = el("div", "monitor-pace-core");
    append(paceCore,
      el("strong", "", formatNumber(encodeProgress.speed, 2) + "×"),
      el("span", "", "real time")
    );
    paceRing.appendChild(paceCore);
    append(pace, paceRing, el("p", "", "Observed HLS pace"));

    var durationReady = Boolean(queue.duration_index_complete);
    var timeGrid = el("div", "monitor-time-grid");
    [
      [formatLongDuration(queue.total_duration_seconds), "current queue runtime"],
      [formatLongDuration(queue.remaining_duration_seconds), "media still queued"],
      [durationReady ? formatLongDuration(queue.predicted_processing_seconds) : "Indexing…", "predicted processing"],
      [durationReady ? formatFinishTime(queue.predicted_finish_at) : (queue.duration_indexed_count || 0) + " / " + (queue.total || 0), durationReady ? "estimated finish" : "durations indexed"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-time-card");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      timeGrid.appendChild(card);
    });
    append(forecast, pace, timeGrid);
    section.appendChild(forecast);

    var columns = el("div", "monitor-columns");
    var previewSection = el("div", "monitor-preview-section");
    previewSection.appendChild(el("h3", "", "Current 10-second frame"));
    if (encodeProgress.preview_url) {
      var preview = el("div", "monitor-preview");
      var previewImage = el("img");
      previewImage.src = encodeProgress.preview_url;
      previewImage.alt = "Preview near the current encode point in " + encodeProgress.source_name;
      previewImage.decoding = "async";
      append(preview, previewImage, el("span", "", formatDuration(encodeProgress.preview_time_seconds)));
      previewSection.appendChild(preview);
    } else {
      previewSection.appendChild(el("div", "monitor-preview-empty", "Waiting for the first timeline frame…"));
    }
    var parameterSection = el("div", "monitor-parameters");
    parameterSection.appendChild(el("h3", "", "FFmpeg parameters"));
    var parameterList = el("dl", "monitor-parameter-list");
    Object.keys(encodeProgress.parameters || {}).forEach(function (label) {
      append(parameterList, el("dt", "", label), el("dd", "", encodeProgress.parameters[label]));
    });
    parameterSection.appendChild(parameterList);

    var upcomingSection = el("div", "monitor-upcoming");
    upcomingSection.appendChild(el("h3", "", "Up next · " + (queue.order_label || "queue order")));
    var upcoming = queue.upcoming || [];
    if (upcoming.length) {
      var previewList = el("ol", "monitor-upcoming-preview");
      previewList.start = 1;
      upcoming.slice(0, 3).forEach(function (name) {
        var item = el("li");
        item.title = name;
        item.appendChild(el("span", "", prettyVideoTitle(name)));
        previewList.appendChild(item);
      });
      upcomingSection.appendChild(previewList);
      if (upcoming.length > 3) {
        var drawer = el("details", "queue-drawer");
        drawer.open = monitorUiState.queueOpen;
        var queueSummary = el("summary", "", drawer.open ? "Hide complete queue" : "Browse all " + upcoming.length + " queued videos");
        drawer.appendChild(queueSummary);
        var fullList = el("ol", "queue-full-list");
        fullList.start = 1;
        upcoming.forEach(function (name) {
          var fullItem = el("li");
          fullItem.title = name;
          fullItem.appendChild(el("span", "", prettyVideoTitle(name)));
          fullList.appendChild(fullItem);
        });
        fullList.scrollTop = monitorUiState.queueScrollTop;
        fullList.addEventListener("scroll", function () {
          monitorUiState.queueScrollTop = fullList.scrollTop;
        }, { passive: true });
        drawer.addEventListener("toggle", function () {
          monitorUiState.queueOpen = drawer.open;
          queueSummary.textContent = drawer.open ? "Hide complete queue" : "Browse all " + upcoming.length + " queued videos";
        });
        drawer.appendChild(fullList);
        upcomingSection.appendChild(drawer);
      }
    } else {
      upcomingSection.appendChild(el("p", "monitor-none", "Nothing else is queued after this file."));
    }
    append(columns, previewSection, parameterSection, upcomingSection);
    section.appendChild(columns);

    var commandBar = el("div", "monitor-command-bar");
    var details = el("details", "monitor-command");
    details.open = monitorUiState.commandOpen;
    var commandSummary = el("summary", "", details.open ? "Hide active FFmpeg command" : "Show active FFmpeg command");
    details.appendChild(commandSummary);
    var pre = el("pre");
    pre.appendChild(el("code", "", encodeProgress.command || "Command unavailable"));
    pre.scrollTop = monitorUiState.commandScrollTop;
    pre.addEventListener("scroll", function () {
      monitorUiState.commandScrollTop = pre.scrollTop;
    }, { passive: true });
    details.addEventListener("toggle", function () {
      monitorUiState.commandOpen = details.open;
      commandSummary.textContent = details.open ? "Hide active FFmpeg command" : "Show active FFmpeg command";
    });
    details.appendChild(pre);
    var copyButton = el("button", "copy-command", Date.now() < monitorUiState.copiedUntil ? "Copied ✓" : "Copy FFmpeg command");
    copyButton.type = "button";
    copyButton.addEventListener("click", function () {
      copyButton.disabled = true;
      copyText(encodeProgress.command || "").then(function () {
        monitorUiState.copiedUntil = Date.now() + 2500;
        copyButton.textContent = "Copied ✓";
        window.setTimeout(function () {
          if (copyButton.isConnected) {
            copyButton.textContent = "Copy FFmpeg command";
            copyButton.disabled = false;
          }
        }, 2500);
      }).catch(function () {
        copyButton.textContent = "Select command below to copy";
        details.open = true;
        monitorUiState.commandOpen = true;
        copyButton.disabled = false;
      });
    });
    append(commandBar, details, copyButton);
    section.appendChild(commandBar);
    section.appendChild(el("p", "monitor-note", encodeProgress.note));
    return section;
  }

  function refreshEncodingMonitor() {
    var existing = document.getElementById("encode-monitor");
    if (existing) {
      var oldQueue = existing.querySelector(".queue-full-list");
      var oldCommand = existing.querySelector(".monitor-command pre");
      if (oldQueue) monitorUiState.queueScrollTop = oldQueue.scrollTop;
      if (oldCommand) monitorUiState.commandScrollTop = oldCommand.scrollTop;
      var replacement = buildEncodingMonitor();
      existing.replaceWith(replacement);
      var newQueue = replacement.querySelector(".queue-full-list");
      var newCommand = replacement.querySelector(".monitor-command pre");
      if (newQueue) newQueue.scrollTop = monitorUiState.queueScrollTop;
      if (newCommand) newCommand.scrollTop = monitorUiState.commandScrollTop;
    }
  }

  function buildCategoryMonitor() {
    var section = el("section", "encoding-monitor category-monitor library-monitor");
    section.id = "category-monitor";
    section.setAttribute("aria-label", "Video category analysis status");

    var progress = categoryProgress;
    if (!progress) {
      var indexed = Number(contentIndex.analyzed_count || 0);
      var pending = Number(contentIndex.pending_count || 0);
      if (indexed || pending) {
        progress = {
          state: "waiting", phase_label: "Category index is available",
          analyzed_count: indexed, pending_count: pending,
          catalog_count: indexed + pending,
          percent: indexed + pending ? 100 * indexed / (indexed + pending) : 100,
          model: "MobileCLIP2-S0", upcoming: []
        };
      } else {
        section.classList.add("telemetry-loading");
        append(section, el("span", "monitor-pulse"), el("p", "", "Checking category analysis…"));
        return section;
      }
    }

    var state = String(progress.state || "unknown");
    var active = state === "analyzing";
    var complete = state === "complete" || Number(progress.pending_count || 0) === 0;
    var failed = state === "error";
    var categoryPercent = Number(progress.percent);
    if (!Number.isFinite(categoryPercent)) {
      categoryPercent = Number(progress.catalog_count) ? 100 * Number(progress.analyzed_count || 0) / Number(progress.catalog_count) : 100;
    }
    section.classList.toggle("is-idle", !active);
    section.classList.toggle("is-complete", complete);
    section.classList.toggle("is-error", failed);

    var top = el("div", "monitor-top");
    var heading = el("div", "monitor-heading");
    var badgeText = active ? "Live categorization" : (complete ? "Categories complete" : (failed ? "Analyzer needs attention" : "Category queue"));
    var badgeRow = el("div", "monitor-badge-row");
    badgeRow.appendChild(el("span", "monitor-badge " + (active ? "is-active" : "is-idle"), badgeText));
    if (active && progress.batch_total) {
      badgeRow.appendChild(el("span", "monitor-pass-badge", "Batch " + (progress.batch_position || 1) + " of " + progress.batch_total));
    }
    var sourceText = progress.source ? prettyVideoTitle(progress.source) : (progress.reason || (complete ? "Every cached video has category metadata." : "Completed results stay cached while the next batch waits."));
    append(heading,
      badgeRow,
      el("h2", "", progress.phase_label || (active ? "Analyzing cached thumbnail frames" : "Visual category analysis")),
      el("p", "monitor-source", sourceText)
    );
    var count = el("div", "monitor-queue category-count");
    append(count,
      el("strong", "", (progress.analyzed_count || 0) + " / " + (progress.catalog_count || 0)),
      el("span", "", "videos categorized"),
      el("small", "", (progress.pending_count || 0) + " pending")
    );
    append(top, heading, count);
    section.appendChild(top);

    var metrics = el("div", "monitor-metrics category-metrics");
    [
      [formatPercent(categoryPercent), "library categorized"],
      [String(progress.pending_count || 0), "videos pending"],
      [formatNumber(progress.videos_per_hour, 1), "videos per hour"],
      [complete ? "Complete" : formatLongDuration(progress.eta_seconds), "estimated remaining"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-metric");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      metrics.appendChild(card);
    });
    section.appendChild(metrics);

    var overallWrap = el("div", "monitor-progress-wrap");
    var overallLabels = el("div", "monitor-progress-labels");
    append(overallLabels,
      el("span", "", (progress.analyzed_count || 0) + " analyzed of " + (progress.catalog_count || 0) + " videos"),
      el("span", "", formatPercent(categoryPercent) + " complete")
    );
    var overallTrack = el("div", "monitor-progress category-progress");
    overallTrack.setAttribute("role", "progressbar");
    overallTrack.setAttribute("aria-label", "Category analysis progress");
    overallTrack.setAttribute("aria-valuemin", "0");
    overallTrack.setAttribute("aria-valuemax", "100");
    overallTrack.setAttribute("aria-valuenow", String(categoryPercent));
    var overallFill = el("span");
    overallFill.style.width = formatPercent(categoryPercent);
    overallTrack.appendChild(overallFill);
    append(overallWrap, overallLabels, overallTrack);
    section.appendChild(overallWrap);

    if (active && Number(progress.frames_total || 0) > 0) {
      var frameWrap = el("div", "monitor-progress-wrap category-frame-progress");
      var frameLabels = el("div", "monitor-progress-labels");
      append(frameLabels,
        el("span", "", "Current video · " + (progress.frames_done || 0) + " of " + progress.frames_total + " thumbnail frames"),
        el("span", "", formatPercent(progress.frame_percent))
      );
      var frameTrack = el("div", "monitor-progress is-overall");
      frameTrack.setAttribute("role", "progressbar");
      frameTrack.setAttribute("aria-label", "Current category frame progress");
      frameTrack.setAttribute("aria-valuemin", "0");
      frameTrack.setAttribute("aria-valuemax", "100");
      frameTrack.setAttribute("aria-valuenow", String(Number(progress.frame_percent) || 0));
      var frameFill = el("span");
      frameFill.style.width = formatPercent(progress.frame_percent);
      frameTrack.appendChild(frameFill);
      append(frameWrap, frameLabels, frameTrack);
      section.appendChild(frameWrap);
    }

    var lower = el("div", "category-lower");
    var facts = el("div", "category-facts");
    [
      [formatNumber(progress.average_seconds_per_video, 1) + " sec", "compute per video"],
      [progress.batch_total ? (progress.batch_position || 0) + " / " + progress.batch_total : "—", "current timer batch"],
      [complete ? "Complete" : formatFinishTime(progress.estimated_finish_at), "estimated finish"],
      [progress.model || "MobileCLIP2-S0", "visual model"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-time-card");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      facts.appendChild(card);
    });
    lower.appendChild(facts);

    var upcoming = progress.upcoming || [];
    var queueSection = el("div", "monitor-upcoming category-upcoming");
    queueSection.appendChild(el("h3", "", "Waiting for category analysis"));
    if (upcoming.length) {
      var drawer = el("details", "queue-drawer");
      drawer.open = categoryUiState.queueOpen;
      var summary = el("summary", "", drawer.open ? "Hide category queue" : "Browse all " + upcoming.length + " pending videos");
      var list = el("ol", "queue-full-list");
      upcoming.forEach(function (name) {
        var item = el("li");
        item.title = name;
        item.appendChild(el("span", "", prettyVideoTitle(name)));
        list.appendChild(item);
      });
      list.scrollTop = categoryUiState.queueScrollTop;
      list.addEventListener("scroll", function () { categoryUiState.queueScrollTop = list.scrollTop; }, { passive: true });
      drawer.addEventListener("toggle", function () {
        categoryUiState.queueOpen = drawer.open;
        summary.textContent = drawer.open ? "Hide category queue" : "Browse all " + upcoming.length + " pending videos";
      });
      append(drawer, summary, list);
      queueSection.appendChild(drawer);
    } else {
      queueSection.appendChild(el("p", "monitor-none", complete ? "The category queue is empty." : "The next queue update is being prepared."));
    }
    lower.appendChild(queueSection);
    section.appendChild(lower);
    if (failed && progress.error) section.appendChild(el("p", "category-error", progress.error));
    section.appendChild(el("p", "monitor-note", "Categories use existing 10-second thumbnails; source videos are not decoded again and completed results remain cached."));
    return section;
  }

  function refreshCategoryMonitor() {
    var existing = document.getElementById("category-monitor");
    if (!existing) return;
    var oldQueue = existing.querySelector(".queue-full-list");
    if (oldQueue) categoryUiState.queueScrollTop = oldQueue.scrollTop;
    var replacement = buildCategoryMonitor();
    existing.replaceWith(replacement);
    var newQueue = replacement.querySelector(".queue-full-list");
    if (newQueue) newQueue.scrollTop = categoryUiState.queueScrollTop;
  }

  function loadCategoryProgress() {
    if (currentVideoId()) return;
    fetch("data/content-analysis-progress.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Category telemetry request returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        categoryProgress = data;
        refreshCategoryMonitor();
      })
      .catch(function () {
        if (!categoryProgress) refreshCategoryMonitor();
      });
  }

  function qualityMonitorVisible() {
    if (!qualityProgress || qualityProgress.enabled === false) return false;
    return String(qualityProgress.state || "").toLowerCase() !== "disabled";
  }

  function buildQualityMonitor() {
    if (!qualityMonitorVisible()) return null;

    var progress = qualityProgress;
    var state = String(progress.state || (progress.active ? "analyzing" : "waiting")).toLowerCase();
    var active = Boolean(progress.active) || state === "analyzing" || state === "running" || state === "processing";
    var complete = state === "complete" || state === "idle" && Number(progress.pending_count || 0) === 0;
    var failed = state === "error" || state === "failed";
    var current = progress.current || {};
    var engine = progress.engine || progress;
    var catalogCount = Number(progress.catalog_count || 0);
    var analyzedCount = Number(progress.analyzed_count || 0);
    var pendingCount = Number(progress.pending_count || 0);
    var overallPercent = Number(progress.percent);
    if (!Number.isFinite(overallPercent)) {
      overallPercent = catalogCount ? 100 * analyzedCount / catalogCount : (complete ? 100 : 0);
    }
    var enginePercent = Number(engine.percent);
    if (!Number.isFinite(enginePercent)) enginePercent = 0;
    var frameTotal = Number(engine.frames_total || engine.total_frames || 0);
    var frameDone = Number(engine.frames_done || 0);
    var framePercent = frameTotal ? 100 * frameDone / frameTotal : 0;
    var currentTitle = current.title || current.source_relative || progress.source || "";

    var section = el("section", "encoding-monitor quality-monitor library-monitor");
    section.id = "quality-monitor";
    section.setAttribute("aria-label", "Streaming quality analysis status");
    section.classList.toggle("is-idle", !active);
    section.classList.toggle("is-complete", complete);
    section.classList.toggle("is-error", failed);

    var top = el("div", "monitor-top");
    var heading = el("div", "monitor-heading");
    var badgeRow = el("div", "monitor-badge-row");
    var badgeText = active ? "Live quality analysis" : (complete ? "Quality analysis complete" : (failed ? "Quality analyzer needs attention" : "Quality queue"));
    badgeRow.appendChild(el("span", "monitor-badge " + (active ? "is-active" : "is-idle"), badgeText));
    if (engine.phase || progress.phase) {
      badgeRow.appendChild(el("span", "monitor-pass-badge", engine.phase || progress.phase));
    }
    append(
      heading,
      badgeRow,
      el("h2", "", progress.phase_label || (active ? "Measuring encoded video quality" : "Perceptual quality analysis")),
      el("p", "monitor-source", currentTitle ? prettyVideoTitle(currentTitle) : (
        progress.note || progress.reason || (
          toNumber(progress.pending_count, 0) > 0
            ? "The next queued video will start when the timer runs and resources are free."
            : "Waiting for an encoded video to analyze."
        )
      ))
    );
    var count = el("div", "monitor-queue quality-count");
    var queueTotal = Number(progress.queue_total || 0);
    var queuePosition = Number(progress.queue_position || 0);
    append(
      count,
      el("strong", "", active && queueTotal ? queuePosition + " / " + queueTotal : analyzedCount + " / " + catalogCount),
      el("span", "", active && queueTotal ? "current quality queue" : "quality reports ready"),
      el("small", "", active && queueTotal
        ? analyzedCount + " reports ready · " + Math.max(0, queueTotal - queuePosition) + " after this"
        : pendingCount + " pending · " + Number(progress.waiting_content_count || 0) + " waiting on content")
    );
    append(top, heading, count);
    section.appendChild(top);

    var metrics = el("div", "monitor-metrics quality-live-metrics");
    [
      [formatNumber(engine.fps, 1), "analysis fps"],
      [formatNumber(engine.speed, 2) + "×", "real-time speed"],
      [formatPercent(enginePercent), "current metric"],
      [active ? formatDuration(engine.eta_seconds || progress.eta_seconds) : (complete ? "Complete" : "Waiting"), "estimated remaining"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-metric");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      metrics.appendChild(card);
    });
    section.appendChild(metrics);

    var overallWrap = el("div", "monitor-progress-wrap");
    var overallLabels = el("div", "monitor-progress-labels");
    append(
      overallLabels,
      el("span", "", analyzedCount + " analyzed of " + catalogCount + " videos"),
      el("span", "", formatPercent(overallPercent) + " of the library")
    );
    var overallTrack = el("div", "monitor-progress quality-progress");
    overallTrack.setAttribute("role", "progressbar");
    overallTrack.setAttribute("aria-label", "Library quality analysis progress");
    overallTrack.setAttribute("aria-valuemin", "0");
    overallTrack.setAttribute("aria-valuemax", "100");
    overallTrack.setAttribute("aria-valuenow", String(overallPercent));
    var overallFill = el("span");
    overallFill.style.width = formatPercent(overallPercent);
    overallTrack.appendChild(overallFill);
    append(overallWrap, overallLabels, overallTrack);
    section.appendChild(overallWrap);

    if (active) {
      var metricWrap = el("div", "monitor-progress-wrap quality-current-progress");
      var metricLabels = el("div", "monitor-progress-labels");
      var processedText = Number(engine.duration_seconds || 0) > 0
        ? formatDuration(engine.processed_seconds) + " / " + formatDuration(engine.duration_seconds)
        : formatPercent(enginePercent);
      append(
        metricLabels,
        el("span", "", "Current metric · " + (engine.phase || progress.phase || "measuring")),
        el("span", "", processedText)
      );
      var metricTrack = el("div", "monitor-progress is-overall");
      metricTrack.setAttribute("role", "progressbar");
      metricTrack.setAttribute("aria-label", "Current quality metric progress");
      metricTrack.setAttribute("aria-valuemin", "0");
      metricTrack.setAttribute("aria-valuemax", "100");
      metricTrack.setAttribute("aria-valuenow", String(enginePercent));
      var metricFill = el("span");
      metricFill.style.width = formatPercent(enginePercent);
      metricTrack.appendChild(metricFill);
      append(metricWrap, metricLabels, metricTrack);
      section.appendChild(metricWrap);

      if (frameTotal || Number(engine.scenes_detected || 0)) {
        var sceneWrap = el("div", "monitor-progress-wrap quality-scene-progress");
        var sceneLabels = el("div", "monitor-progress-labels");
        append(
          sceneLabels,
          el("span", "", frameTotal ? frameDone + " of " + frameTotal + " metric frames" : "Scene detection"),
          el("span", "", Number(engine.scenes_detected || 0) + " scenes detected")
        );
        var sceneTrack = el("div", "monitor-progress is-overall");
        sceneTrack.setAttribute("role", "progressbar");
        sceneTrack.setAttribute("aria-label", "Quality frame and scene progress");
        sceneTrack.setAttribute("aria-valuemin", "0");
        sceneTrack.setAttribute("aria-valuemax", "100");
        sceneTrack.setAttribute("aria-valuenow", String(framePercent));
        var sceneFill = el("span");
        sceneFill.style.width = formatPercent(framePercent);
        sceneTrack.appendChild(sceneFill);
        append(sceneWrap, sceneLabels, sceneTrack);
        section.appendChild(sceneWrap);
      }
    }

    var lower = el("div", "quality-monitor-lower");
    var facts = el("div", "category-facts quality-facts");
    [
      [formatNumber(progress.average_seconds_per_video, 1) + " sec", "average per video"],
      [formatDuration(progress.elapsed_seconds), "current run elapsed"],
      [complete ? "Complete" : formatFinishTime(progress.estimated_finish_at), "estimated finish"],
      [Number(engine.scenes_detected || 0), "current scenes"]
    ].forEach(function (metric) {
      var card = el("div", "monitor-time-card");
      append(card, el("strong", "", metric[0]), el("span", "", metric[1]));
      facts.appendChild(card);
    });
    lower.appendChild(facts);

    var upcoming = Array.isArray(progress.upcoming) ? progress.upcoming : [];
    var queueSection = el("div", "monitor-upcoming quality-upcoming");
    queueSection.appendChild(el("h3", "", upcoming.length ? "Up next" : "Quality queue"));
    if (upcoming.length) {
      var drawer = el("details", "queue-drawer");
      drawer.open = qualityUiState.queueOpen;
      var queueSummary = el("summary", "", drawer.open ? "Hide quality queue" : "Browse all " + upcoming.length + " pending videos");
      var queueList = el("ol", "queue-full-list");
      queueList.start = active && queuePosition ? queuePosition + 1 : 1;
      upcoming.forEach(function (name) {
        var queueItem = el("li");
        queueItem.title = name;
        queueItem.appendChild(el("span", "", prettyVideoTitle(name)));
        queueList.appendChild(queueItem);
      });
      queueList.scrollTop = qualityUiState.queueScrollTop;
      queueList.addEventListener("scroll", function () { qualityUiState.queueScrollTop = queueList.scrollTop; }, { passive: true });
      drawer.addEventListener("toggle", function () {
        qualityUiState.queueOpen = drawer.open;
        queueSummary.textContent = drawer.open ? "Hide quality queue" : "Browse all " + upcoming.length + " pending videos";
      });
      append(drawer, queueSummary, queueList);
      queueSection.appendChild(drawer);
    } else {
      queueSection.appendChild(el("p", "monitor-none", complete ? "The quality queue is empty." : "The next quality job is being prepared."));
    }
    lower.appendChild(queueSection);
    section.appendChild(lower);

    var detailBar = el("div", "quality-monitor-details");
    var runDetails = el("details", "monitor-command quality-run-details");
    runDetails.open = qualityUiState.detailsOpen;
    var runSummary = el("summary", "", runDetails.open ? "Hide run details" : "Show run details");
    runDetails.addEventListener("toggle", function () {
      qualityUiState.detailsOpen = runDetails.open;
      runSummary.textContent = runDetails.open ? "Hide run details" : "Show run details";
    });
    var detailList = el("dl", "monitor-parameter-list quality-detail-list");
    [
      ["State", progress.state || "Unknown"],
      ["Phase", engine.phase || progress.phase || "Waiting"],
      ["Run started", formatDate(progress.run_started_at, true)],
      ["Item started", formatDate(progress.item_started_at, true)],
      ["Video ID", current.video_id || "—"],
      ["Cache key", current.cache_key || "—"]
    ].forEach(function (pair) { append(detailList, el("dt", "", pair[0]), el("dd", "", pair[1])); });
    append(runDetails, runSummary, detailList);
    detailBar.appendChild(runDetails);

    var analysisCommand = engine.command || engine.ffmpeg_command || (Array.isArray(engine.ffmpeg_args) ? engine.ffmpeg_args.join(" ") : "");
    if (analysisCommand) {
      var commandDetails = el("details", "monitor-command quality-command");
      commandDetails.open = qualityUiState.commandOpen;
      var commandSummary = el("summary", "", commandDetails.open ? "Hide analysis command" : "Show analysis command");
      var commandPre = el("pre");
      commandPre.appendChild(el("code", "", analysisCommand));
      commandPre.scrollTop = qualityUiState.commandScrollTop;
      commandPre.addEventListener("scroll", function () { qualityUiState.commandScrollTop = commandPre.scrollTop; }, { passive: true });
      commandDetails.addEventListener("toggle", function () {
        qualityUiState.commandOpen = commandDetails.open;
        commandSummary.textContent = commandDetails.open ? "Hide analysis command" : "Show analysis command";
      });
      append(commandDetails, commandSummary, commandPre);
      var copyButton = el("button", "copy-command", Date.now() < qualityUiState.copiedUntil ? "Copied ✓" : "Copy analysis command");
      copyButton.type = "button";
      copyButton.addEventListener("click", function () {
        copyButton.disabled = true;
        copyText(analysisCommand).then(function () {
          qualityUiState.copiedUntil = Date.now() + 2500;
          copyButton.textContent = "Copied ✓";
          window.setTimeout(function () {
            if (copyButton.isConnected) {
              copyButton.textContent = "Copy analysis command";
              copyButton.disabled = false;
            }
          }, 2500);
        }).catch(function () {
          qualityUiState.commandOpen = true;
          commandDetails.open = true;
          copyButton.textContent = "Select the command to copy";
          copyButton.disabled = false;
        });
      });
      append(detailBar, commandDetails, copyButton);
    }
    section.appendChild(detailBar);
    if (failed && progress.error) section.appendChild(el("p", "category-error", progress.error));
    section.appendChild(el("p", "monitor-note", "Quality reports compare the encoded rendition with its source. Completed reports remain cached until that source version changes."));
    return section;
  }

  function refreshQualityMonitor() {
    if (currentVideoId()) return;
    var existing = document.getElementById("quality-monitor");
    var replacement = buildQualityMonitor();
    if (!replacement) {
      if (existing) existing.remove();
      return;
    }
    if (existing) {
      var oldQueue = existing.querySelector(".queue-full-list");
      var oldCommand = existing.querySelector(".quality-command pre");
      if (oldQueue) qualityUiState.queueScrollTop = oldQueue.scrollTop;
      if (oldCommand) qualityUiState.commandScrollTop = oldCommand.scrollTop;
      existing.replaceWith(replacement);
    } else if (catalog) {
      var anchor = document.getElementById("category-monitor") || document.getElementById("encode-monitor");
      if (anchor) anchor.insertAdjacentElement("afterend", replacement);
      else app.appendChild(replacement);
    }
    var newQueue = replacement.querySelector(".queue-full-list");
    var newCommand = replacement.querySelector(".quality-command pre");
    if (newQueue) newQueue.scrollTop = qualityUiState.queueScrollTop;
    if (newCommand) newCommand.scrollTop = qualityUiState.commandScrollTop;
  }

  function loadQualityProgress() {
    if (currentVideoId()) return;
    fetch("data/quality-analysis-progress.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Quality telemetry request returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        qualityProgress = data;
        refreshQualityMonitor();
      })
      .catch(function () {
        qualityProgress = null;
        refreshQualityMonitor();
      });
  }

  function loadEncodingProgress() {
    if (currentVideoId()) return;
    fetch("data/encode-progress.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Telemetry request returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        encodeProgress = data;
        refreshEncodingMonitor();
      })
      .catch(function () {
        if (!encodeProgress) refreshEncodingMonitor();
      });
  }

  function buildCard(item, cardIndex) {
    var displayTitle = prettyVideoTitle(item.title || item.source_relative);
    var card = el("article", "video-card");
    var link = el("a");
    link.href = routeHref(item.id);
    link.setAttribute("aria-label", "Open " + displayTitle);
    link.addEventListener("click", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigate(item.id);
    });

    var poster = el("div", "card-poster");
    var image = el("img");
    image.className = "card-image";
    image.alt = "";
    image.loading = "eager";
    image.fetchPriority = cardIndex < 4 ? "high" : "auto";
    image.decoding = "async";
    function revealPoster() {
      image.classList.add("is-ready");
      poster.classList.add("is-ready");
    }
    image.addEventListener("load", revealPoster, { once: true });
    image.addEventListener("error", function () { poster.classList.add("is-ready"); }, { once: true });
    loadMediaAccess(item).then(function (access) {
      if (!image.isConnected) return;
      image.src = mediaAssetUrl(item, access, item.poster_url);
      if (image.complete) revealPoster();
    });
    append(poster,
      image,
      el("span", "card-stream", "PLAY · " + ((item.hls_variants[0] || {}).name || "HLS")),
      el("span", "card-duration", formatDuration(item.duration_seconds))
    );

    var body = el("div", "card-body");
    var title = el("h2", "card-title", displayTitle);
    title.title = displayTitle;
    var path = el("p", "card-path", item.source_relative);
    path.title = item.source_relative;
    var tagSources = el("div", "card-tag-sources");
    var analyzed = analyzedActivitiesFor(item);
    var record = visualRecordFor(item);
    var detectedRow = el("div", "card-tag-row is-analysis");
    detectedRow.appendChild(el("span", "card-tag-label", "✦ Video analysis"));
    var detectedTags = el("div", "card-tags");
    if (analyzed.length) {
      analyzed.slice(0, 3).forEach(function (activity) {
        var chip = el("span", "category-chip is-visual source-" + activity.source, activity.label + (confidenceText(activity) ? " · " + confidenceText(activity) : ""));
        chip.title = analysisSourceLabel(activity) + (confidenceText(activity) ? " with " + confidenceText(activity) + " model confidence" : "");
        detectedTags.appendChild(chip);
      });
    } else {
      detectedTags.appendChild(el("span", "tag-empty", record ? "No confident detections" : "Analysis pending"));
    }
    detectedRow.appendChild(detectedTags);
    tagSources.appendChild(detectedRow);

    var filenameTags = filenameActivitiesFor(item);
    var filenameRow = el("div", "card-tag-row is-filename");
    filenameRow.appendChild(el("span", "card-tag-label", "Aa Filename hints"));
    var filenameTagList = el("div", "card-tags");
    if (filenameTags.length) {
      filenameTags.slice(0, 3).forEach(function (activity) {
        var chip = el("span", "category-chip is-filename", activity.label);
        chip.title = "Unverified hint inferred only from the source filename";
        filenameTagList.appendChild(chip);
      });
    } else {
      filenameTagList.appendChild(el("span", "tag-empty", "No filename hints"));
    }
    filenameRow.appendChild(filenameTagList);
    tagSources.appendChild(filenameRow);
    var length = durationClass(item);
    var lengthTag = el("div", "card-length-row");
    lengthTag.appendChild(el("span", "length-chip length-" + length.key, length.label + " · " + length.description));
    var facts = el("div", "card-facts");
    var video = item.video_streams[0] || {};
    append(facts,
      el("span", "chip", resolution(item)),
      el("span", "chip", titleCaseCodec(video.codec_name)),
      el("span", "chip", formatBytes(item.size_bytes)),
      item.uploaded_at ? el("span", "chip", "Uploaded " + formatDate(item.uploaded_at, false)) : null
    );
    append(body, title, path, tagSources, lengthTag, facts);
    append(link, poster, body);
    card.appendChild(link);
    var shareButton = buildShareButton(item, "card-share-button", "Share ↗");
    if (shareButton) card.appendChild(shareButton);
    return card;
  }

  function filteredItems() {
    var query = libraryState.query.trim().toLocaleLowerCase();
    var items = catalog.items.filter(function (item) {
      var activities = activitiesFor(item);
      var filterActivities = activitiesForSource(item, libraryState.tagSource);
      var length = durationClass(item);
      var searchable = [item.title, item.source_relative, item.format_long_name || "", length.label, length.description]
        .concat(activities.map(function (activity) { return activity.label; }))
        .join(" ")
        .toLocaleLowerCase();
      var matchesQuery = !query || searchable.indexOf(query) !== -1;
      var itemTagKeys = filterActivities.map(function (activity) { return activity.key; });
      var matchesTags = !libraryState.tags.length || (libraryState.tagMode === "any"
        ? libraryState.tags.some(function (key) { return itemTagKeys.indexOf(key) !== -1; })
        : libraryState.tags.every(function (key) { return itemTagKeys.indexOf(key) !== -1; }));
      var matchesLength = libraryState.length === "all" || length.key === libraryState.length;
      return matchesQuery && matchesTags && matchesLength;
    });

    items.sort(function (a, b) {
      if (libraryState.sort === "uploaded-newest" || libraryState.sort === "uploaded-oldest") {
        var direction = libraryState.sort === "uploaded-oldest" ? 1 : -1;
        var aSequence = Number(a.upload_sequence) || 0;
        var bSequence = Number(b.upload_sequence) || 0;
        if (aSequence && bSequence && aSequence !== bSequence) return (aSequence - bSequence) * direction;
        var uploadOrder = String(a.uploaded_at || a.modified_at).localeCompare(String(b.uploaded_at || b.modified_at));
        if (uploadOrder) return uploadOrder * direction;
      }
      if (libraryState.sort === "oldest") return String(a.modified_at).localeCompare(String(b.modified_at));
      if (libraryState.sort === "name") return a.title.localeCompare(b.title, undefined, { numeric: true, sensitivity: "base" });
      if (libraryState.sort === "activity") {
        var activityOrder = activitiesFor(a)[0].label.localeCompare(activitiesFor(b)[0].label, undefined, { sensitivity: "base" });
        return activityOrder || a.title.localeCompare(b.title, undefined, { numeric: true, sensitivity: "base" });
      }
      if (libraryState.sort === "shortest") return Number(a.duration_seconds) - Number(b.duration_seconds);
      if (libraryState.sort === "longest") return Number(b.duration_seconds) - Number(a.duration_seconds);
      if (libraryState.sort === "size") return Number(b.size_bytes) - Number(a.size_bytes);
      return String(b.modified_at).localeCompare(String(a.modified_at));
    });
    return items;
  }

  function randomizeIds(items) {
    var ids = items.map(function (item) { return item.id; });
    for (var index = ids.length - 1; index > 0; index -= 1) {
      var swapIndex = Math.floor(Math.random() * (index + 1));
      var temporary = ids[index];
      ids[index] = ids[swapIndex];
      ids[swapIndex] = temporary;
    }
    return ids;
  }

  function matchingSignature(items) {
    return items.map(function (item) { return item.id; }).sort().join("|");
  }

  function ensureShuffleOrder(currentId) {
    var items = filteredItems();
    var signature = matchingSignature(items);
    var currentPosition = shuffleState.order.indexOf(currentId);
    if (!shuffleState.active || shuffleState.signature !== signature || currentPosition === -1) {
      var order = randomizeIds(items);
      var incomingPosition = order.indexOf(currentId);
      if (incomingPosition > 0) {
        order.splice(incomingPosition, 1);
        order.unshift(currentId);
      }
      shuffleState.active = true;
      shuffleState.order = order;
      shuffleState.position = Math.max(0, order.indexOf(currentId));
      shuffleState.signature = signature;
    } else {
      shuffleState.position = currentPosition;
    }
    return shuffleState.order;
  }

  function startShufflePlayback() {
    var items = filteredItems();
    if (!items.length) return;
    shuffleState.active = true;
    shuffleState.order = randomizeIds(items);
    shuffleState.position = 0;
    shuffleState.signature = matchingSignature(items);
    navigate(shuffleState.order[0], { shuffle: true });
  }

  function shuffleNeighbor(item, direction) {
    var order = ensureShuffleOrder(item.id);
    if (order.length < 2) return null;
    var position = order.indexOf(item.id);
    var nextPosition = (position + direction + order.length) % order.length;
    return catalog.items.find(function (entry) { return entry.id === order[nextPosition]; }) || null;
  }

  function goToShuffleNeighbor(item, direction) {
    var target = shuffleNeighbor(item, direction);
    if (!target) return false;
    shuffleState.position = shuffleState.order.indexOf(target.id);
    navigate(target.id, { shuffle: true });
    return true;
  }

  function detailSequence(item) {
    var items = filteredItems();
    if (items.some(function (entry) { return entry.id === item.id; })) return items;
    return catalog.items.slice().sort(function (a, b) {
      return String(b.modified_at).localeCompare(String(a.modified_at));
    });
  }

  function pageNumbers(current, total) {
    var values = [1, total, current - 2, current - 1, current, current + 1, current + 2]
      .filter(function (value) { return value >= 1 && value <= total; })
      .sort(function (a, b) { return a - b; });
    return values.filter(function (value, index) { return index === 0 || value !== values[index - 1]; });
  }

  function renderPagination(paginations, totalItems, pageCount, grid, note, shuffleButton) {
    paginations = Array.isArray(paginations) ? paginations : [paginations];
    paginations.forEach(function (pagination) {
      pagination.replaceChildren();
      pagination.hidden = pageCount <= 1;
    });
    if (pageCount <= 1) return;

    function changePage(page) {
      libraryState.page = Math.min(pageCount, Math.max(1, page));
      syncLibraryUrl();
      renderGrid(grid, note, paginations, shuffleButton);
      note.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    paginations.forEach(function (pagination, paginationIndex) {
      var previous = el("button", "pager-step", "← Previous");
      previous.type = "button";
      previous.disabled = libraryState.page === 1;
      previous.addEventListener("click", function () { changePage(libraryState.page - 1); });
      pagination.appendChild(previous);

      var numbers = el("div", "pager-numbers");
      var shown = pageNumbers(libraryState.page, pageCount);
      shown.forEach(function (page, index) {
        if (index && page - shown[index - 1] > 1) numbers.appendChild(el("span", "pager-gap", "…"));
        var button = el("button", "pager-page", page);
        button.type = "button";
        button.setAttribute("aria-label", "Go to page " + page);
        if (page === libraryState.page) {
          button.classList.add("is-current");
          button.setAttribute("aria-current", "page");
        }
        button.addEventListener("click", function () { changePage(page); });
        numbers.appendChild(button);
      });
      pagination.appendChild(numbers);

      var next = el("button", "pager-step", "Next →");
      next.type = "button";
      next.disabled = libraryState.page === pageCount;
      next.addEventListener("click", function () { changePage(libraryState.page + 1); });
      pagination.appendChild(next);
      pagination.setAttribute(
        "aria-label",
        (paginationIndex === 0 ? "Video pages above results" : "Video pages below results") +
          " · " + totalItems + " matching videos"
      );
    });
  }

  function renderGrid(grid, note, paginations, shuffleButton) {
    var items = filteredItems();
    var pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    libraryState.page = Math.min(pageCount, Math.max(1, libraryState.page));
    syncLibraryUrl();
    var start = (libraryState.page - 1) * PAGE_SIZE;
    var visibleItems = items.slice(start, start + PAGE_SIZE);
    grid.replaceChildren();
    note.textContent = items.length
      ? "Showing " + (start + 1) + "–" + (start + visibleItems.length) + " of " + items.length + " matching videos · Page " + libraryState.page + " of " + pageCount
      : "0 of " + catalog.items.length + " videos";
    if (shuffleButton) {
      shuffleButton.disabled = !items.length;
      shuffleButton.querySelector("strong").textContent = items.length
        ? "Shuffle " + items.length + (items.length === 1 ? " match" : " matches")
        : "Nothing to shuffle";
    }

    if (!items.length) {
      var empty = el("div", "empty-state");
      append(empty, el("h2", "", "No matching videos"), el("p", "", "Try another search, tag combination, or duration."));
      grid.appendChild(empty);
      paginations.forEach(function (pagination) {
        pagination.replaceChildren();
        pagination.hidden = true;
      });
      return;
    }
    visibleItems.forEach(function (item, cardIndex) { grid.appendChild(buildCard(item, cardIndex)); });
    renderPagination(paginations, items.length, pageCount, grid, note, shuffleButton);
  }

  function renderLibrary() {
    destroyPlayer();
    document.title = brandText("gallery_name", "Video Gallery");
    app.replaceChildren();
    app.appendChild(buildHero(catalog.items));

    if (!catalog.items.length) {
      var empty = el("section", "empty-state");
      append(empty,
        el("h2", "", brandText("empty_title", "The library is ready")),
        el("p", "", brandText("empty_body", "New videos are being prepared. Check back soon."))
      );
      app.appendChild(empty);
      if (FEATURES.encoder_status) app.appendChild(buildEncodingMonitor());
      if (FEATURES.content_analysis) app.appendChild(buildCategoryMonitor());
      if (qualityMonitorVisible()) app.appendChild(buildQualityMonitor());
      return;
    }

    var toolbar = el("section", "toolbar");
    toolbar.setAttribute("aria-label", "Library controls");
    var searchWrap = el("label", "search-wrap");
    var search = el("input");
    search.type = "search";
    search.placeholder = "Search " + brandText("video_plural", "videos");
    search.setAttribute("aria-label", "Search videos");
    search.value = libraryState.query;
    searchWrap.appendChild(search);

    var tagCounts = { all: {}, visual: {}, filename: {} };
    function countTags(bucket, tags) {
      var seen = {};
      tags.forEach(function (tag) {
        if (!tag || !tag.key || seen[tag.key]) return;
        seen[tag.key] = true;
        bucket[tag.key] = (bucket[tag.key] || 0) + 1;
      });
    }
    catalog.items.forEach(function (item) {
      countTags(tagCounts.all, activitiesFor(item));
      countTags(tagCounts.visual, analyzedActivitiesFor(item));
      countTags(tagCounts.filename, filenameActivitiesFor(item));
    });
    var activeTagCounts = tagCounts[libraryState.tagSource];

    var tagFilter = el("details", "tag-filter");
    tagFilter.open = tagDrawerOpen;
    var tagSummary = el("summary");
    function updateTagSummary() {
      var sourceLabel = libraryState.tagSource === "visual" ? "visual detections" : (libraryState.tagSource === "filename" ? "filename hints" : "content tags");
      if (!libraryState.tags.length) tagSummary.textContent = "All " + sourceLabel;
      else if (libraryState.tags.length === 1) {
        var single = CONTENT_TAGS.find(function (tag) { return tag.key === libraryState.tags[0]; });
        tagSummary.textContent = (single ? single.label : "1 tag") + " · " + sourceLabel;
      } else {
        tagSummary.textContent = libraryState.tags.length + " " + sourceLabel + " · match " + libraryState.tagMode;
      }
    }
    updateTagSummary();
    tagFilter.appendChild(tagSummary);
    var tagPanel = el("div", "tag-panel");
    var tagPanelTop = el("div", "tag-panel-top");
    var sourceLabel = el("label", "tag-mode tag-source-select");
    sourceLabel.appendChild(el("span", "", "Trust source"));
    var tagSource = el("select");
    [["all", "Any source"], ["visual", "Visual analysis only"], ["filename", "Filename hints only"]].forEach(function (choice) {
      var option = el("option", "", choice[1]);
      option.value = choice[0];
      option.selected = choice[0] === libraryState.tagSource;
      tagSource.appendChild(option);
    });
    sourceLabel.appendChild(tagSource);
    var modeLabel = el("label", "tag-mode");
    modeLabel.appendChild(el("span", "", "Selected tags"));
    var tagMode = el("select");
    [["all", "Match all"], ["any", "Match any"]].forEach(function (choice) {
      var option = el("option", "", choice[1]);
      option.value = choice[0];
      option.selected = choice[0] === libraryState.tagMode;
      tagMode.appendChild(option);
    });
    modeLabel.appendChild(tagMode);
    var clearTags = el("button", "clear-tags", "Clear");
    clearTags.type = "button";
    clearTags.disabled = !libraryState.tags.length;
    append(tagPanelTop, sourceLabel, modeLabel, clearTags);
    tagPanel.appendChild(tagPanelTop);
    var sourceNote = libraryState.tagSource === "visual"
      ? "✦ Visual analysis reads cached frames. It is the stronger automated signal, but model detections can still be wrong."
      : (libraryState.tagSource === "filename"
        ? "Aa Filename hints are text matches only. They have not been verified against the video."
        : "Choose Visual analysis only when you want the more trustworthy automated signal; filename hints remain clearly separate.");
    tagPanel.appendChild(el("p", "tag-source-note source-" + libraryState.tagSource, sourceNote));

    var tagGroups = CONTENT_TAGS.map(function (tag) { return tag.group; }).filter(function (group, index, groups) {
      return group && groups.indexOf(group) === index;
    });
    tagGroups.forEach(function (groupName) {
      var available = CONTENT_TAGS.filter(function (tag) {
        return tag.group === groupName && ((activeTagCounts[tag.key] || 0) > 0 || libraryState.tags.indexOf(tag.key) !== -1);
      });
      if (!available.length) return;
      var group = el("fieldset", "tag-group");
      group.appendChild(el("legend", "", groupName));
      available.forEach(function (tag) {
        var label = el("label", "tag-choice");
        var checkbox = el("input");
        checkbox.type = "checkbox";
        checkbox.value = tag.key;
        checkbox.checked = libraryState.tags.indexOf(tag.key) !== -1;
        append(label, checkbox, el("span", "", tag.label), el("small", "", activeTagCounts[tag.key] || 0));
        checkbox.addEventListener("change", function () {
          if (checkbox.checked && libraryState.tags.indexOf(tag.key) === -1) libraryState.tags.push(tag.key);
          if (!checkbox.checked) libraryState.tags = libraryState.tags.filter(function (key) { return key !== tag.key; });
          libraryState.page = 1;
          clearTags.disabled = !libraryState.tags.length;
          updateTagSummary();
          renderGrid(grid, note, paginations, shuffleButton);
        });
        group.appendChild(label);
      });
      tagPanel.appendChild(group);
    });
    tagMode.addEventListener("change", function () {
      libraryState.tagMode = tagMode.value;
      libraryState.page = 1;
      updateTagSummary();
      renderGrid(grid, note, paginations, shuffleButton);
    });
    tagSource.addEventListener("change", function () {
      libraryState.tagSource = tagSource.value;
      libraryState.page = 1;
      renderLibrary();
    });
    clearTags.addEventListener("click", function () {
      libraryState.tags = [];
      libraryState.page = 1;
      tagPanel.querySelectorAll('input[type="checkbox"]').forEach(function (checkbox) { checkbox.checked = false; });
      clearTags.disabled = true;
      updateTagSummary();
      renderGrid(grid, note, paginations, shuffleButton);
    });
    tagFilter.addEventListener("toggle", function () { tagDrawerOpen = tagFilter.open; });
    tagFilter.appendChild(tagPanel);

    var length = el("select");
    length.setAttribute("aria-label", "Filter by video duration");
    [
      ["all", "Any duration"],
      ["short", "Short · under 5 min"],
      ["medium", "Medium · 5–20 min"],
      ["long", "Long · 20+ min"]
    ].forEach(function (choice) {
      var option = el("option", "", choice[1]);
      option.value = choice[0];
      if (choice[0] === libraryState.length) option.selected = true;
      length.appendChild(option);
    });

    var sort = el("select");
    sort.setAttribute("aria-label", "Sort videos");
    [
      ["newest", "Recently modified"],
      ["oldest", "Oldest modified"],
      ["uploaded-newest", "Recently uploaded"],
      ["uploaded-oldest", "First uploaded"],
      ["name", "Title A–Z"],
      ["activity", "Activity A–Z"],
      ["shortest", "Shortest first"],
      ["longest", "Longest first"],
      ["size", "Largest first"]
    ].forEach(function (choice) {
      var option = el("option", "", choice[1]);
      option.value = choice[0];
      if (choice[0] === libraryState.sort) option.selected = true;
      sort.appendChild(option);
    });
    var shuffleButton = el("button", "shuffle-button");
    shuffleButton.type = "button";
    shuffleButton.title = "Play every matching video once in a random order";
    shuffleButton.setAttribute("aria-label", "Shuffle play the matching videos");
    append(shuffleButton, el("span", "shuffle-icon", "⤨"), el("strong", "", "Shuffle matches"));
    shuffleButton.addEventListener("click", startShufflePlayback);
    append(toolbar, searchWrap, tagFilter, length, sort, shuffleButton);
    app.appendChild(toolbar);

    var analyzerSummary = Number(contentIndex.analyzed_count || 0)
      ? contentIndex.analyzed_count + " visually analyzed · " + Number(contentIndex.pending_count || 0) + " waiting"
      : "Visual tags begin after encoding is idle";
    var sourceGuidance = libraryState.tagSource === "visual" ? "Filtering visual detections only" : (libraryState.tagSource === "filename" ? "Filtering unverified filename hints only" : "Filtering both sources");
    var guidance = el("p", "filter-guidance", analyzerSummary + " · " + sourceGuidance + " · Shuffle uses the active filters · Short under 5 min · Medium 5–20 min · Long 20+ min");
    app.appendChild(guidance);
    var note = el("p", "results-note");
    var grid = el("section", "video-grid");
    grid.setAttribute("aria-label", "Videos");
    var paginationTop = el("nav", "pagination pagination-top");
    var paginationBottom = el("nav", "pagination pagination-bottom");
    var paginations = [paginationTop, paginationBottom];
    append(app, note, paginationTop, grid, paginationBottom);

    search.addEventListener("input", function () {
      libraryState.query = search.value;
      libraryState.page = 1;
      renderGrid(grid, note, paginations, shuffleButton);
    });
    length.addEventListener("change", function () {
      libraryState.length = length.value;
      libraryState.page = 1;
      renderGrid(grid, note, paginations, shuffleButton);
    });
    sort.addEventListener("change", function () {
      libraryState.sort = sort.value;
      libraryState.page = 1;
      renderGrid(grid, note, paginations, shuffleButton);
    });
    renderGrid(grid, note, paginations, shuffleButton);
    if (FEATURES.encoder_status) app.appendChild(buildEncodingMonitor());
    if (FEATURES.content_analysis) app.appendChild(buildCategoryMonitor());
    if (qualityMonitorVisible()) app.appendChild(buildQualityMonitor());
  }

  function addStat(list, label, value) {
    var wrap = el("div", "stat");
    append(wrap, el("dt", "", label), el("dd", "", value || "Unknown"));
    list.appendChild(wrap);
  }

  function buildContentSignals(item) {
    var section = el("section", "section content-signals");
    var head = el("div", "section-head");
    append(head,
      el("h2", "", "Content signals"),
      el("p", "", "What the video analysis saw versus what the filename merely suggests")
    );
    section.appendChild(head);

    var grid = el("div", "signal-grid");
    var analyzed = analyzedActivitiesFor(item);
    var record = visualRecordFor(item);
    var analysisCard = el("article", "signal-card signal-analysis");
    var analysisHead = el("div", "signal-card-head");
    append(analysisHead,
      el("div", "", "✦ Visual category analysis"),
      el("span", "signal-trust is-stronger", record ? "Stronger automated signal" : "Pending")
    );
    analysisCard.appendChild(analysisHead);
    analysisCard.appendChild(el("p", "signal-explainer", record
      ? "Detected from cached frames sampled across this video. Confidence is shown for each model result; automated detections are useful, not guaranteed."
      : "This video is still waiting for frame analysis. Filename hints below are available in the meantime."));
    var analysisTags = el("div", "signal-tags");
    if (analyzed.length) {
      analyzed.forEach(function (tag) {
        var chip = el("span", "signal-chip source-" + tag.source);
        append(chip,
          el("strong", "", tag.label),
          el("small", "", analysisSourceLabel(tag) + (confidenceText(tag) ? " · " + confidenceText(tag) + " confidence" : ""))
        );
        var coverage = Number(tag.coverage);
        chip.title = Number.isFinite(coverage) && coverage > 0
          ? "Evidence appeared in " + Math.round(coverage * 100) + "% of sampled frames"
          : analysisSourceLabel(tag);
        analysisTags.appendChild(chip);
      });
    } else {
      analysisTags.appendChild(el("p", "signal-empty", record ? "No category passed the confidence threshold." : "Visual analysis pending."));
    }
    analysisCard.appendChild(analysisTags);

    var filenameCard = el("article", "signal-card signal-filename");
    var filenameHead = el("div", "signal-card-head");
    append(filenameHead,
      el("div", "", "Aa Filename hints"),
      el("span", "signal-trust is-unverified", "Unverified text only")
    );
    filenameCard.appendChild(filenameHead);
    filenameCard.appendChild(el("p", "signal-explainer", "Matched from words in the original filename. These hints are useful for finding clips, but they do not prove the activity appears in the video."));
    var filenameTags = el("div", "signal-tags");
    var hints = filenameActivitiesFor(item);
    if (hints.length) {
      hints.forEach(function (tag) {
        var chip = el("span", "signal-chip is-filename");
        append(chip, el("strong", "", tag.label), el("small", "", "Filename match · not verified"));
        filenameTags.appendChild(chip);
      });
    } else {
      filenameTags.appendChild(el("p", "signal-empty", "No category words were recognized in the filename."));
    }
    filenameCard.appendChild(filenameTags);
    append(grid, analysisCard, filenameCard);
    section.appendChild(grid);
    return section;
  }

  function qualityMetricValue(value) {
    if (value === null || value === undefined || value === "") return null;
    if (value && typeof value === "object") {
      var keys = ["mean", "average", "score", "weighted_mean", "value", "p50", "median"];
      for (var index = 0; index < keys.length; index += 1) {
        var raw = value[keys[index]];
        if (raw === null || raw === undefined || raw === "") continue;
        var nested = Number(raw);
        if (Number.isFinite(nested)) return nested;
      }
      return null;
    }
    var numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  function qualityMetricObject(report, aliases) {
    var sources = [
      report && report.metrics,
      report && report.summary && report.summary.metrics,
      report && report.overall && report.overall.metrics,
      report
    ];
    for (var sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
      var source = sources[sourceIndex];
      if (!source || typeof source !== "object") continue;
      for (var aliasIndex = 0; aliasIndex < aliases.length; aliasIndex += 1) {
        if (Object.prototype.hasOwnProperty.call(source, aliases[aliasIndex])) return source[aliases[aliasIndex]];
      }
    }
    return null;
  }

  function qualityPointMetric(point, aliases) {
    if (!point || typeof point !== "object") return null;
    var sources = [point.metrics, point];
    for (var sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
      var source = sources[sourceIndex];
      if (!source || typeof source !== "object") continue;
      for (var aliasIndex = 0; aliasIndex < aliases.length; aliasIndex += 1) {
        if (Object.prototype.hasOwnProperty.call(source, aliases[aliasIndex])) {
          return qualityMetricValue(source[aliases[aliasIndex]]);
        }
      }
    }
    return null;
  }

  function qualityPointTime(point, fallback) {
    var keys = ["time_seconds", "timestamp_seconds", "timestamp", "time", "pts_time", "start_seconds", "start"];
    for (var index = 0; index < keys.length; index += 1) {
      var value = Number(point && point[keys[index]]);
      if (Number.isFinite(value)) return Math.max(0, value);
    }
    return Math.max(0, Number(fallback) || 0);
  }

  function metricDisplay(value, decimals, suffix) {
    var numeric = qualityMetricValue(value);
    return numeric === null ? "—" : numeric.toFixed(decimals) + (suffix || "");
  }

  function metricDetail(value, decimals, suffix) {
    if (!value || typeof value !== "object") return "";
    var bits = [];
    [["p5", "P5"], ["p10", "P10"], ["worst_decile", "Worst decile"], ["min", "Minimum"], ["minimum", "Minimum"]].forEach(function (pair) {
      var raw = value[pair[0]];
      if (raw === null || raw === undefined || raw === "") return;
      var number = Number(raw);
      if (Number.isFinite(number)) {
        if (bits.length < 2) bits.push(pair[1] + " " + number.toFixed(decimals) + (suffix || ""));
      }
    });
    return bits.join(" · ");
  }

  function reportTimeline(report) {
    var values = report.timeline || report.samples || report.frames || report.quality_over_time || [];
    if (values && !Array.isArray(values)) values = values.samples || values.frames || values.points || [];
    if (!Array.isArray(values)) return [];
    return values.map(function (point, index) {
      return {
        source: point,
        time: qualityPointTime(point, index),
        composite: qualityPointMetric(point, ["composite", "score", "overall_score"]),
        vmaf: qualityPointMetric(point, ["vmaf", "vmaf_standard", "standard_vmaf", "libvmaf"]),
        phone: qualityPointMetric(point, ["vmaf_phone", "phone_vmaf", "vmaf_mobile"]),
        ssim: qualityPointMetric(point, ["ssim", "ssim_y"]),
        psnr: qualityPointMetric(point, ["psnr", "psnr_y"]),
        phash: qualityPointMetric(point, ["phash", "phash_similarity"]),
        temporalPhash: qualityPointMetric(point, ["temporal_phash", "temporal_consistency"])
      };
    }).filter(function (point) {
      return point.composite !== null || point.vmaf !== null;
    }).map(function (point) {
      if (point.composite === null) point.composite = point.vmaf;
      return point;
    }).sort(function (a, b) { return a.time - b.time; });
  }

  function downsampleQualityPoints(points, limit) {
    if (points.length <= limit) return points;
    var result = [points[0]];
    var bucketCount = limit - 2;
    for (var bucket = 0; bucket < bucketCount; bucket += 1) {
      var start = 1 + Math.floor(bucket * (points.length - 2) / bucketCount);
      var end = 1 + Math.floor((bucket + 1) * (points.length - 2) / bucketCount);
      var selected = points[start];
      for (var index = start + 1; index < Math.max(start + 1, end); index += 1) {
        if (points[index] && points[index].composite < selected.composite) selected = points[index];
      }
      if (selected) result.push(selected);
    }
    result.push(points[points.length - 1]);
    return result;
  }

  function seekVideo(video, seconds) {
    var seek = function () {
      video.currentTime = Math.min(Math.max(0, Number(seconds) || 0), Math.max(0, (video.duration || Number(seconds) + 1) - .1));
      video.focus({ preventScroll: true });
      var playback = video.play();
      if (playback && typeof playback.catch === "function") playback.catch(function () {});
    };
    if (video.readyState >= 1) seek();
    else video.addEventListener("loadedmetadata", seek, { once: true });
    video.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function svgNode(tag, attributes) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attributes || {}).forEach(function (name) { node.setAttribute(name, String(attributes[name])); });
    return node;
  }

  function buildQualityTimeline(report, video) {
    var allPoints = reportTimeline(report);
    if (allPoints.length < 2) return null;
    var points = downsampleQualityPoints(allPoints, 500);
    var section = el("div", "quality-timeline-wrap");
    var legend = el("div", "quality-chart-legend");
    append(
      legend,
      el("span", "quality-legend-composite", "Overall score"),
      el("span", "quality-legend-standard", "Standard VMAF"),
      points.some(function (point) { return point.phone !== null; }) ? el("span", "quality-legend-phone", "Phone VMAF · informational") : null
    );
    section.appendChild(legend);

    var chart = el("div", "quality-chart");
    chart.tabIndex = 0;
    chart.setAttribute("role", "group");
    chart.setAttribute("aria-label", "Quality over time. Use left and right arrows to inspect points and Enter to play from the selected time.");
    var svg = svgNode("svg", { viewBox: "0 0 1000 260", preserveAspectRatio: "none", "aria-hidden": "true" });
    var plotTop = 18;
    var plotBottom = 224;
    var plotLeft = 12;
    var plotRight = 988;
    [0, 25, 50, 75, 100].forEach(function (score) {
      var y = plotBottom - (score / 100) * (plotBottom - plotTop);
      svg.appendChild(svgNode("line", { x1: plotLeft, y1: y, x2: plotRight, y2: y, class: "quality-grid-line" }));
    });
    var maximumTime = Math.max(1, points[points.length - 1].time);
    function xFor(point) { return plotLeft + point.time / maximumTime * (plotRight - plotLeft); }
    function yFor(value) { return plotBottom - Math.max(0, Math.min(100, Number(value) || 0)) / 100 * (plotBottom - plotTop); }
    var compositeLine = svgNode("polyline", {
      points: points.map(function (point) { return xFor(point).toFixed(2) + "," + yFor(point.composite).toFixed(2); }).join(" "),
      class: "quality-line quality-line-composite"
    });
    svg.appendChild(compositeLine);
    var standardPoints = points.filter(function (point) { return point.vmaf !== null; });
    if (standardPoints.length > 1) {
      svg.appendChild(svgNode("polyline", {
        points: standardPoints.map(function (point) { return xFor(point).toFixed(2) + "," + yFor(point.vmaf).toFixed(2); }).join(" "),
        class: "quality-line quality-line-standard"
      }));
    }
    var phonePoints = points.filter(function (point) { return point.phone !== null; });
    if (phonePoints.length > 1) {
      svg.appendChild(svgNode("polyline", {
        points: phonePoints.map(function (point) { return xFor(point).toFixed(2) + "," + yFor(point.phone).toFixed(2); }).join(" "),
        class: "quality-line quality-line-phone"
      }));
    }
    var cursor = svgNode("line", { x1: plotLeft, y1: plotTop, x2: plotLeft, y2: plotBottom, class: "quality-chart-cursor" });
    var marker = svgNode("circle", { cx: plotLeft, cy: yFor(points[0].composite), r: 6, class: "quality-chart-marker" });
    append(svg, cursor, marker);
    chart.appendChild(svg);
    var readout = el("div", "quality-chart-readout");
    chart.appendChild(readout);
    section.appendChild(chart);
    var axis = el("div", "quality-chart-axis");
    append(axis, el("span", "", "0:00"), el("span", "", formatDuration(maximumTime)));
    section.appendChild(axis);

    var selectedIndex = 0;
    function updateSelection(index) {
      selectedIndex = Math.max(0, Math.min(points.length - 1, index));
      var point = points[selectedIndex];
      var x = xFor(point);
      cursor.setAttribute("x1", x);
      cursor.setAttribute("x2", x);
      marker.setAttribute("cx", x);
      marker.setAttribute("cy", yFor(point.composite));
      var values = [
        formatDuration(point.time),
        "Overall " + point.composite.toFixed(1)
      ];
      if (point.vmaf !== null) values.push("VMAF " + point.vmaf.toFixed(1));
      if (point.phone !== null) values.push("Phone " + point.phone.toFixed(1));
      if (point.ssim !== null) values.push("SSIM " + point.ssim.toFixed(4));
      if (point.psnr !== null) values.push("PSNR " + point.psnr.toFixed(2) + " dB");
      if (point.phash !== null) values.push("pHash " + point.phash.toFixed(1));
      if (point.temporalPhash !== null) values.push("Temporal pHash " + point.temporalPhash.toFixed(1));
      readout.textContent = values.join(" · ") + " · click to play";
      chart.setAttribute("aria-label", values.join(", ") + ". Press Enter to play from this point.");
    }
    function nearestIndex(clientX) {
      var bounds = chart.getBoundingClientRect();
      var ratio = Math.max(0, Math.min(1, (clientX - bounds.left) / Math.max(1, bounds.width)));
      var target = ratio * maximumTime;
      var bestIndex = 0;
      var bestDistance = Infinity;
      points.forEach(function (point, index) {
        var distance = Math.abs(point.time - target);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestIndex = index;
        }
      });
      return bestIndex;
    }
    chart.addEventListener("pointermove", function (event) { updateSelection(nearestIndex(event.clientX)); });
    chart.addEventListener("click", function (event) {
      updateSelection(nearestIndex(event.clientX));
      seekVideo(video, points[selectedIndex].time);
    });
    chart.addEventListener("keydown", function (event) {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight" || event.key === "Home" || event.key === "End") {
        event.preventDefault();
        if (event.key === "Home") updateSelection(0);
        else if (event.key === "End") updateSelection(points.length - 1);
        else updateSelection(selectedIndex + (event.key === "ArrowRight" ? 1 : -1));
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        seekVideo(video, points[selectedIndex].time);
      }
    });
    updateSelection(0);
    return section;
  }

  function sceneMetric(scene, aliases) {
    return qualityPointMetric(scene, aliases);
  }

  function buildWorstSceneTable(report, video) {
    var scenes = report.worst_scenes || report.scenes || (report.scene_analysis && report.scene_analysis.scenes) || [];
    if (!Array.isArray(scenes) || !scenes.length) return null;
    var zeroBasedSceneIndexes = scenes.some(function (scene) { return Number(scene && scene.index) === 0; });
    scenes = scenes.slice().sort(function (left, right) {
      var leftScore = qualityMetricValue(left.score);
      var rightScore = qualityMetricValue(right.score);
      if (leftScore === null) leftScore = sceneMetric(left, ["composite", "vmaf", "vmaf_standard", "standard_vmaf"]);
      if (rightScore === null) rightScore = sceneMetric(right, ["composite", "vmaf", "vmaf_standard", "standard_vmaf"]);
      return (leftScore === null ? Infinity : leftScore) - (rightScore === null ? Infinity : rightScore);
    }).slice(0, 12);

    var wrap = el("div", "quality-scene-table-wrap");
    var table = el("table", "stream-table quality-scene-table");
    var thead = el("thead");
    var headRow = el("tr");
    ["Worst scene", "Time", "Score", "Band", "Standard VMAF", "SSIM", "PSNR", "pHash", "Phone VMAF", "Temporal pHash", ""].forEach(function (label) {
      headRow.appendChild(el("th", "", label));
    });
    thead.appendChild(headRow);
    var tbody = el("tbody");
    scenes.forEach(function (scene, index) {
      var start = qualityPointTime(scene, 0);
      var end = Number(scene.end_seconds != null ? scene.end_seconds : scene.end);
      var timeText = formatDuration(start) + (Number.isFinite(end) ? "–" + formatDuration(end) : "");
      var row = el("tr");
      var sceneScore = qualityMetricValue(scene.score);
      if (sceneScore === null) sceneScore = sceneMetric(scene, ["composite"]);
      var playCell = el("td");
      var play = el("button", "quality-scene-play", "Play");
      play.type = "button";
      play.addEventListener("click", function () { seekVideo(video, start); });
      playCell.appendChild(play);
      append(
        row,
        el("td", "", scene.label || scene.name || "Scene " + (
          scene.index != null
            ? Number(scene.index) + (zeroBasedSceneIndexes ? 1 : 0)
            : index + 1
        )),
        el("td", "", timeText),
        el("td", "", metricDisplay(sceneScore, 1)),
        el("td", "", scene.band || scene.quality_band || "—"),
        el("td", "", metricDisplay(sceneMetric(scene, ["vmaf", "vmaf_standard", "standard_vmaf"]), 1)),
        el("td", "", metricDisplay(sceneMetric(scene, ["ssim", "ssim_y"]), 4)),
        el("td", "", metricDisplay(sceneMetric(scene, ["psnr", "psnr_y"]), 2, " dB")),
        el("td", "", metricDisplay(sceneMetric(scene, ["phash", "phash_similarity"]), 1)),
        el("td", "", metricDisplay(sceneMetric(scene, ["vmaf_phone", "phone_vmaf", "vmaf_mobile"]), 1)),
        el("td", "", metricDisplay(sceneMetric(scene, ["temporal_phash", "temporal_consistency"]), 1)),
        playCell
      );
      tbody.appendChild(row);
    });
    append(table, thead, tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function qualityArtifactLink(basePath, filename, label, download) {
    if (!/^[A-Za-z0-9._-]+$/.test(filename)) return null;
    var link = el("a", "quality-artifact", label);
    link.href = basePath + filename;
    if (download) link.setAttribute("download", "");
    else {
      link.target = "_blank";
      link.rel = "noopener";
    }
    return link;
  }

  function reportIsHdrNormalized(report) {
    if (report.hdr_normalized === true || report.hdr_normalization === true) return true;
    var normalization = report.normalization || (report.settings && report.settings.normalization) || {};
    if (typeof normalization === "string") return /hdr|tone/i.test(normalization);
    return normalization.hdr_normalized === true || normalization.applied === true && /hdr|tone/i.test(String(normalization.method || normalization.label || ""));
  }

  function buildQualityReport(item, report, reportBase, video) {
    var section = el("section", "section quality-report");
    section.id = "quality-report";
    var standard = qualityMetricObject(report, ["vmaf", "vmaf_standard", "standard_vmaf", "libvmaf"]);
    var phone = qualityMetricObject(report, ["vmaf_phone", "phone_vmaf", "vmaf_mobile"]);
    var ssim = qualityMetricObject(report, ["ssim", "ssim_y"]);
    var psnr = qualityMetricObject(report, ["psnr", "psnr_y"]);
    var phash = qualityMetricObject(report, ["phash", "phash_similarity"]);
    var composite = qualityMetricObject(report, ["composite"]);
    var overall = report.overall || report.summary || {};
    var overallScore = qualityMetricValue(overall.score != null ? overall.score : report.overall_score);
    if (overallScore === null) overallScore = qualityMetricValue(composite);
    if (overallScore === null) overallScore = qualityMetricValue(standard);
    var band = overall.band || overall.quality_band || report.band || report.quality_band || "Unrated";

    var head = el("div", "section-head quality-report-head");
    var headCopy = el("div");
    append(headCopy, el("h2", "", "Encoded quality report"), el("p", "", "Objective comparison of this HLS rendition with its source"));
    var badges = el("div", "quality-report-badges");
    badges.appendChild(el("span", "quality-band", band));
    if (reportIsHdrNormalized(report)) {
      var hdr = el("span", "quality-hdr-badge", "HDR normalized");
      hdr.title = "The comparison was normalized to a common display-referred range before scoring.";
      badges.appendChild(hdr);
    }
    append(head, headCopy, badges);
    section.appendChild(head);

    var summary = el("div", "quality-summary");
    var scoreCard = el("article", "quality-score-card");
    append(
      scoreCard,
      el("span", "", "Overall quality"),
      el("strong", "", overallScore === null ? "—" : overallScore.toFixed(1)),
      el("small", "", band + " · standard VMAF is the primary perceptual metric")
    );
    summary.appendChild(scoreCard);
    var metricGrid = el("div", "quality-metric-grid");
    [
      ["Standard VMAF", standard, 1, "", "Primary perceptual score", "is-primary"],
      ["SSIM", ssim, 4, "", "Structural similarity", ""],
      ["PSNR", psnr, 2, " dB", "Signal fidelity", ""],
      ["pHash", phash, 1, "", "Cross-stream visual similarity", ""],
      ["Phone VMAF", phone, 1, "", "Informational phone-viewing model", "is-informational"]
    ].forEach(function (metric) {
      var card = el("article", "quality-metric-card " + metric[5]);
      var label = el("div", "quality-metric-label");
      label.appendChild(el("span", "", metric[0]));
      if (metric[5] === "is-primary") label.appendChild(el("small", "", "Primary"));
      if (metric[5] === "is-informational") label.appendChild(el("small", "", "Informational"));
      append(
        card,
        label,
        el("strong", "", metricDisplay(metric[1], metric[2], metric[3])),
        el("p", "", metricDetail(metric[1], metric[2], metric[3]) || metric[4])
      );
      metricGrid.appendChild(card);
    });
    summary.appendChild(metricGrid);
    section.appendChild(summary);

    if (reportIsHdrNormalized(report)) {
      section.appendChild(el("p", "quality-normalization-note", "HDR-normalized comparison · source and encode were converted to the same analysis range so the scores remain comparable."));
    }

    var timeline = buildQualityTimeline(report, video);
    if (timeline) {
      var timelineSection = el("div", "quality-report-block");
      var timelineHead = el("div", "quality-subhead");
      append(timelineHead, el("h3", "", "Quality over time"), el("p", "", "Point at the graph to inspect · click to play that moment"));
      append(timelineSection, timelineHead, timeline);
      section.appendChild(timelineSection);
    }

    var scenes = buildWorstSceneTable(report, video);
    if (scenes) {
      var sceneSection = el("div", "quality-report-block");
      var sceneHead = el("div", "quality-subhead");
      append(sceneHead, el("h3", "", "Worst scenes"), el("p", "", "Lowest overall score first · up to 12 scenes"));
      append(sceneSection, sceneHead, scenes);
      section.appendChild(sceneSection);
    }

    var footer = el("div", "quality-report-footer");
    var generated = report.generated_at || report.completed_at || report.analyzed_at;
    footer.appendChild(el("p", "", generated ? "Report generated " + formatDate(generated, true) : "Cached report for this exact source version"));
    var artifacts = el("div", "quality-artifacts");
    append(
      artifacts,
      qualityArtifactLink(reportBase, "report.json", "Download JSON", true),
      qualityArtifactLink(reportBase, "frames.csv", "Download frame CSV", true),
      qualityArtifactLink(reportBase, "report.html", "Open standalone report", false)
    );
    append(footer, artifacts);
    section.appendChild(footer);
    return section;
  }

  function loadQualityReport(item) {
    if (FEATURES.quality_analysis === false) return Promise.resolve(null);
    var safeKey = String(item.cache_key || "");
    if (!/^[0-9a-f]{18}--[0-9a-f]{14}$/.test(safeKey)) return Promise.resolve(null);
    var reportBase = "data/quality/" + encodeURIComponent(safeKey) + "/";
    return fetch(reportBase + "report.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error("Quality report request returned " + response.status);
        return response.json();
      })
      .then(function (report) {
        if (!report || report.enabled === false || String(report.state || "").toLowerCase() === "disabled") return null;
        if (report.cache_key && report.cache_key !== item.cache_key) return null;
        if (report.video_id && report.video_id !== item.id) return null;
        if (report.gallery && report.gallery.cache_key && report.gallery.cache_key !== item.cache_key) return null;
        if (report.gallery && report.gallery.video_id && report.gallery.video_id !== item.id) return null;
        return { report: report, base: reportBase };
      })
      .catch(function () { return null; });
  }

  function buildTrackCard(stream, kind, index) {
    var card = el("article", "track-card");
    var title = kind + " track " + (index + 1);
    if (stream.language && stream.language !== "und") title += " · " + stream.language.toUpperCase();
    card.appendChild(el("h3", "", title));
    var list = el("dl", "track-list");
    function pair(label, value) { append(list, el("dt", "", label), el("dd", "", value || "Unknown")); }
    pair("Codec", titleCaseCodec(stream.codec_name));
    if (stream.profile) pair("Profile", stream.profile);
    if (kind === "Video") {
      pair("Dimensions", stream.width && stream.height ? stream.width + "×" + stream.height : "Unknown");
      pair("Frame rate", stream.frame_rate ? Number(stream.frame_rate).toFixed(3).replace(/\.0+$/, "") + " fps" : "Unknown");
      pair("Pixel format", stream.pixel_format);
      pair("Bit rate", formatBitrate(stream.bit_rate));
      if (stream.color_space) pair("Color", [stream.color_space, stream.color_primaries, stream.color_transfer].filter(Boolean).join(" / "));
    } else if (kind === "Audio") {
      pair("Channels", stream.channel_layout || (stream.channels ? stream.channels + " channels" : "Unknown"));
      pair("Sample rate", stream.sample_rate ? Number(stream.sample_rate).toLocaleString() + " Hz" : "Unknown");
      pair("Bit rate", formatBitrate(stream.bit_rate));
      if (stream.sample_format) pair("Sample format", stream.sample_format);
    } else {
      if (stream.title) pair("Title", stream.title);
    }
    card.appendChild(list);
    return card;
  }

  function setPlayerMessage(node, message) {
    node.textContent = message || "";
    node.classList.toggle("visible", Boolean(message));
  }

  function startConfiguredPlayback(video, status) {
    var startMuted = FEATURES.unmuted === false;
    video.muted = startMuted;
    video.defaultMuted = startMuted;
    video.volume = startMuted ? 0 : 1;
    if (startMuted) video.setAttribute("muted", "");
    else video.removeAttribute("muted");
    if (FEATURES.autoplay === false) {
      status.textContent = startMuted ? "Ready · tap play to begin" : "Ready with sound · tap play to begin";
      return;
    }
    var playback = video.play();
    if (playback && typeof playback.catch === "function") {
      playback.catch(function () {
        status.textContent = startMuted ? "Ready · tap play to begin" : "Ready with sound · tap play to begin";
      });
    }
  }

  function attachHlsPlayer(video, source, qualitySelect, status, message) {
    var nativeHls = Boolean(video.canPlayType("application/vnd.apple.mpegurl"));
    var applePlatform = /Apple/i.test(navigator.vendor || "") || /iPhone|iPad|iPod/i.test(navigator.userAgent || "");

    qualitySelect.replaceChildren();
    var automatic = el("option", "", "Auto");
    automatic.value = "-1";
    qualitySelect.appendChild(automatic);

    if (nativeHls && applePlatform) {
      video.src = source;
      qualitySelect.disabled = true;
      status.textContent = "Starting automatically…";
      startConfiguredPlayback(video, status);
      return;
    }

    if (window.Hls && window.Hls.isSupported()) {
      activeHls = new window.Hls({
        enableWorker: true,
        startLevel: -1,
        capLevelToPlayerSize: true,
        maxBufferLength: 30,
        backBufferLength: 30
      });
      activeHls.loadSource(source);
      activeHls.attachMedia(video);
      activeHls.on(window.Hls.Events.MANIFEST_PARSED, function (_event, data) {
        setPlayerMessage(message, "");
        status.textContent = "HLS ready · " + data.levels.length + " " + (data.levels.length === 1 ? "quality" : "qualities");
        data.levels.forEach(function (level, index) {
          var label = level.height ? level.height + "p" : Math.round(level.bitrate / 1000) + " kb/s";
          var option = el("option", "", label);
          option.value = String(index);
          qualitySelect.appendChild(option);
        });
        startConfiguredPlayback(video, status);
      });
      activeHls.on(window.Hls.Events.LEVEL_SWITCHED, function (_event, data) {
        if (activeHls.autoLevelEnabled) qualitySelect.value = "-1";
        var level = activeHls.levels[data.level];
        status.textContent = "Playing " + (level && level.height ? level.height + "p" : "HLS");
      });
      activeHls.on(window.Hls.Events.ERROR, function (_event, data) {
        if (!data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          setPlayerMessage(message, "The stream was interrupted. Retrying…");
          activeHls.startLoad();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          setPlayerMessage(message, "The player encountered a media error. Recovering…");
          activeHls.recoverMediaError();
        } else {
          setPlayerMessage(message, "This HLS stream could not be played in the current browser.");
          activeHls.destroy();
          activeHls = null;
        }
      });
      qualitySelect.disabled = false;
      qualitySelect.addEventListener("change", function () {
        if (activeHls) activeHls.currentLevel = Number(qualitySelect.value);
      });
      return;
    }

    if (nativeHls) {
      video.src = source;
      qualitySelect.disabled = true;
      status.textContent = "Starting automatically…";
      startConfiguredPlayback(video, status);
      return;
    }

    qualitySelect.disabled = true;
    status.textContent = "Playback unsupported";
    setPlayerMessage(message, "This browser does not support HLS or Media Source playback.");
  }

  function buildTimeline(item, video, access) {
    var section = el("section", "section");
    var head = el("div", "section-head");
    append(head,
      el("h2", "", "Visual timeline"),
      el("p", "", "A frame every " + catalog.thumbnail_interval_seconds + " seconds · tap to seek")
    );
    var timeline = el("div", "timeline");
    timeline.setAttribute("aria-label", "Video thumbnail timeline");
    item.thumbnails.forEach(function (thumbnail) {
      var button = el("button");
      button.type = "button";
      button.setAttribute("aria-label", "Seek to " + formatDuration(thumbnail.time_seconds));
      var image = el("img");
      image.src = mediaAssetUrl(item, access, thumbnail.url);
      image.alt = "";
      image.loading = "lazy";
      append(button, image, el("time", "", formatDuration(thumbnail.time_seconds)));
      button.addEventListener("click", function () {
        var seek = function () {
          video.currentTime = Math.min(Number(thumbnail.time_seconds), Math.max(0, (video.duration || thumbnail.time_seconds + 1) - .1));
          video.focus({ preventScroll: true });
        };
        if (video.readyState >= 1) seek();
        else video.addEventListener("loadedmetadata", seek, { once: true });
        video.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      timeline.appendChild(button);
    });
    append(section, head, timeline);
    return section;
  }

  function buildDetailNavigation(item) {
    var shuffleMode = shuffleRequested();
    var sequence;
    var position;
    if (shuffleMode) {
      var order = ensureShuffleOrder(item.id);
      sequence = order.map(function (id) {
        return catalog.items.find(function (entry) { return entry.id === id; });
      }).filter(Boolean);
      position = sequence.findIndex(function (entry) { return entry.id === item.id; });
    } else {
      sequence = detailSequence(item);
      position = sequence.findIndex(function (entry) { return entry.id === item.id; });
    }
    position = Math.max(0, position);

    var navigation = el("nav", "detail-navigation");
    navigation.setAttribute("aria-label", "Move between gallery videos");
    var back = el("a", "back-link", shuffleMode ? "← Gallery · stop shuffle" : "← Back to gallery");
    back.href = routeHref(null, false);
    back.addEventListener("click", function (event) {
      event.preventDefault();
      navigate(null, { shuffle: false });
    });

    var sequenceNote = el("div", "detail-sequence");
    append(sequenceNote,
      el("strong", "", shuffleMode ? "Shuffle play" : "Filtered order"),
      el("span", "", sequence.length ? (position + 1) + " of " + sequence.length : "Current video")
    );

    var steps = el("div", "detail-steps");
    function buildStep(direction, label) {
      var target = sequence.length > 1
        ? sequence[(position + direction + sequence.length) % sequence.length]
        : null;
      var className = "video-step video-step-" + (direction < 0 ? "previous" : "next");
      if (!target) {
        var disabled = el("span", className + " is-disabled");
        append(disabled, el("b", "", label), el("small", "", "No other match"));
        return disabled;
      }
      var link = el("a", className);
      link.href = routeHref(target.id, shuffleMode);
      link.setAttribute("aria-label", label + " video: " + prettyVideoTitle(target.title || target.source_relative));
      append(link,
        el("b", "", direction < 0 ? "← " + label : label + " →"),
        el("small", "", prettyVideoTitle(target.title || target.source_relative))
      );
      link.addEventListener("click", function (event) {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (shuffleMode) goToShuffleNeighbor(item, direction);
        else navigate(target.id, { shuffle: false });
      });
      return link;
    }
    append(steps, buildStep(-1, "Previous"), buildStep(1, "Next"));
    append(navigation, back, sequenceNote, steps);
    return navigation;
  }

  function renderDetail(item) {
    destroyPlayer();
    var displayTitle = prettyVideoTitle(item.title || item.source_relative);
    document.title = displayTitle + " · " + brandText("gallery_name", "Video Gallery");
    app.replaceChildren();

    app.appendChild(buildDetailNavigation(item));

    var heading = el("header", "detail-heading");
    var sourceLine = el("p", "detail-file");
    append(sourceLine, el("span", "", "Original file"), el("code", "", item.source_relative));
    append(
      heading,
      el("p", "eyebrow", (shuffleRequested() ? "Shuffle playing · " : "Now playing · ") + brandText("owner_name", "Gallery")),
      el("h1", "", displayTitle),
      sourceLine
    );
    app.appendChild(heading);

    var player = el("section", "player-shell");
    player.setAttribute("tabindex", "-1");
    var stage = el("div", "player-stage");
    var video = el("video");
    video.controls = true;
    video.autoplay = FEATURES.autoplay !== false;
    video.muted = FEATURES.unmuted === false;
    video.defaultMuted = FEATURES.unmuted === false;
    video.volume = FEATURES.unmuted === false ? 0 : 1;
    video.playsInline = true;
    video.preload = "auto";
    video.setAttribute("crossorigin", "anonymous");
    video.setAttribute("aria-label", "Play " + displayTitle);
    var playerMessage = el("div", "player-message");
    playerMessage.setAttribute("role", "status");
    append(stage, video, playerMessage);

    var bar = el("div", "player-bar");
    var playerStatus = el("span", "player-status", "Preparing HLS stream…");
    var qualityWrap = el("label", "quality-wrap");
    qualityWrap.appendChild(el("span", "", "Quality"));
    var quality = el("select", "quality-select");
    quality.setAttribute("aria-label", "Streaming quality");
    qualityWrap.appendChild(quality);
    var playerActions = el("div", "player-actions");
    append(playerActions, buildShareButton(item, "detail-share-button", "Copy guest link"), qualityWrap);
    append(bar, playerStatus, playerActions);
    append(player, stage, bar);
    app.appendChild(player);
    video.addEventListener("playing", function () {
      playerStatus.textContent = (video.muted ? "Playing muted · tap the speaker for sound" : "Playing with sound") + (shuffleRequested() ? " · shuffle on" : "");
    });
    video.addEventListener("volumechange", function () {
      if (!video.paused) playerStatus.textContent = (video.muted ? "Playing muted · tap the speaker for sound" : "Playing with sound") + (shuffleRequested() ? " · shuffle on" : "");
    });
    video.addEventListener("ended", function () {
      if (!shuffleRequested()) return;
      playerStatus.textContent = "Shuffle · choosing the next matching video…";
      window.setTimeout(function () {
        if (!video.isConnected) return;
        if (!goToShuffleNeighbor(item, 1)) playerStatus.textContent = "Shuffle finished · this is the only matching video";
      }, 650);
    });
    loadMediaAccess(item).then(function (access) {
      if (!video.isConnected) return;
      video.poster = mediaAssetUrl(item, access, item.poster_url);
      attachHlsPlayer(video, access.hls_url, quality, playerStatus, playerMessage);
    }).catch(function () {
      playerStatus.textContent = "Stream unavailable";
      setPlayerMessage(playerMessage, "The streaming URL could not be prepared. Please reload and try again.");
    });
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        if (!player.isConnected) return;
        var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        player.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
        window.setTimeout(function () {
          if (!video.isConnected) return;
          try {
            video.focus({ preventScroll: true });
          } catch (_error) {
            video.focus();
          }
        }, reducedMotion ? 0 : 350);
      });
    });

    var contentSignals = buildContentSignals(item);
    app.appendChild(contentSignals);
    loadQualityReport(item).then(function (loaded) {
      if (!loaded || !contentSignals.isConnected) return;
      contentSignals.insertAdjacentElement("afterend", buildQualityReport(item, loaded.report, loaded.base, video));
    });

    var overview = el("section", "section");
    var overviewHead = el("div", "section-head");
    append(overviewHead, el("h2", "", "Media facts"), el("p", "", "Read from the original file"));
    var stats = el("dl", "stat-grid");
    var firstVideo = item.video_streams[0] || {};
    var firstAudio = item.audio_streams[0] || {};
    addStat(stats, "Duration", formatDuration(item.duration_seconds));
    addStat(stats, "Length", durationClass(item).label + " · " + durationClass(item).description);
    addStat(stats, "File size", formatBytes(item.size_bytes));
    addStat(stats, "Resolution", resolution(item));
    addStat(stats, "Source video", titleCaseCodec(firstVideo.codec_name));
    addStat(stats, "Source audio", item.audio_streams.length ? titleCaseCodec(firstAudio.codec_name) : "No audio");
    addStat(stats, "Container", item.format_long_name || item.format_name);
    addStat(stats, "Overall rate", formatBitrate(item.bit_rate));
    addStat(stats, "Uploaded", item.uploaded_at ? formatDate(item.uploaded_at, true) : "Not yet indexed");
    addStat(stats, "Created", item.creation_at ? formatDate(item.creation_at, true) : "Not embedded in source");
    addStat(stats, "Modified", formatDate(item.modified_at, true));
    append(overview, overviewHead, stats);
    app.appendChild(overview);

    var streamSection = el("section", "section");
    var streamHead = el("div", "section-head");
    append(streamHead, el("h2", "", "Source tracks"), el("p", "", (item.video_streams.length + item.audio_streams.length + item.subtitle_streams.length) + " detected"));
    var tracks = el("div", "track-grid");
    item.video_streams.forEach(function (stream, index) { tracks.appendChild(buildTrackCard(stream, "Video", index)); });
    item.audio_streams.forEach(function (stream, index) { tracks.appendChild(buildTrackCard(stream, "Audio", index)); });
    item.subtitle_streams.forEach(function (stream, index) { tracks.appendChild(buildTrackCard(stream, "Subtitle", index)); });
    append(streamSection, streamHead, tracks);
    app.appendChild(streamSection);

    var hlsSection = el("section", "section");
    var hlsHead = el("div", "section-head");
    append(
      hlsHead,
      el("h2", "", "Streaming renditions"),
      el("p", "", "H.264 + AAC · " + Number(catalog.hls_segment_seconds || 6) + "-second HLS segments")
    );
    var table = el("table", "stream-table");
    var thead = el("thead");
    var headRow = el("tr");
    ["Quality", "Dimensions", "Video rate", "Audio rate", "Bandwidth"].forEach(function (label) { headRow.appendChild(el("th", "", label)); });
    thead.appendChild(headRow);
    var tbody = el("tbody");
    item.hls_variants.forEach(function (variant) {
      var row = el("tr");
      append(row,
        el("td", "", variant.name),
        el("td", "", variant.width + "×" + variant.height),
        el("td", "", formatBitrate(variant.video_bitrate)),
        el("td", "", variant.audio_bitrate ? formatBitrate(variant.audio_bitrate) : "No audio"),
        el("td", "", formatBitrate(variant.bandwidth))
      );
      tbody.appendChild(row);
    });
    append(table, thead, tbody);
    append(hlsSection, hlsHead, table);
    app.appendChild(hlsSection);

    if (item.thumbnails.length) {
      var timelinePlaceholder = el("section", "section timeline-loading");
      append(timelinePlaceholder, el("div", "section-head"), el("p", "monitor-note", "Preparing the visual timeline…"));
      app.appendChild(timelinePlaceholder);
      loadMediaAccess(item).then(function (access) {
        if (timelinePlaceholder.isConnected) timelinePlaceholder.replaceWith(buildTimeline(item, video, access));
      });
    }
  }

  function renderRoute() {
    if (!catalog) return;
    var id = currentVideoId();
    if (!id) {
      renderLibrary();
      return;
    }
    var item = catalog.items.find(function (entry) { return entry.id === id; });
    if (!item) {
      document.title = "Video not found · " + brandText("gallery_name", "Video Gallery");
      app.replaceChildren();
      var missing = el("section", "error-state");
      var home = el("a", "button", "Return to the archive");
      home.href = "./";
      home.addEventListener("click", function (event) { event.preventDefault(); navigate(null); });
      append(missing, el("p", "eyebrow", "Not found"), el("h1", "", "That video is no longer listed."), el("p", "", "It may have been moved, renamed, or removed."), home);
      app.appendChild(missing);
      return;
    }
    renderDetail(item);
  }

  function loadCatalog(refreshOnly) {
    fetch("data/catalog.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Catalog request returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.items)) throw new Error("Catalog data is invalid");
        var changed = !catalog || catalog.generated_at !== data.generated_at || catalog.items.length !== data.items.length;
        catalog = data;
        headerStatus.textContent = data.items.length + " " + videoNoun(data.items.length) + " · updated " + formatDate(data.generated_at, true);
        updatedNode.textContent = "Catalog updated " + formatDate(data.generated_at, true);
        if (!refreshOnly || (changed && !currentVideoId())) renderRoute();
      })
      .catch(function (error) {
        if (!catalog) showError("The video catalog could not be opened.", "Run the media scanner once, then reload this page. " + error.message);
      });
  }

  function loadContentIndex(refreshOnly) {
    fetch("data/content-index.json?_=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Content index request returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.items || typeof data.items !== "object") return;
        var changed = contentIndex.updated_at !== data.updated_at || Number(contentIndex.analyzed_count || 0) !== Number(data.analyzed_count || 0);
        contentIndex = data;
        var focusedControl = document.activeElement && document.activeElement.closest && document.activeElement.closest(".toolbar");
        if (catalog && currentVideoId() && !refreshOnly) renderRoute();
        else if (catalog && !currentVideoId() && (!refreshOnly || changed) && !focusedControl) renderLibrary();
      })
      .catch(function () {
        // Filename tags remain available if the optional visual analyzer is not installed yet.
      });
  }

  window.addEventListener("popstate", function () {
    loadLibraryStateFromUrl();
    renderRoute();
    if (!currentVideoId()) {
      if (FEATURES.encoder_status) loadEncodingProgress();
      if (FEATURES.content_analysis) loadCategoryProgress();
      if (FEATURES.quality_analysis !== false) loadQualityProgress();
    }
  });
  window.addEventListener("beforeunload", function () {
    destroyPlayer();
    if (telemetryTimer) window.clearInterval(telemetryTimer);
    if (categoryTimer) window.clearInterval(categoryTimer);
    if (qualityTimer) window.clearInterval(qualityTimer);
    if (catalogTimer) window.clearInterval(catalogTimer);
    if (contentIndexTimer) window.clearInterval(contentIndexTimer);
  });
  loadLibraryStateFromUrl();
  if (FEATURES.encoder_status) {
    loadEncodingProgress();
    telemetryTimer = window.setInterval(loadEncodingProgress, 5000);
  }
  if (FEATURES.content_analysis) {
    loadCategoryProgress();
    categoryTimer = window.setInterval(loadCategoryProgress, 5000);
    loadContentIndex();
    contentIndexTimer = window.setInterval(function () { loadContentIndex(true); }, 60000);
  }
  if (FEATURES.quality_analysis !== false) {
    loadQualityProgress();
    qualityTimer = window.setInterval(loadQualityProgress, 5000);
  }
  loadCatalog();
  catalogTimer = window.setInterval(function () { loadCatalog(true); }, 30000);
}());
