/* Forms marked data-once are sent only once: a double-click (or an impatient second
   click on a slow connection) must not receive a delivery or start a build twice.
   The server refuses repeats too; this just avoids the confusing second request. */
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-once")) return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    // app.js may cancel the submit (data-confirm); only lock forms that really go.
    setTimeout(function () {
      if (e.defaultPrevented) return;
      form.dataset.sent = "1";
      form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    }, 0);
  });
  // Coming back with the browser's back button shows the page from cache: unlock it.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-once]").forEach(function (f) {
      delete f.dataset.sent;
      f.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    });
  });
})();
