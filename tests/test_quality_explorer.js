"use strict";

var assert = require("assert");
var explorer = require("../site/assets/quality-explorer.js");

function close(actual, expected, epsilon) {
  assert.ok(
    Math.abs(actual - expected) <= (epsilon || 1e-9),
    "expected " + actual + " to be close to " + expected
  );
}

function point(time, values) {
  var result = {
    time: time,
    frame: time,
    scene: 1,
    composite: null,
    vmaf: null,
    phone: null,
    ssim: null,
    ssimNormalized: null,
    psnr: null,
    psnrNormalized: null,
    phash: null,
    temporalPhash: null
  };
  Object.keys(values || {}).forEach(function (key) { result[key] = values[key]; });
  return result;
}

assert.strictEqual(global.HlsQualityExplorer, explorer);
assert.deepStrictEqual(Object.keys(explorer.METRICS), [
  "composite", "vmaf", "phone", "ssim", "ssimNormalized",
  "psnr", "psnrNormalized", "phash", "temporalPhash"
]);

(function testReportPointsPreferFullFramesAndNormalizeMetrics() {
  var report = {
    settings: { fps: 2 },
    timeline: [{ time_seconds: 99, composite: 1 }],
    frames: [
      {
        frame: 2,
        scene: 3,
        vmaf_standard: 80,
        vmaf_phone: 84,
        ssim: 0.95,
        psnr_y: 35,
        phash_similarity: 90,
        temporal_consistency: 88
      },
      {
        frame: 0,
        scene: 1,
        time_seconds: 0,
        composite: 92,
        metrics: {
          vmaf_standard: 93,
          ssim: 0.99,
          ssim_normalized: 98.5,
          psnr_y: 44,
          psnr_normalized: 77,
          phash_similarity: 96
        }
      }
    ]
  };
  var points = explorer.reportPoints(report);
  assert.strictEqual(points.length, 2);
  assert.strictEqual(points[0].time, 0);
  assert.strictEqual(points[0].composite, 92);
  assert.strictEqual(points[0].ssimNormalized, 98.5);
  assert.strictEqual(points[0].psnrNormalized, 77);
  assert.strictEqual(points[1].time, 1);
  assert.strictEqual(points[1].scene, 3);
  assert.strictEqual(points[1].ssimNormalized, 95);
  assert.strictEqual(points[1].psnrNormalized, 50);
  close(points[1].composite, 80);
}());

(function testReportPointsFallBackToTimelineAndAliases() {
  var points = explorer.reportPoints({
    frames: [],
    quality_over_time: {
      points: [
        {
          timestamp_seconds: 4,
          overall_score: 77,
          standard_vmaf: 78,
          phone_vmaf: null,
          ssim_y: 0.9,
          normalized_psnr: 60,
          psnr: 38,
          phash: 70,
          temporal_phash: 65
        }
      ]
    }
  });
  assert.strictEqual(points.length, 1);
  assert.strictEqual(points[0].time, 4);
  assert.strictEqual(points[0].composite, 77);
  assert.strictEqual(points[0].vmaf, 78);
  assert.strictEqual(points[0].phone, null);
  assert.strictEqual(points[0].ssimNormalized, 90);
  assert.strictEqual(points[0].psnrNormalized, 60);
  assert.strictEqual(points[0].temporalPhash, 65);
}());

(function testTinyDownsampleLimitRemainsBounded() {
  var points = [];
  for (var index = 0; index < 30; index += 1) {
    points.push(point(index, {
      composite: index,
      vmaf: 30 - index,
      ssimNormalized: 90,
      psnrNormalized: 80,
      phash: 70
    }));
  }
  var sampled = explorer.downsample(
    points, 0, 29, 3, ["composite", "vmaf", "ssimNormalized", "psnrNormalized", "phash"]
  );
  assert.ok(sampled.length <= 3);
  assert.strictEqual(sampled[0].time, 0);
  assert.strictEqual(sampled[sampled.length - 1].time, 29);
}());

(function testParseExactMediaPlaylistRanges() {
  var playlist = "\uFEFF#EXTM3U\r\n" +
    "#EXT-X-TARGETDURATION:7\r\n" +
    "#EXTINF:6.006,\r\n" +
    "seg-000.ts\r\n" +
    "#EXT-X-DISCONTINUITY\r\n" +
    "#EXTINF: 5.5,Second segment\r\n" +
    "#EXT-X-BYTERANGE:1000@0\r\n" +
    "path/seg-001.ts?token=a,b\r\n" +
    "#EXTINF:-1,\r\n" +
    "ignored.ts\r\n" +
    "#EXT-X-ENDLIST\r\n";
  var ranges = explorer.parseMediaPlaylist(playlist);
  assert.strictEqual(ranges.length, 2);
  assert.deepStrictEqual(ranges[0], {
    index: 1,
    start: 0,
    end: 6.006,
    duration: 6.006,
    uri: "seg-000.ts",
    exact: true
  });
  close(ranges[1].start, 6.006);
  close(ranges[1].end, 11.506);
  assert.strictEqual(ranges[1].uri, "path/seg-001.ts?token=a,b");
  assert.strictEqual(ranges[1].exact, true);
  assert.deepStrictEqual(explorer.parseMediaPlaylist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv0/index.m3u8"), []);
}());

(function testNominalSegmentsHaveClippedFinalRange() {
  var ranges = explorer.nominalSegments(13, 6);
  assert.deepStrictEqual(ranges, [
    { index: 1, start: 0, end: 6, duration: 6, uri: null, exact: false },
    { index: 2, start: 6, end: 12, duration: 6, uri: null, exact: false },
    { index: 3, start: 12, end: 13, duration: 1, uri: null, exact: false }
  ]);
  assert.deepStrictEqual(explorer.nominalSegments(0, 6), []);
  assert.deepStrictEqual(explorer.nominalSegments(10, 0), []);
}());

(function testReportScenesUseSecondsOrFrameFallback() {
  var scenes = explorer.reportScenes({
    settings: { fps: 10 },
    scenes: [
      {
        index: 2,
        start_frame: 50,
        end_frame: 80,
        scene_change_strength: 12.5,
        label: "Second"
      },
      {
        index: 1,
        start_seconds: 0,
        duration_seconds: 5
      }
    ]
  });
  assert.strictEqual(scenes.length, 2);
  assert.strictEqual(scenes[0].index, 1);
  assert.strictEqual(scenes[0].start, 0);
  assert.strictEqual(scenes[0].end, 5);
  assert.strictEqual(scenes[1].index, 2);
  assert.strictEqual(scenes[1].start, 5);
  assert.strictEqual(scenes[1].end, 8);
  assert.strictEqual(scenes[1].label, "Second");
  assert.strictEqual(scenes[1].sceneChangeStrength, 12.5);
}());

(function testRangeSummaryUsesMeanWorstDecileAndCompositeFormula() {
  var points = [];
  for (var index = 0; index < 20; index += 1) {
    points.push(point(index, {
      composite: index,
      vmaf: 80 + index,
      ssim: 0.9,
      ssimNormalized: 90,
      psnr: 40,
      psnrNormalized: 66.6666666667,
      phash: 95,
      temporalPhash: 85
    }));
  }
  var summary = explorer.summarizeRange(points, 0, 20);
  assert.strictEqual(summary.pointCount, 20);
  close(summary.metrics.composite.mean, 9.5);
  close(summary.metrics.composite.worstDecile, 0.5);
  close(summary.score, 6.8);
  assert.strictEqual(summary.band, "Poor");
  assert.strictEqual(summary.metrics.phone.mean, null);
  assert.strictEqual(summary.metrics.phone.worstDecile, null);
}());

(function testSummarizeRangesPreservesRangeMetadata() {
  var points = [
    point(0, { composite: 95, vmaf: 95 }),
    point(1, { composite: 85, vmaf: 85 }),
    point(2, { composite: 75, vmaf: 75 })
  ];
  var summaries = explorer.summarizeRanges(points, [
    { index: 7, start: 0, end: 2, duration: 2, uri: "seg.ts", exact: true, label: "Opening" },
    { start: 2, end: 3, exact: false }
  ]);
  assert.strictEqual(summaries.length, 2);
  assert.strictEqual(summaries[0].index, 7);
  assert.strictEqual(summaries[0].uri, "seg.ts");
  assert.strictEqual(summaries[0].exact, true);
  assert.strictEqual(summaries[0].label, "Opening");
  assert.strictEqual(summaries[0].pointCount, 2);
  close(summaries[0].score, 88.5);
  assert.strictEqual(summaries[0].band, "Very good");
  assert.strictEqual(summaries[1].index, 2);
  assert.strictEqual(summaries[1].uri, null);
  assert.strictEqual(summaries[1].exact, false);
}());

(function testRangeAwareMultiMetricExtremaDownsampling() {
  var points = [];
  for (var index = 0; index < 1000; index += 1) {
    points.push(point(index, {
      composite: 80,
      vmaf: 80,
      ssimNormalized: 90,
      psnrNormalized: 70,
      phash: 95
    }));
  }
  points[250].composite = 2;
  points[251].composite = 99;
  points[500].vmaf = 1;
  points[501].vmaf = 100;
  points[750].phash = 3;
  points[751].phash = 98;

  var sampled = explorer.downsample(
    points, 200, 800, 80, ["composite", "vmaf", "phash"]
  );
  assert.ok(sampled.length <= 80);
  assert.strictEqual(sampled[0].time, 200);
  assert.strictEqual(sampled[sampled.length - 1].time, 800);
  [250, 251, 500, 501, 750, 751].forEach(function (time) {
    assert.ok(sampled.some(function (sample) { return sample.time === time; }), "missing extrema at " + time);
  });
  for (var index = 1; index < sampled.length; index += 1) {
    assert.ok(sampled[index].time >= sampled[index - 1].time);
  }
  assert.deepStrictEqual(explorer.downsample(points, 2000, 3000, 10, ["composite"]), []);
}());

(function testNearestPointUsesBinarySearchAndEarlierTie() {
  var points = [
    point(0, { composite: 1 }),
    point(5, { composite: 2 }),
    point(10, { composite: 3 }),
    point(20, { composite: 4 })
  ];
  assert.strictEqual(explorer.nearestPointIndex([], 1), -1);
  assert.strictEqual(explorer.nearestPointIndex(points, -10), 0);
  assert.strictEqual(explorer.nearestPointIndex(points, 7.5), 1);
  assert.strictEqual(explorer.nearestPointIndex(points, 9), 2);
  assert.strictEqual(explorer.nearestPointIndex(points, 99), 3);
}());

console.log("quality explorer tests passed");
