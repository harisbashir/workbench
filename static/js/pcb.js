/* Board viewer: loads the SVG drawn from the Gerbers and adds zoom, pan,
   views, layer switches, colours, coordinates and measuring. No libraries. */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn(); else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    const canvas = document.querySelector("[data-pcb-canvas]");
    if (!canvas) return;
    document.querySelectorAll(".layer-dot[data-colour]").forEach((d) => { d.style.background = d.dataset.colour; });
    fetch(canvas.dataset.src, { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then((text) => {
        const doc = new DOMParser().parseFromString(text, "image/svg+xml");
        const root = doc.documentElement;
        if (root.nodeName !== "svg") throw new Error("not svg");
        const svg = document.importNode(root, true);
        svg.setAttribute("class", "pcb-svg");
        svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
        canvas.replaceChildren(svg);
        init(svg);
      })
      .catch(() => { canvas.innerHTML = '<div class="pcb-loading">The board couldn’t be drawn.</div>'; });
  });

  function init(svg) {
    const NS = "http://www.w3.org/2000/svg";
    const base = svg.viewBox.baseVal;
    const home = { x: base.x, y: base.y, w: base.width, h: base.height };
    let vb = Object.assign({}, home);
    const origin = (svg.getAttribute("data-origin") || "0 0").split(" ").map(Number);
    const boardW = Number(svg.getAttribute("data-board-width")) || 0;
    const coords = document.querySelector("[data-coords]");
    let view = "top";

    function apply() { svg.setAttribute("viewBox", vb.x + " " + vb.y + " " + vb.w + " " + vb.h); }

    function toUser(e) {
      const pt = svg.createSVGPoint();
      pt.x = e.clientX; pt.y = e.clientY;
      return pt.matrixTransform(svg.getScreenCTM().inverse());
    }

    function zoom(k, p) {
      const nw = Math.min(home.w * 3, Math.max(home.w / 400, vb.w * k));
      const kk = nw / vb.w;
      p = p || { x: vb.x + vb.w / 2, y: vb.y + vb.h / 2 };
      vb.x = p.x - (p.x - vb.x) * kk;
      vb.y = p.y - (p.y - vb.y) * kk;
      vb.w *= kk; vb.h *= kk;
      apply();
      scaleOverlay();
    }

    svg.addEventListener("wheel", (e) => { e.preventDefault(); zoom(Math.exp(e.deltaY * 0.0015), toUser(e)); }, { passive: false });
    document.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.zoom === "fit") { vb = Object.assign({}, home); apply(); scaleOverlay(); }
      else zoom(b.dataset.zoom === "in" ? 0.7 : 1 / 0.7);
    }));

    // Board coordinates in mm, from the board's lower-left corner.
    function boardPoint(p) {
      let x = p.x, y = -p.y;
      if (view === "bottom") x = 2 * origin[0] + boardW - x;
      return { x: x - origin[0], y: y - origin[1] };
    }

    // --- panning ---------------------------------------------------------------
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      if (measuring || e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y };
      svg.setPointerCapture(e.pointerId);
      svg.classList.add("grabbing");
    });
    svg.addEventListener("pointermove", (e) => {
      const p = toUser(e);
      const b = boardPoint(p);
      if (coords) coords.textContent = "X " + b.x.toFixed(2) + "  Y " + b.y.toFixed(2) + " mm";
      if (drag) {
        const s = Math.max(vb.w / svg.clientWidth, vb.h / svg.clientHeight);
        vb.x = drag.vx - (e.clientX - drag.x) * s;
        vb.y = drag.vy - (e.clientY - drag.y) * s;
        apply();
      }
      if (measuring && mA && !mDone) drawMeasure(mA, p);
    });
    ["pointerup", "pointercancel"].forEach((ev) => svg.addEventListener(ev, () => { drag = null; svg.classList.remove("grabbing"); }));
    svg.addEventListener("pointerleave", () => { if (coords) coords.textContent = ""; });

    // --- views -------------------------------------------------------------------
    const viewButtons = document.querySelectorAll("[data-set-view]");
    function setView(name) {
      view = name;
      svg.querySelectorAll("[data-view]").forEach((g) => g.setAttribute("display", g.dataset.view === name ? "inline" : "none"));
      viewButtons.forEach((b) => b.classList.toggle("active", b.dataset.setView === name));
      svg.classList.toggle("on-layers", name === "layers");
      clearMeasure();
    }
    viewButtons.forEach((b) => b.addEventListener("click", () => setView(b.dataset.setView)));

    document.querySelectorAll("[data-layer-toggle]").forEach((cb) => cb.addEventListener("change", () => {
      const el = svg.querySelector('[data-view="layers"] [data-layer="' + cb.dataset.layerToggle + '"]');
      if (el) el.setAttribute("display", cb.checked ? "inline" : "none");
      if (view !== "layers") setView("layers");
    }));

    // --- colours -------------------------------------------------------------------
    const colours = JSON.parse((document.getElementById("pcb-colours") || { textContent: "{}" }).textContent || "{}");
    document.querySelectorAll("[data-mask]").forEach((b) => b.addEventListener("click", () => {
      const c = (colours.masks || {})[b.dataset.mask];
      if (!c) return;
      svg.querySelectorAll('[data-paint="mask-base"]').forEach((el) => el.setAttribute("fill", c[0]));
      svg.querySelectorAll('[data-paint="mask-copper"]').forEach((el) => el.setAttribute("color", c[1]));
      svg.querySelectorAll('[data-paint="silk"]').forEach((el) => el.setAttribute("color", c[2]));
      document.querySelectorAll("[data-mask]").forEach((x) => x.classList.toggle("active", x === b));
      if (view === "layers") setView("top");
    }));
    document.querySelectorAll("[data-finish]").forEach((b) => b.addEventListener("click", () => {
      const c = (colours.finishes || {})[b.dataset.finish];
      if (!c) return;
      svg.querySelectorAll('[data-paint="finish"]').forEach((el) => el.setAttribute("color", c));
      document.querySelectorAll("[data-finish]").forEach((x) => x.classList.toggle("active", x === b));
      if (view === "layers") setView("top");
    }));

    // --- measuring -------------------------------------------------------------------
    let measuring = false, mA = null, mDone = false;
    const overlay = document.createElementNS(NS, "g");
    overlay.setAttribute("class", "pcb-measure");
    svg.appendChild(overlay);
    const mBtn = document.querySelector("[data-measure]");
    function clearMeasure() { overlay.replaceChildren(); mA = null; mDone = false; }
    function scaleOverlay() {
      const fs = vb.w / 55;
      overlay.querySelectorAll("text").forEach((t) => t.setAttribute("font-size", fs));
      overlay.querySelectorAll("circle").forEach((c) => c.setAttribute("r", vb.w / 250));
    }
    function drawMeasure(a, b) {
      overlay.replaceChildren();
      const line = document.createElementNS(NS, "line");
      [["x1", a.x], ["y1", a.y], ["x2", b.x], ["y2", b.y]].forEach(([k, v]) => line.setAttribute(k, v));
      overlay.appendChild(line);
      [a, b].forEach((p) => {
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("cx", p.x); c.setAttribute("cy", p.y);
        overlay.appendChild(c);
      });
      const dx = Math.abs(b.x - a.x), dy = Math.abs(b.y - a.y);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", (a.x + b.x) / 2);
      t.setAttribute("y", (a.y + b.y) / 2 - vb.w / 80);
      t.textContent = Math.hypot(dx, dy).toFixed(2) + " mm  (Δx " + dx.toFixed(2) + ", Δy " + dy.toFixed(2) + ")";
      overlay.appendChild(t);
      scaleOverlay();
    }
    if (mBtn) mBtn.addEventListener("click", () => {
      measuring = !measuring;
      mBtn.setAttribute("aria-pressed", measuring ? "true" : "false");
      mBtn.classList.toggle("btn-primary", measuring);
      svg.classList.toggle("measuring", measuring);
      clearMeasure();
    });
    svg.addEventListener("click", (e) => {
      if (!measuring) return;
      const p = toUser(e);
      if (!mA || mDone) { clearMeasure(); mA = p; drawMeasure(p, p); }
      else { drawMeasure(mA, p); mDone = true; }
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.closest("input, select, textarea")) return;
      if (e.key === "+" || e.key === "=") zoom(0.7);
      else if (e.key === "-") zoom(1 / 0.7);
      else if (e.key === "0") { vb = Object.assign({}, home); apply(); }
      else if (e.key === "Escape") clearMeasure();
    });
  }
})();
