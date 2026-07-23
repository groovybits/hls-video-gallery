(() => {
  "use strict";

  const query = new URLSearchParams(window.location.search);
  if (query.has("video")) {
    window.location.replace(`index.html${window.location.search}${window.location.hash}`);
  }
})();
