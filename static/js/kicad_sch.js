// Sources: the schematic preview. Loads the sheet's SVG, adds pan and zoom, shows the details of the
// part you click, and opens a sub-sheet when you click its box.
(function () {
  "use strict";
  const canvas = document.querySelector("[data-sch-canvas]");
  if (!canvas) return;
  const info = document.querySelector("[data-part-info]");
  const urlsEl = document.getElementById("sheet-urls");
  const sheetUrls = urlsEl ? JSON.parse(urlsEl.textContent) : {};

  fetch(canvas.dataset.src, { credentials: "same-origin" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.text(); })
    .then((text) => {
      const doc = new DOMParser().parseFromString(text, "image/svg+xml");
      const svg = doc.documentElement;
      if (!svg || svg.nodeName !== "svg") throw new Error("not svg");
      svg.querySelectorAll("script, foreignObject").forEach((n) => n.remove());
      canvas.textContent = "";
      canvas.appendChild(document.importNode(svg, true));
      setup(canvas.querySelector("svg"));
    })
    .catch(() => { canvas.textContent = "The drawing couldn't be loaded."; });

  function setup(svg) {
    const w = Number(svg.getAttribute("data-width")) || 297;
    const h = Number(svg.getAttribute("data-height")) || 210;
    const home = { x: 0, y: 0, w: w, h: h };
    let vb = Object.assign({}, home);
    const apply = () => svg.setAttribute("viewBox", [vb.x, vb.y, vb.w, vb.h].join(" "));
    function zoom(f, cx, cy) {
      cx = cx === undefined ? vb.x + vb.w / 2 : cx;
      cy = cy === undefined ? vb.y + vb.h / 2 : cy;
      const nw = Math.min(Math.max(vb.w * f, w / 200), w * 4);
      const k = nw / vb.w;
      vb = { x: cx - (cx - vb.x) * k, y: cy - (cy - vb.y) * k, w: nw, h: vb.h * k };
      apply();
    }
    function point(e) {
      const r = svg.getBoundingClientRect();
      return { x: vb.x + (e.clientX - r.left) / r.width * vb.w, y: vb.y + (e.clientY - r.top) / r.height * vb.h };
    }
    svg.addEventListener("wheel", (e) => { e.preventDefault(); const p = point(e); zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, p.x, p.y); }, { passive: false });
    let drag = null, moved = false;
    svg.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, vb: Object.assign({}, vb) }; moved = false; });
    window.addEventListener("pointerup", () => { drag = null; });
    window.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      const dx = (e.clientX - drag.x) / r.width * vb.w, dy = (e.clientY - drag.y) / r.height * vb.h;
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) moved = true;
      vb.x = drag.vb.x - dx; vb.y = drag.vb.y - dy; apply();
    });
    document.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.zoom === "fit") { vb = Object.assign({}, home); apply(); } else zoom(b.dataset.zoom === "in" ? 0.7 : 1 / 0.7);
    }));
    svg.addEventListener("click", (e) => {
      if (moved) return;
      const sheet = e.target.closest("g.sheet");
      if (sheet && sheetUrls[sheet.dataset.sheet]) { window.location.href = sheetUrls[sheet.dataset.sheet]; return; }
      const sym = e.target.closest("g.sym");
      svg.querySelectorAll("g.sym.selected").forEach((g) => g.classList.remove("selected"));
      document.querySelectorAll("[data-part-row].selected").forEach((r) => r.classList.remove("selected"));
      if (!sym || !info) return;
      sym.classList.add("selected");
      const row = document.querySelector('[data-part-row="' + CSS.escape(sym.dataset.ref || "") + '"]');
      if (row) row.classList.add("selected");
      info.textContent = "";
      [["Reference", sym.dataset.ref], ["Value", sym.dataset.value], ["Footprint", sym.dataset.footprint], ["Symbol", sym.dataset.lib]]
        .forEach(([k, v]) => {
          if (!v) return;
          const div = document.createElement("div"); div.className = "kv";
          const a = document.createElement("span"); a.textContent = k;
          const b = document.createElement("strong"); b.textContent = v;
          div.append(a, b); info.appendChild(div);
        });
    });
    apply();
  }
})();
