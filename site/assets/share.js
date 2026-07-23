(function () {
  "use strict";

  var app = document.getElementById("share-app");
  if (!app) return;

  var video = app.querySelector("video");
  var message = app.querySelector(".share-message");
  var status = app.querySelector(".share-status");
  var quality = app.querySelector(".share-quality");
  var endpoint = app.getAttribute("data-media-endpoint");
  var activeHls = null;

  function showError(text) {
    status.textContent = "Stream unavailable";
    message.textContent = text;
    message.classList.add("is-visible");
  }

  function beginPlayback() {
    video.muted = false;
    video.defaultMuted = false;
    video.volume = 1;
    video.removeAttribute("muted");
    var attempt = video.play();
    if (attempt && typeof attempt.catch === "function") attempt.catch(function () {
      status.textContent = "Ready with sound · tap play to begin";
    });
  }

  function attachStream(url) {
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.addEventListener("loadedmetadata", beginPlayback, { once: true });
      return;
    }
    if (window.Hls && window.Hls.isSupported()) {
      activeHls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30
      });
      activeHls.loadSource(url);
      activeHls.attachMedia(video);
      activeHls.on(window.Hls.Events.MANIFEST_PARSED, beginPlayback);
      activeHls.on(window.Hls.Events.ERROR, function (_event, data) {
        if (!data || !data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          activeHls.startLoad();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          activeHls.recoverMediaError();
        } else {
          showError("The player could not open this shared stream. Please reload and try again.");
        }
      });
      return;
    }
    showError("This browser does not support HLS video playback.");
  }

  video.addEventListener("playing", function () {
    message.classList.remove("is-visible");
    status.textContent = video.muted ? "Playing muted · tap the speaker for sound" : "Playing with sound";
  });
  video.addEventListener("volumechange", function () {
    if (!video.paused) status.textContent = video.muted ? "Playing muted · tap the speaker for sound" : "Playing with sound";
  });

  fetch(endpoint, { cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" })
    .then(function (response) {
      if (!response.ok) throw new Error("Shared media access returned " + response.status);
      return response.json();
    })
    .then(function (data) {
      if (!data || !data.hls_url || !data.poster_url) throw new Error("Shared media response is incomplete");
      video.poster = data.poster_url;
      quality.textContent = (data.quality || "HLS") + " · phone-ready stream";
      attachStream(data.hls_url);
    })
    .catch(function () {
      showError("This private video is temporarily unavailable. Please try again shortly.");
    });

  window.addEventListener("pagehide", function () {
    if (activeHls) activeHls.destroy();
  }, { once: true });
}());
