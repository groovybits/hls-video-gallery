(function (root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.HlsQualityExplorer = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var METRICS = Object.freeze({
    composite: Object.freeze({ key: "composite", label: "Overall", unit: "", score: true }),
    vmaf: Object.freeze({ key: "vmaf", label: "Standard VMAF", unit: "", score: true }),
    phone: Object.freeze({ key: "phone", label: "Phone VMAF", unit: "", score: true, informational: true }),
    ssim: Object.freeze({ key: "ssim", label: "SSIM", unit: "", score: false }),
    ssimNormalized: Object.freeze({ key: "ssimNormalized", label: "SSIM score", unit: "", score: true }),
    psnr: Object.freeze({ key: "psnr", label: "PSNR", unit: " dB", score: false }),
    psnrNormalized: Object.freeze({ key: "psnrNormalized", label: "PSNR score", unit: "", score: true }),
    phash: Object.freeze({ key: "phash", label: "pHash", unit: "", score: true }),
    temporalPhash: Object.freeze({
      key: "temporalPhash",
      label: "Temporal pHash",
      unit: "",
      score: true,
      informational: true
    })
  });
  var METRIC_KEYS = Object.keys(METRICS);
  var DEFAULT_DOWNSAMPLE_METRICS = [
    "composite", "vmaf", "ssimNormalized", "psnrNormalized", "phash", "phone", "temporalPhash"
  ];

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    var result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function clampScore(value) {
    var result = finiteNumber(value);
    return result === null ? null : Math.max(0, Math.min(100, result));
  }

  function metricValue(value) {
    var direct = finiteNumber(value);
    if (direct !== null) return direct;
    if (!value || typeof value !== "object") return null;
    var aliases = ["value", "score", "mean", "average", "weighted_mean", "p50", "median"];
    for (var index = 0; index < aliases.length; index += 1) {
      var nested = finiteNumber(value[aliases[index]]);
      if (nested !== null) return nested;
    }
    return null;
  }

  function pointMetric(point, aliases) {
    if (!point || typeof point !== "object") return null;
    var sources = [point.metrics, point];
    for (var sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
      var source = sources[sourceIndex];
      if (!source || typeof source !== "object") continue;
      for (var aliasIndex = 0; aliasIndex < aliases.length; aliasIndex += 1) {
        if (Object.prototype.hasOwnProperty.call(source, aliases[aliasIndex])) {
          var value = metricValue(source[aliases[aliasIndex]]);
          if (value !== null) return value;
        }
      }
    }
    return null;
  }

  function reportFrameRate(report) {
    var candidates = [
      report && report.settings && report.settings.fps,
      report && report.settings && report.settings.frame_rate,
      report && report.analysis && report.analysis.frame_rate,
      report && report.video && report.video.analysis_fps
    ];
    for (var index = 0; index < candidates.length; index += 1) {
      var value = finiteNumber(candidates[index]);
      if (value !== null && value > 0) return value;
    }
    return null;
  }

  function pointTime(point, fallbackIndex, frameRate) {
    var aliases = [
      "time_seconds", "timestamp_seconds", "timestamp", "time", "pts_time", "start_seconds", "start"
    ];
    for (var index = 0; index < aliases.length; index += 1) {
      var value = finiteNumber(point && point[aliases[index]]);
      if (value !== null) return Math.max(0, value);
    }
    var frame = finiteNumber(point && point.frame);
    if (frame !== null && frameRate) return Math.max(0, frame / frameRate);
    return Math.max(0, Number(fallbackIndex) || 0);
  }

  function normalizeSsim(raw, explicit) {
    var normalized = clampScore(explicit);
    if (normalized !== null) return normalized;
    if (raw === null) return null;
    return clampScore(Math.abs(raw) <= 1.5 ? raw * 100 : raw);
  }

  function normalizePsnr(raw, explicit) {
    var normalized = clampScore(explicit);
    if (normalized !== null) return normalized;
    return raw === null ? null : clampScore((raw - 20) / 30 * 100);
  }

  function calculatedComposite(vmaf, ssimNormalized, psnrNormalized, phash) {
    if ([vmaf, ssimNormalized, psnrNormalized, phash].some(function (value) {
      return value === null;
    })) return null;
    return clampScore(
      0.50 * vmaf +
      0.20 * ssimNormalized +
      0.15 * psnrNormalized +
      0.15 * phash
    );
  }

  function reportPointSource(report) {
    if (!report || typeof report !== "object") return [];
    var candidates = [report.frames, report.timeline, report.samples, report.quality_over_time];
    for (var index = 0; index < candidates.length; index += 1) {
      var values = candidates[index];
      if (values && !Array.isArray(values)) {
        values = values.frames || values.samples || values.points || [];
      }
      if (Array.isArray(values) && values.length) return values;
    }
    return [];
  }

  function reportPoints(report) {
    var values = reportPointSource(report);
    var frameRate = reportFrameRate(report);
    return values.map(function (source, originalIndex) {
      var ssim = pointMetric(source, ["ssim", "ssim_y"]);
      var psnr = pointMetric(source, ["psnr", "psnr_y"]);
      var ssimNormalized = normalizeSsim(
        ssim,
        pointMetric(source, ["ssim_normalized", "normalized_ssim", "ssim_score"])
      );
      var psnrNormalized = normalizePsnr(
        psnr,
        pointMetric(source, ["psnr_normalized", "normalized_psnr", "psnr_score"])
      );
      var vmaf = clampScore(pointMetric(
        source, ["vmaf_standard", "vmaf", "standard_vmaf", "libvmaf"]
      ));
      var phash = clampScore(pointMetric(source, ["phash_similarity", "phash"]));
      var composite = clampScore(pointMetric(source, ["composite", "score", "overall_score"]));
      if (composite === null) {
        composite = calculatedComposite(vmaf, ssimNormalized, psnrNormalized, phash);
      }
      return {
        time: pointTime(source, originalIndex, frameRate),
        frame: finiteNumber(source && source.frame),
        scene: source && source.scene !== undefined && source.scene !== null ? source.scene : null,
        composite: composite,
        vmaf: vmaf,
        phone: clampScore(pointMetric(source, ["vmaf_phone", "phone_vmaf", "vmaf_mobile"])),
        ssim: ssim,
        ssimNormalized: ssimNormalized,
        psnr: psnr,
        psnrNormalized: psnrNormalized,
        phash: phash,
        temporalPhash: clampScore(pointMetric(
          source, ["temporal_consistency", "temporal_phash", "temporal_pHash"]
        )),
        source: source,
        _order: originalIndex
      };
    }).filter(function (point) {
      return METRIC_KEYS.some(function (key) { return point[key] !== null; });
    }).sort(function (left, right) {
      return left.time === right.time ? left._order - right._order : left.time - right.time;
    }).map(function (point) {
      delete point._order;
      return point;
    });
  }

  function parseMediaPlaylist(text) {
    if (typeof text !== "string" || !text.trim()) return [];
    var lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
    var ranges = [];
    var pendingDuration = null;
    var cursor = 0;
    for (var index = 0; index < lines.length; index += 1) {
      var line = lines[index].trim();
      var match = /^#EXTINF\s*:\s*([0-9]+(?:\.[0-9]+)?)(?:\s*,.*)?$/i.exec(line);
      if (match) {
        var duration = finiteNumber(match[1]);
        pendingDuration = duration !== null && duration > 0 ? duration : null;
        continue;
      }
      if (!line || line.charAt(0) === "#" || pendingDuration === null) continue;
      var start = cursor;
      var end = start + pendingDuration;
      ranges.push({
        index: ranges.length + 1,
        start: start,
        end: end,
        duration: pendingDuration,
        uri: line,
        exact: true
      });
      cursor = end;
      pendingDuration = null;
    }
    return ranges;
  }

  function nominalSegments(duration, seconds) {
    var total = finiteNumber(duration);
    var interval = finiteNumber(seconds);
    if (total === null || interval === null || total <= 0 || interval <= 0) return [];
    var count = Math.ceil(total / interval);
    if (count > 1000000) throw new RangeError("Nominal segment count is unreasonably large");
    var ranges = [];
    for (var index = 0; index < count; index += 1) {
      var start = index * interval;
      var end = Math.min(total, start + interval);
      ranges.push({
        index: index + 1,
        start: start,
        end: end,
        duration: end - start,
        uri: null,
        exact: false
      });
    }
    return ranges;
  }

  function reportScenes(report) {
    var scenes = report && (
      report.scenes ||
      report.scene_analysis && report.scene_analysis.scenes ||
      report.worst_scenes
    );
    if (!Array.isArray(scenes)) return [];
    var frameRate = reportFrameRate(report);
    return scenes.map(function (scene, ordinal) {
      var start = finiteNumber(scene && (
        scene.start_seconds !== undefined ? scene.start_seconds :
          scene.start !== undefined ? scene.start : scene.timestamp_seconds
      ));
      var end = finiteNumber(scene && (
        scene.end_seconds !== undefined ? scene.end_seconds : scene.end
      ));
      var duration = finiteNumber(scene && scene.duration_seconds);
      var startFrame = finiteNumber(scene && scene.start_frame);
      var endFrame = finiteNumber(scene && scene.end_frame);
      if (start === null && startFrame !== null && frameRate) start = startFrame / frameRate;
      if (end === null && endFrame !== null && frameRate) end = endFrame / frameRate;
      if (start === null) start = 0;
      if (end === null && duration !== null) end = start + duration;
      if (end === null || end <= start) return null;
      return {
        index: scene.index !== undefined && scene.index !== null ? scene.index : ordinal + 1,
        start: Math.max(0, start),
        end: Math.max(0, end),
        duration: Math.max(0, end - start),
        uri: null,
        exact: true,
        label: scene.label || scene.name || null,
        sceneChangeStrength: finiteNumber(scene.scene_change_strength),
        source: scene
      };
    }).filter(function (scene) {
      return scene && scene.end > scene.start;
    }).sort(function (left, right) {
      return left.start === right.start
        ? finiteNumber(left.index) - finiteNumber(right.index)
        : left.start - right.start;
    });
  }

  function lowerBound(points, time) {
    var low = 0;
    var high = points.length;
    while (low < high) {
      var middle = low + Math.floor((high - low) / 2);
      if (points[middle].time < time) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function upperBound(points, time) {
    var low = 0;
    var high = points.length;
    while (low < high) {
      var middle = low + Math.floor((high - low) / 2);
      if (points[middle].time <= time) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function metricSummary(values) {
    var available = values.filter(function (value) {
      return finiteNumber(value) !== null;
    }).map(Number);
    if (!available.length) return { mean: null, worstDecile: null };
    var mean = available.reduce(function (sum, value) { return sum + value; }, 0) / available.length;
    var ordered = available.slice().sort(function (left, right) { return left - right; });
    var count = Math.max(1, Math.ceil(ordered.length * 0.10));
    var worst = ordered.slice(0, count).reduce(function (sum, value) {
      return sum + value;
    }, 0) / count;
    return { mean: mean, worstDecile: worst };
  }

  function qualityBand(score) {
    if (score === null) return null;
    if (score >= 90) return "Excellent";
    if (score >= 80) return "Very good";
    if (score >= 70) return "Good";
    if (score >= 55) return "Fair";
    return "Poor";
  }

  function summarizeRange(points, start, end) {
    var rangeStart = finiteNumber(start);
    var rangeEnd = finiteNumber(end);
    if (rangeStart === null) rangeStart = 0;
    if (rangeEnd === null || rangeEnd < rangeStart) rangeEnd = rangeStart;
    var first = lowerBound(points, rangeStart);
    var last = lowerBound(points, rangeEnd);
    var metrics = {};
    METRIC_KEYS.forEach(function (key) {
      var values = [];
      for (var index = first; index < last; index += 1) values.push(points[index][key]);
      metrics[key] = metricSummary(values);
    });
    var composite = metrics.composite;
    var score = composite.mean === null || composite.worstDecile === null
      ? null
      : clampScore(0.70 * composite.mean + 0.30 * composite.worstDecile);
    return {
      start: rangeStart,
      end: rangeEnd,
      duration: Math.max(0, rangeEnd - rangeStart),
      pointCount: Math.max(0, last - first),
      score: score,
      band: qualityBand(score),
      metrics: metrics
    };
  }

  function summarizeRanges(points, ranges) {
    if (!Array.isArray(ranges)) return [];
    return ranges.map(function (range, ordinal) {
      var start = finiteNumber(range && range.start);
      var end = finiteNumber(range && range.end);
      var duration = finiteNumber(range && range.duration);
      if (start === null) start = 0;
      if (end === null && duration !== null) end = start + duration;
      if (end === null) end = start;
      var summary = summarizeRange(points, start, end);
      var result = {};
      Object.keys(range || {}).forEach(function (key) { result[key] = range[key]; });
      result.index = range && range.index !== undefined && range.index !== null
        ? range.index : ordinal + 1;
      result.start = summary.start;
      result.end = summary.end;
      result.duration = summary.duration;
      result.uri = range && range.uri !== undefined ? range.uri : null;
      result.exact = Boolean(range && range.exact);
      result.pointCount = summary.pointCount;
      result.score = summary.score;
      result.band = summary.band;
      result.metrics = summary.metrics;
      return result;
    });
  }

  function validMetricKeys(metricKeys) {
    var requested = Array.isArray(metricKeys) && metricKeys.length
      ? metricKeys : DEFAULT_DOWNSAMPLE_METRICS;
    var seen = {};
    return requested.filter(function (key) {
      if (!Object.prototype.hasOwnProperty.call(METRICS, key) || seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  function downsample(points, start, end, limit, metricKeys) {
    if (!Array.isArray(points) || !points.length) return [];
    var rangeStart = finiteNumber(start);
    var rangeEnd = finiteNumber(end);
    if (rangeStart === null) rangeStart = points[0].time;
    if (rangeEnd === null) rangeEnd = points[points.length - 1].time;
    if (rangeEnd < rangeStart) {
      var swap = rangeStart;
      rangeStart = rangeEnd;
      rangeEnd = swap;
    }
    var first = lowerBound(points, rangeStart);
    var lastExclusive = upperBound(points, rangeEnd);
    if (first >= lastExclusive) return [];
    var count = lastExclusive - first;
    var maximum = Math.max(1, Math.floor(finiteNumber(limit) || 500));
    if (count <= maximum) return points.slice(first, lastExclusive);
    if (maximum === 1) return [points[first]];

    var keys = validMetricKeys(metricKeys);
    var selected = {};
    selected[first] = true;
    selected[lastExclusive - 1] = true;
    var availableSlots = maximum - 2;
    var metricCapacity = Math.floor(availableSlots / 2);
    if (keys.length > metricCapacity) keys = keys.slice(0, metricCapacity);
    var extremaPerBucket = Math.max(1, keys.length * 2);
    var bucketCount = keys.length
      ? Math.floor(availableSlots / extremaPerBucket)
      : 0;

    for (var bucket = 0; bucket < bucketCount; bucket += 1) {
      var bucketStart = first + Math.floor(bucket * count / bucketCount);
      var bucketEnd = first + Math.floor((bucket + 1) * count / bucketCount);
      if (bucketEnd <= bucketStart) bucketEnd = bucketStart + 1;
      keys.forEach(function (key) {
        var minimumIndex = -1;
        var maximumIndex = -1;
        var minimumValue = Infinity;
        var maximumValue = -Infinity;
        for (var index = bucketStart; index < bucketEnd && index < lastExclusive; index += 1) {
          var value = finiteNumber(points[index][key]);
          if (value === null) continue;
          if (value < minimumValue) {
            minimumValue = value;
            minimumIndex = index;
          }
          if (value > maximumValue) {
            maximumValue = value;
            maximumIndex = index;
          }
        }
        if (minimumIndex >= 0) selected[minimumIndex] = true;
        if (maximumIndex >= 0) selected[maximumIndex] = true;
      });
    }

    var selectedIndexes = Object.keys(selected).map(Number);
    var remaining = maximum - selectedIndexes.length;
    if (remaining > 0) {
      for (var fill = 1; fill <= remaining; fill += 1) {
        var candidate = first + Math.round(fill * (count - 1) / (remaining + 1));
        if (!selected[candidate]) selected[candidate] = true;
      }
    }
    selectedIndexes = Object.keys(selected).map(Number).sort(function (left, right) {
      return left - right;
    });
    if (selectedIndexes.length > maximum) selectedIndexes = selectedIndexes.slice(0, maximum);
    return selectedIndexes.map(function (index) { return points[index]; });
  }

  function nearestPointIndex(points, time) {
    if (!Array.isArray(points) || !points.length) return -1;
    var target = finiteNumber(time);
    if (target === null || target <= points[0].time) return 0;
    var finalIndex = points.length - 1;
    if (target >= points[finalIndex].time) return finalIndex;
    var after = lowerBound(points, target);
    if (after <= 0) return 0;
    if (after >= points.length) return finalIndex;
    var before = after - 1;
    return target - points[before].time <= points[after].time - target ? before : after;
  }

  return Object.freeze({
    METRICS: METRICS,
    reportPoints: reportPoints,
    parseMediaPlaylist: parseMediaPlaylist,
    nominalSegments: nominalSegments,
    reportScenes: reportScenes,
    summarizeRange: summarizeRange,
    summarizeRanges: summarizeRanges,
    downsample: downsample,
    nearestPointIndex: nearestPointIndex
  });
}));
