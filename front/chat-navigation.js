(() => {
  "use strict";
  document.querySelector("#back-button").addEventListener("click", () => {
    const cameFromThisSite = document.referrer.startsWith(window.location.origin);
    if (cameFromThisSite && window.history.length > 1) window.history.back();
    else window.location.assign("/");
  });
})();
