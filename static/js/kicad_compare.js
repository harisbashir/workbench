// Sources: before/after KiCad drawings side by side, with "show only changes".
(function () {
  "use strict";
  const boxes = document.querySelectorAll("[data-compare-svg]");
  boxes.forEach((box) => {
    fetch(box.dataset.src, { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then((text) => {
        const svg = new DOMParser().parseFromString(text, "image/svg+xml").documentElement;
        if (!svg || svg.nodeName !== "svg") throw new Error("not svg");
        svg.querySelectorAll("script, foreignObject").forEach((n) => n.remove());
        // a PCB drawing has several views; show the top one
        const views = svg.querySelectorAll("[data-view]");
        views.forEach((g) => g.setAttribute("display", g.getAttribute("data-view") === "top" ? "inline" : "none"));
        box.textContent = "";
        box.appendChild(document.importNode(svg, true));
      })
      .catch(() => { box.textContent = "The drawing couldn't be loaded."; });
  });
  const toggle = document.querySelector("[data-only-changes]");
  if (toggle) toggle.addEventListener("change", () => boxes.forEach((b) => b.classList.toggle("only-changes", toggle.checked)));
})();
