/* Diagram review page: pan and zoom the drawing, highlight a block from a comment, save as PNG. */
(function () {
  "use strict";
  function init() {
    const box = document.querySelector("[data-diagram-view]");
    if (box) {
      const svg = box.querySelector("svg");
      const vb = svg.getAttribute("viewBox").split(" ").map(Number);
      let view = vb.slice();
      svg.removeAttribute("width"); svg.removeAttribute("height");
      const apply = () => svg.setAttribute("viewBox", view.map((v) => v.toFixed(2)).join(" "));
      box.addEventListener("wheel", (e) => {
        e.preventDefault();
        const r = svg.getBoundingClientRect(), f = Math.exp(e.deltaY * 0.0012);
        const px = view[0] + ((e.clientX - r.left) / r.width) * view[2], py = view[1] + ((e.clientY - r.top) / r.height) * view[3];
        view = [px - (px - view[0]) * f, py - (py - view[1]) * f, view[2] * f, view[3] * f];
        apply();
      }, { passive: false });
      let drag = null;
      box.addEventListener("pointerdown", (e) => { drag = [e.clientX, e.clientY]; box.setPointerCapture(e.pointerId); });
      box.addEventListener("pointermove", (e) => {
        if (!drag) return;
        const r = svg.getBoundingClientRect(), s = Math.max(view[2] / r.width, view[3] / r.height);
        view[0] -= (e.clientX - drag[0]) * s; view[1] -= (e.clientY - drag[1]) * s; drag = [e.clientX, e.clientY]; apply();
      });
      box.addEventListener("pointerup", () => (drag = null));
      box.addEventListener("dblclick", () => { view = vb.slice(); apply(); });
      document.querySelectorAll("[data-node-ref]").forEach((c) => c.addEventListener("click", (e) => {
        if (e.target.closest("form")) return;
        svg.querySelectorAll(".hl-box").forEach((x) => x.remove());
        const els = svg.querySelectorAll(`[data-node="${CSS.escape(c.dataset.nodeRef)}"]`);
        if (!els.length) return;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        els.forEach((el) => { const b = el.getBBox(); x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.width); y1 = Math.max(y1, b.y + b.height); });
        const hl = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        [["x", x0 - 6], ["y", y0 - 6], ["width", x1 - x0 + 12], ["height", y1 - y0 + 12], ["rx", 8], ["fill", "none"], ["stroke", "#f59e0b"], ["stroke-width", 3], ["stroke-dasharray", "6 3"]]
          .forEach(([k, v]) => hl.setAttribute(k, v));
        hl.classList.add("hl-box");
        svg.append(hl);
        box.scrollIntoView({ behavior: "smooth", block: "center" });
      }));
    }
    document.querySelectorAll("[data-png]").forEach((b) => b.addEventListener("click", () => svgToPng(b.dataset.png, b.dataset.name)));
  }

  function svgToPng(url, name) {
    fetch(url, { credentials: "same-origin" }).then((r) => r.text()).then((text) => {
      const img = new Image();
      const blob = new Blob([text], { type: "image/svg+xml" });
      const src = URL.createObjectURL(blob);
      img.onload = () => {
        const scale = 2, c = document.createElement("canvas");
        c.width = img.naturalWidth * scale; c.height = img.naturalHeight * scale;
        const ctx = c.getContext("2d"); ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
        ctx.drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(src);
        c.toBlob((png) => {
          const a = document.createElement("a"); a.href = URL.createObjectURL(png); a.download = name || "diagram.png";
          document.body.append(a); a.click(); a.remove();
        }, "image/png");
      };
      img.src = src;
    });
  }
  window.WorkbenchSvgToPng = svgToPng;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
