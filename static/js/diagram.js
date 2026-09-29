/* Workbench block diagram editor.
 *
 * Drawing: drag shapes from the palette (or double-click the canvas), drag a block's round handles
 * onto another block to connect them, drag corners to resize. Select several with Shift or by
 * dragging a box. Scroll to zoom; drag with the middle/right button or hold Space to move around.
 * Keys: Delete, Ctrl+Z / Ctrl+Y, Ctrl+C / Ctrl+V, Ctrl+D duplicate, Ctrl+A, arrows to nudge, Ctrl+S save.
 */
(function () {
  "use strict";
  const root = document.querySelector("[data-diagram-editor]");
  if (!root) return;
  const G = window.DiagramGeometry;
  const STYLE = JSON.parse(document.getElementById("diagram-style").textContent);
  const META = JSON.parse(document.getElementById("diagram-meta").textContent);
  G.init(STYLE);
  const NS = "http://www.w3.org/2000/svg";
  const GRID = STYLE.grid;
  const csrf = () => (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";

  let doc = JSON.parse(document.getElementById("diagram-data").textContent);
  doc.nodes = doc.nodes || []; doc.edges = doc.edges || []; doc.page = doc.page || { legend: true };
  let savedVersion = META.version, dirty = false;
  let sel = new Set(), selEdge = null, hover = null, defaultKind = "signal";
  const undoStack = [], redoStack = [];
  let clipboard = null;

  const svg = root.querySelector("[data-canvas] svg");
  const layers = {};
  ["grid", "drawing", "hits", "overlay"].forEach((k) => (layers[k] = svg.querySelector(`[data-layer=${k}]`)));
  const canvasBox = root.querySelector("[data-canvas]");
  const panel = root.querySelector("[data-props]");
  const statusEl = root.querySelector("[data-save-status]");
  let view = { x: 0, y: 0, s: 1 };

  // ---------------------------------------------------------------- helpers
  const byId = () => { const m = {}; doc.nodes.forEach((n) => (m[n.id] = n)); return m; };
  const snap = (v) => Math.round(v / GRID) * GRID;
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.append(e); return e; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function newId(prefix) {
    let max = 0;
    (prefix === "e" ? doc.edges : doc.nodes).forEach((x) => { const m = /^[a-z]+(\d+)$/.exec(x.id); if (m) max = Math.max(max, +m[1]); });
    return prefix + (max + 1);
  }
  function toDiagram(clientX, clientY) {
    const r = svg.getBoundingClientRect();
    return [view.x + (clientX - r.left) / view.s, view.y + (clientY - r.top) / view.s];
  }

  // ---------------------------------------------------------------- history
  function pushHistory() {
    undoStack.push(JSON.stringify(doc));
    if (undoStack.length > 150) undoStack.shift();
    redoStack.length = 0;
  }
  function changed() {
    dirty = true;
    setStatus();
    try { localStorage.setItem(draftKey, JSON.stringify({ base: savedVersion, t: Date.now(), data: doc })); } catch (e) { /* storage full or blocked */ }
    render();
  }
  function undo() { if (!undoStack.length) return; redoStack.push(JSON.stringify(doc)); doc = JSON.parse(undoStack.pop()); pruneSelection(); changed(); buildPanel(); }
  function redo() { if (!redoStack.length) return; undoStack.push(JSON.stringify(doc)); doc = JSON.parse(redoStack.pop()); pruneSelection(); changed(); buildPanel(); }
  function pruneSelection() {
    const ids = new Set(doc.nodes.map((n) => n.id));
    sel = new Set([...sel].filter((id) => ids.has(id)));
    if (selEdge && !doc.edges.some((e) => e.id === selEdge)) selEdge = null;
  }

  // ---------------------------------------------------------------- rendering
  let lastPaths = {};
  function render() {
    const { prims, paths } = G.layout(doc);
    lastPaths = paths;
    let html = "";
    prims.forEach((p) => {
      const meta = p[p.length - 1] || {};
      const data = meta.node ? ` data-node="${esc(meta.node)}"` : meta.edge ? ` data-edge="${esc(meta.edge)}"` : "";
      if (p[0] === "path") {
        const [, d, fill, stroke, width, dash] = p;
        html += `<path d="${G.d(d)}" fill="${fill || "none"}"${stroke ? ` stroke="${stroke}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round"` : ""}${dash ? ` stroke-dasharray="${dash}"` : ""}${data}/>`;
      } else if (p[0] === "text") {
        const [, x, y, text, size, bold, color, anchor] = p;
        html += `<text x="${x.toFixed(2)}" y="${y.toFixed(2)}" font-size="${size}" fill="${color}" text-anchor="${anchor}"${bold ? ' font-weight="bold"' : ""}${data}>${esc(text)}</text>`;
      } else if (p[0] === "label_bg") {
        html += `<rect x="${p[1].toFixed(2)}" y="${p[2].toFixed(2)}" width="${p[3].toFixed(2)}" height="${p[4].toFixed(2)}" rx="3" fill="#fff" fill-opacity="0.92"${data}/>`;
      }
    });
    layers.drawing.innerHTML = html;
    // hit areas: frames first so blocks inside them stay clickable
    let hits = "";
    const ordered = doc.nodes.filter((n) => n.type === "frame").concat(doc.nodes.filter((n) => n.type !== "frame"));
    doc.edges.forEach((e) => {
      const pts = paths[e.id];
      if (pts) hits += `<path class="dg-hit-edge" d="M ${pts.map((p) => p.join(" ")).join(" L ")}" data-hit-edge="${esc(e.id)}"/>`;
    });
    ordered.forEach((n) => {
      if (n.type === "frame") {
        hits += `<path class="dg-hit-frame" d="M ${n.x} ${n.y} h ${n.w} v ${n.h} h ${-n.w} Z" data-hit-node="${esc(n.id)}"/>`;
        hits += `<rect class="dg-hit" x="${n.x}" y="${n.y}" width="${Math.min(n.w, 220)}" height="28" data-hit-node="${esc(n.id)}"/>`;
      } else hits += `<rect class="dg-hit" x="${n.x - 3}" y="${n.y - 3}" width="${n.w + 6}" height="${n.h + 6}" data-hit-node="${esc(n.id)}"/>`;
    });
    layers.hits.innerHTML = hits;
    renderOverlay();
    updateViewBox();
  }

  function renderOverlay() {
    const o = layers.overlay;
    o.textContent = "";
    const nodes = byId(), k = 1 / view.s;
    sel.forEach((id) => {
      const n = nodes[id]; if (!n) return;
      el("rect", { x: n.x - 4, y: n.y - 4, width: n.w + 8, height: n.h + 8, rx: 5, class: "dg-sel", "stroke-width": 1.5 * k, "stroke-dasharray": `${5 * k} ${3 * k}` }, o);
    });
    if (sel.size === 1) {
      const n = nodes[[...sel][0]];
      if (n) [["nw", n.x, n.y], ["ne", n.x + n.w, n.y], ["sw", n.x, n.y + n.h], ["se", n.x + n.w, n.y + n.h]].forEach(([h, x, y]) =>
        el("rect", { x: x - 4 * k, y: y - 4 * k, width: 8 * k, height: 8 * k, class: `dg-handle dg-${h}`, "data-handle": h, "stroke-width": 1.2 * k }, o));
    }
    if (selEdge && lastPaths[selEdge]) {
      el("path", { d: "M " + lastPaths[selEdge].map((p) => p.join(" ")).join(" L "), class: "dg-edge-sel", "stroke-width": 6 * k }, o);
    }
    const portNode = hover && nodes[hover] ? nodes[hover] : (sel.size === 1 ? nodes[[...sel][0]] : null);
    if (portNode && portNode.type !== "text" && !drag) {
      const n = portNode;
      [["top", n.x + n.w / 2, n.y - 12 * k], ["right", n.x + n.w + 12 * k, n.y + n.h / 2], ["bottom", n.x + n.w / 2, n.y + n.h + 12 * k], ["left", n.x - 12 * k, n.y + n.h / 2]]
        .forEach(([side, x, y]) => {
          const g = el("g", { class: "dg-port", "data-port": side, "data-port-node": n.id }, o);
          el("circle", { cx: x, cy: y, r: 6 * k, "stroke-width": 1.5 * k }, g);
          el("path", { d: `M ${x - 3 * k} ${y} H ${x + 3 * k} M ${x} ${y - 3 * k} V ${y + 3 * k}`, "stroke-width": 1.5 * k }, g);
        });
    }
    if (drag && drag.mode === "band") {
      const [x0, y0, x1, y1] = drag.band;
      el("rect", { x: Math.min(x0, x1), y: Math.min(y0, y1), width: Math.abs(x1 - x0), height: Math.abs(y1 - y0), class: "dg-band", "stroke-width": k }, o);
    }
    if (drag && drag.mode === "connect") {
      el("path", { d: `M ${drag.from[0]} ${drag.from[1]} L ${drag.to[0]} ${drag.to[1]}`, class: "dg-temp", "stroke-width": 2 * k, "stroke-dasharray": `${6 * k} ${4 * k}` }, o);
    }
  }

  function updateViewBox() {
    const w = canvasBox.clientWidth || 800, h = canvasBox.clientHeight || 600;
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${w / view.s} ${h / view.s}`);
    const bg = layers.grid.querySelector("rect");
    bg.setAttribute("x", view.x); bg.setAttribute("y", view.y); bg.setAttribute("width", w / view.s); bg.setAttribute("height", h / view.s);
    root.querySelector("[data-zoom-label]").textContent = Math.round(view.s * 100) + "%";
  }

  function fit() {
    const { prims } = G.layout(doc);
    const [x, y, w, h] = G.bounds(prims, 40);
    const cw = canvasBox.clientWidth || 800, ch = canvasBox.clientHeight || 600;
    view.s = Math.min(cw / w, ch / h, 1.5);
    view.x = x - (cw / view.s - w) / 2; view.y = y - (ch / view.s - h) / 2;
    render();
  }
  function zoomAt(f, cx, cy) {
    const ns = Math.min(Math.max(view.s * f, 0.15), 4);
    const [px, py] = cx == null ? [view.x + canvasBox.clientWidth / view.s / 2, view.y + canvasBox.clientHeight / view.s / 2] : toDiagram(cx, cy);
    view.x = px - (px - view.x) * (view.s / ns); view.y = py - (py - view.y) * (view.s / ns); view.s = ns;
    render();
  }

  // ---------------------------------------------------------------- editing operations
  function addNode(type, x, y, withHistory = true) {
    const s = STYLE.shapes[type] || STYLE.shapes.block;
    if (withHistory) pushHistory();
    const n = { id: newId("n"), type, x: snap(x - s.w / 2), y: snap(y - s.h / 2), w: s.w, h: s.h, label: type === "text" ? "Note" : s.label, sub: "" };
    if (type !== "frame") freeSpot(n);
    doc.nodes.push(n);
    sel = new Set([n.id]); selEdge = null;
    changed(); buildPanel(); focusLabel();
    return n;
  }
  // Nudge a new block to the nearest place where it doesn't cover another block.
  function freeSpot(n) {
    const others = doc.nodes.filter((m) => m.type !== "frame");
    const clash = (x, y) => others.some((m) => x < m.x + m.w + 10 && x + n.w + 10 > m.x && y < m.y + m.h + 10 && y + n.h + 10 > m.y);
    if (!clash(n.x, n.y)) return;
    for (let r = 1; r < 40; r++) {
      for (const [dx, dy] of [[1, 0], [0, 1], [-1, 0], [0, -1], [1, 1], [-1, 1], [1, -1], [-1, -1]]) {
        const x = snap(n.x + dx * r * 30), y = snap(n.y + dy * r * 30);
        if (!clash(x, y)) { n.x = x; n.y = y; return; }
      }
    }
  }
  function deleteSelection() {
    if (!sel.size && !selEdge) return;
    pushHistory();
    if (selEdge) doc.edges = doc.edges.filter((e) => e.id !== selEdge);
    if (sel.size) {
      doc.nodes = doc.nodes.filter((n) => !sel.has(n.id));
      doc.edges = doc.edges.filter((e) => !sel.has(e.from) && !sel.has(e.to));
    }
    sel.clear(); selEdge = null;
    changed(); buildPanel();
  }
  function copySelection() {
    if (!sel.size) return;
    const nodes = doc.nodes.filter((n) => sel.has(n.id));
    const edges = doc.edges.filter((e) => sel.has(e.from) && sel.has(e.to));
    clipboard = clone({ nodes, edges });
    try { localStorage.setItem("wb-diagram-clipboard", JSON.stringify(clipboard)); } catch (e) { /* ignore */ }
  }
  function paste(offset = 20) {
    let cb = clipboard;
    try { cb = cb || JSON.parse(localStorage.getItem("wb-diagram-clipboard") || "null"); } catch (e) { /* ignore */ }
    if (!cb || !cb.nodes.length) return;
    pushHistory();
    const map = {};
    const newSel = new Set();
    cb.nodes.forEach((n) => { const c = clone(n); c.id = newId("n"); map[n.id] = c.id; c.x += offset; c.y += offset; doc.nodes.push(c); newSel.add(c.id); });
    cb.edges.forEach((e) => { const c = clone(e); c.id = newId("e"); c.from = map[e.from]; c.to = map[e.to]; doc.edges.push(c); });
    clipboard = { nodes: cb.nodes.map((n) => ({ ...n, x: n.x + offset, y: n.y + offset })), edges: cb.edges };
    sel = newSel; selEdge = null;
    changed(); buildPanel();
  }
  function align(how) {
    const ns = doc.nodes.filter((n) => sel.has(n.id));
    if (ns.length < 2) return;
    pushHistory();
    const L = Math.min(...ns.map((n) => n.x)), R = Math.max(...ns.map((n) => n.x + n.w)), T = Math.min(...ns.map((n) => n.y)), B = Math.max(...ns.map((n) => n.y + n.h));
    if (how === "left") ns.forEach((n) => (n.x = L));
    if (how === "right") ns.forEach((n) => (n.x = R - n.w));
    if (how === "center") ns.forEach((n) => (n.x = snap((L + R) / 2 - n.w / 2)));
    if (how === "top") ns.forEach((n) => (n.y = T));
    if (how === "bottom") ns.forEach((n) => (n.y = B - n.h));
    if (how === "middle") ns.forEach((n) => (n.y = snap((T + B) / 2 - n.h / 2)));
    if (how === "hspace" && ns.length > 2) {
      ns.sort((a, b) => a.x - b.x);
      const gap = (R - L - ns.reduce((s, n) => s + n.w, 0)) / (ns.length - 1);
      let x = L; ns.forEach((n) => { n.x = snap(x); x += n.w + gap; });
    }
    if (how === "vspace" && ns.length > 2) {
      ns.sort((a, b) => a.y - b.y);
      const gap = (B - T - ns.reduce((s, n) => s + n.h, 0)) / (ns.length - 1);
      let y = T; ns.forEach((n) => { n.y = snap(y); y += n.h + gap; });
    }
    changed();
  }
  function order(front) {
    if (!sel.size) return;
    pushHistory();
    const picked = doc.nodes.filter((n) => sel.has(n.id)), rest = doc.nodes.filter((n) => !sel.has(n.id));
    doc.nodes = front ? rest.concat(picked) : picked.concat(rest);
    changed();
  }

  // ---------------------------------------------------------------- pointer interaction
  let drag = null, spaceDown = false;

  svg.addEventListener("pointerdown", (e) => {
    const t = e.target;
    const [x, y] = toDiagram(e.clientX, e.clientY);
    svg.setPointerCapture(e.pointerId);
    canvasBox.focus({ preventScroll: true });
    if (e.button === 1 || e.button === 2 || spaceDown) { drag = { mode: "pan", cx: e.clientX, cy: e.clientY, vx: view.x, vy: view.y }; return; }
    const port = t.closest("[data-port]");
    if (port) {
      const n = byId()[port.dataset.portNode];
      const c = { top: [n.x + n.w / 2, n.y], right: [n.x + n.w, n.y + n.h / 2], bottom: [n.x + n.w / 2, n.y + n.h], left: [n.x, n.y + n.h / 2] }[port.dataset.port];
      drag = { mode: "connect", node: n.id, side: port.dataset.port, from: c, to: [x, y] };
      return;
    }
    const handle = t.closest("[data-handle]");
    if (handle && sel.size === 1) {
      const n = byId()[[...sel][0]];
      pushHistory();
      drag = { mode: "resize", handle: handle.dataset.handle, node: n.id, start: { ...n }, x0: x, y0: y };
      return;
    }
    const hitN = t.closest("[data-hit-node]");
    if (hitN) {
      const id = hitN.dataset.hitNode;
      if (e.shiftKey || e.ctrlKey || e.metaKey) { sel.has(id) ? sel.delete(id) : sel.add(id); }
      else if (!sel.has(id)) sel = new Set([id]);
      selEdge = null;
      const nodes = byId(), moving = new Set(sel);
      // moving a frame carries the blocks inside it
      sel.forEach((sid) => {
        const f = nodes[sid];
        if (f && f.type === "frame") doc.nodes.forEach((m) => { if (m.id !== f.id && m.x >= f.x && m.y >= f.y && m.x + m.w <= f.x + f.w && m.y + m.h <= f.y + f.h) moving.add(m.id); });
      });
      drag = { mode: "move", x0: x, y0: y, ids: [...moving], start: [...moving].map((mid) => [nodes[mid].x, nodes[mid].y]), moved: false, lead: id };
      buildPanel(); renderOverlay();
      return;
    }
    const hitE = t.closest("[data-hit-edge]");
    if (hitE) { selEdge = hitE.dataset.hitEdge; sel.clear(); buildPanel(); renderOverlay(); drag = null; return; }
    if (!e.shiftKey) { sel.clear(); selEdge = null; buildPanel(); }
    drag = { mode: "band", band: [x, y, x, y], add: e.shiftKey };
    renderOverlay();
  });

  svg.addEventListener("pointermove", (e) => {
    const [x, y] = toDiagram(e.clientX, e.clientY);
    if (!drag) {
      const h = e.target.closest("[data-hit-node]"), p = e.target.closest("[data-port]");
      const nh = p ? p.dataset.portNode : h ? h.dataset.hitNode : null;
      if (nh !== hover) { hover = nh; renderOverlay(); }
      return;
    }
    if (drag.mode === "pan") {
      view.x = drag.vx - (e.clientX - drag.cx) / view.s; view.y = drag.vy - (e.clientY - drag.cy) / view.s; updateViewBox(); return;
    }
    if (drag.mode === "move") {
      const nodes = byId(), li = drag.ids.indexOf(drag.lead);
      const lx = drag.start[li][0] + (x - drag.x0), ly = drag.start[li][1] + (y - drag.y0);
      const dx = (e.altKey ? lx : snap(lx)) - drag.start[li][0], dy = (e.altKey ? ly : snap(ly)) - drag.start[li][1];
      if (!drag.moved && (Math.abs(dx) > 0 || Math.abs(dy) > 0)) { pushHistory(); drag.moved = true; }
      if (!drag.moved) return;
      drag.ids.forEach((id, i) => { nodes[id].x = drag.start[i][0] + dx; nodes[id].y = drag.start[i][1] + dy; });
      render(); return;
    }
    if (drag.mode === "resize") {
      const n = byId()[drag.node], s = drag.start, h = drag.handle, minW = n.type === "text" ? 30 : 40, minH = n.type === "text" ? 16 : 24;
      let x0 = s.x, y0 = s.y, x1 = s.x + s.w, y1 = s.y + s.h;
      const sx = e.altKey ? x : snap(x), sy = e.altKey ? y : snap(y);
      if (h.includes("w")) x0 = Math.min(sx, x1 - minW); if (h.includes("e")) x1 = Math.max(sx, x0 + minW);
      if (h.includes("n")) y0 = Math.min(sy, y1 - minH); if (h.includes("s")) y1 = Math.max(sy, y0 + minH);
      Object.assign(n, { x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
      render(); return;
    }
    if (drag.mode === "band") { drag.band[2] = x; drag.band[3] = y; renderOverlay(); return; }
    if (drag.mode === "connect") {
      drag.to = [x, y];
      const h = document.elementFromPoint(e.clientX, e.clientY);
      const target = h && h.closest && h.closest("[data-hit-node]");
      hover = target ? target.dataset.hitNode : null;
      renderOverlay();
    }
  });

  svg.addEventListener("pointerup", (e) => {
    if (!drag) return;
    const d = drag; drag = null;
    if (d.mode === "move" && d.moved) changed();
    else if (d.mode === "resize") { changed(); buildPanel(); }
    else if (d.mode === "band") {
      const [x0, y0, x1, y1] = d.band, L = Math.min(x0, x1), R = Math.max(x0, x1), T = Math.min(y0, y1), B = Math.max(y0, y1);
      if (R - L > 3 || B - T > 3) doc.nodes.forEach((n) => { if (n.x < R && n.x + n.w > L && n.y < B && n.y + n.h > T && !(n.type === "frame" && !(n.x >= L && n.x + n.w <= R && n.y >= T && n.y + n.h <= B))) sel.add(n.id); });
      buildPanel(); renderOverlay();
    } else if (d.mode === "connect") {
      const h = document.elementFromPoint(e.clientX, e.clientY);
      const target = h && h.closest && h.closest("[data-hit-node]");
      if (target && target.dataset.hitNode !== d.node) {
        pushHistory();
        const ed = { id: newId("e"), from: d.node, to: target.dataset.hitNode, kind: defaultKind, label: "", arrow: "end", route: "ortho" };
        doc.edges.push(ed);
        selEdge = ed.id; sel.clear(); changed(); buildPanel();
      } else if (!target) {  // dropped on empty canvas: make a new block there, connected
        pushHistory();  // one undo step for the new block and its connection
        const n = addNode("block", d.to[0], d.to[1], false);
        doc.edges.push({ id: newId("e"), from: d.node, to: n.id, kind: defaultKind, label: "", arrow: "end", route: "ortho" });
        changed();
      } else renderOverlay();
    } else renderOverlay();
  });

  svg.addEventListener("dblclick", (e) => {
    const hitN = e.target.closest("[data-hit-node]");
    if (hitN) { sel = new Set([hitN.dataset.hitNode]); selEdge = null; buildPanel(); focusLabel(); renderOverlay(); return; }
    const hitE = e.target.closest("[data-hit-edge]");
    if (hitE) { selEdge = hitE.dataset.hitEdge; sel.clear(); buildPanel(); const f = panel.querySelector("[name=label]"); if (f) f.focus(); renderOverlay(); return; }
    const [x, y] = toDiagram(e.clientX, e.clientY);
    addNode("block", x, y);
  });
  svg.addEventListener("contextmenu", (e) => e.preventDefault());
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (e.shiftKey) { view.x += e.deltaY / view.s; updateViewBox(); return; }
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  svg.addEventListener("pointerleave", () => { if (!drag && hover) { hover = null; renderOverlay(); } });

  // palette: click to add in the middle, or drag onto the canvas
  root.querySelectorAll("[data-shape]").forEach((b) => {
    b.addEventListener("click", () => {
      const w = canvasBox.clientWidth / view.s, h = canvasBox.clientHeight / view.s;
      addNode(b.dataset.shape, view.x + w / 2 + (Math.random() - 0.5) * 60, view.y + h / 2 + (Math.random() - 0.5) * 40);
    });
    b.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/x-wb-shape", b.dataset.shape); e.dataTransfer.effectAllowed = "copy"; });
  });
  canvasBox.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("text/x-wb-shape")) e.preventDefault(); });
  canvasBox.addEventListener("drop", (e) => {
    const t = e.dataTransfer.getData("text/x-wb-shape");
    if (!t) return;
    e.preventDefault();
    const [x, y] = toDiagram(e.clientX, e.clientY);
    addNode(t, x, y);
  });
  root.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => {
    defaultKind = b.dataset.kind;
    root.querySelectorAll("[data-kind]").forEach((x) => x.classList.toggle("active", x === b));
    if (selEdge) { const ed = doc.edges.find((x) => x.id === selEdge); if (ed) { pushHistory(); ed.kind = defaultKind; changed(); buildPanel(); } }
  }));

  // ---------------------------------------------------------------- keyboard
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
    if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
    if (e.key === " ") { spaceDown = true; canvasBox.classList.add("panning"); e.preventDefault(); return; }
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelection(); return; }
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
    if (mod && e.key.toLowerCase() === "c") { copySelection(); return; }
    if (mod && e.key.toLowerCase() === "v") { e.preventDefault(); paste(); return; }
    if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); copySelection(); paste(); return; }
    if (mod && e.key.toLowerCase() === "a") { e.preventDefault(); sel = new Set(doc.nodes.map((n) => n.id)); selEdge = null; buildPanel(); renderOverlay(); return; }
    if (e.key === "Escape") { sel.clear(); selEdge = null; buildPanel(); renderOverlay(); return; }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[e.key] && sel.size) {
      e.preventDefault();
      const step = e.altKey ? 1 : GRID * (e.shiftKey ? 5 : 1);
      pushHistory();
      doc.nodes.forEach((n) => { if (sel.has(n.id)) { n.x += arrows[e.key][0] * step; n.y += arrows[e.key][1] * step; } });
      changed();
    }
  });
  document.addEventListener("keyup", (e) => { if (e.key === " ") { spaceDown = false; canvasBox.classList.remove("panning"); } });

  // ---------------------------------------------------------------- properties panel
  function field(label, input) { const l = document.createElement("label"); l.className = "dg-field"; const s = document.createElement("span"); s.textContent = label; l.append(s, input); return l; }
  function input(name, value, opts = {}) {
    const i = document.createElement(opts.multiline ? "textarea" : "input");
    if (!opts.multiline) i.type = opts.type || "text";
    if (opts.multiline) i.rows = opts.rows || 2;
    i.name = name; i.value = value == null ? "" : value;
    if (opts.min != null) i.min = opts.min;
    return i;
  }
  function select(name, value, choices) {
    const s = document.createElement("select"); s.name = name;
    choices.forEach(([v, l]) => { const o = document.createElement("option"); o.value = v; o.textContent = l; if (String(v) === String(value)) o.selected = true; s.append(o); });
    return s;
  }
  function bindEdit(elm, apply) {
    let pushed = false;
    elm.addEventListener("focus", () => (pushed = false));
    const handler = () => { if (!pushed) { pushHistory(); pushed = true; } apply(elm.value); changed(); };
    elm.addEventListener(elm.tagName === "SELECT" ? "change" : "input", handler);
  }
  function swatches(current, onPick) {
    const wrap = document.createElement("div"); wrap.className = "dg-swatches";
    Object.entries(STYLE.colors).forEach(([name, c]) => {
      const b = document.createElement("button"); b.type = "button"; b.className = "dg-swatch" + (name === current ? " active" : "");
      b.title = name; b.setAttribute("aria-label", "Colour " + name);
      b.style.setProperty("--fill", c.fill); b.style.setProperty("--stroke", c.stroke);
      b.addEventListener("click", () => { pushHistory(); onPick(name); changed(); buildPanel(); });
      wrap.append(b);
    });
    return wrap;
  }
  function button(text, fn, cls) { const b = document.createElement("button"); b.type = "button"; b.className = "btn btn-sm " + (cls || ""); b.textContent = text; b.addEventListener("click", fn); return b; }
  function heading(t) { const h = document.createElement("h3"); h.textContent = t; return h; }

  function buildPanel() {
    panel.textContent = "";
    const nodes = byId();
    if (sel.size === 1) {
      const n = nodes[[...sel][0]];
      if (!n) return;
      panel.append(heading(STYLE.shapes[n.type] ? STYLE.shapes[n.type].label : "Block"));
      const lab = input("label", n.label, { multiline: true }); bindEdit(lab, (v) => (n.label = v)); panel.append(field(n.type === "frame" ? "Title" : "Name", lab));
      if (!["frame", "text"].includes(n.type)) { const sub = input("sub", n.sub, { multiline: true }); bindEdit(sub, (v) => (n.sub = v)); panel.append(field("Detail (part number, value, rating…)", sub)); }
      const shape = select("type", n.type, Object.entries(STYLE.shapes).map(([k, s]) => [k, s.label])); bindEdit(shape, (v) => (n.type = v)); panel.append(field("Shape", shape));
      if (n.type === "board" && META.boards.length) {
        const b = select("ref", n.ref || "", [["", "— not linked —"]].concat(META.boards.map((x) => [x.id, x.name])));
        bindEdit(b, (v) => { n.ref = v; const bd = META.boards.find((x) => String(x.id) === v); if (bd && (!n.label || n.label === "Board")) n.label = bd.name; });
        panel.append(field("Links to board", b));
        const bd = META.boards.find((x) => String(x.id) === String(n.ref));
        if (bd) { const a = document.createElement("a"); a.href = bd.url; a.textContent = "Open " + bd.name + " →"; a.className = "small"; panel.append(a); }
      }
      if (n.type !== "text") { const f = field("Colour", swatches(n.color || (STYLE.shapes[n.type] || {}).color, (c) => (n.color = c))); panel.append(f); }
      const row = document.createElement("div"); row.className = "dg-row";
      const w = input("w", n.w, { type: "number", min: 20 }); bindEdit(w, (v) => (n.w = Math.max(20, +v || 20)));
      const h = input("h", n.h, { type: "number", min: 16 }); bindEdit(h, (v) => (n.h = Math.max(16, +v || 16)));
      row.append(field("Width", w), field("Height", h)); panel.append(row);
      const notes = input("notes", n.notes, { multiline: true, rows: 3 }); bindEdit(notes, (v) => (n.notes = v)); panel.append(field("Notes (not drawn)", notes));
      const acts = document.createElement("div"); acts.className = "btn-row";
      acts.append(button("Duplicate", () => { copySelection(); paste(); }), button("To front", () => order(true)), button("To back", () => order(false)), button("Delete", deleteSelection, "btn-danger"));
      panel.append(acts);
    } else if (sel.size > 1) {
      panel.append(heading(`${sel.size} blocks selected`));
      const al = document.createElement("div"); al.className = "dg-align";
      [["left", "Align left"], ["center", "Align centres"], ["right", "Align right"], ["top", "Align tops"], ["middle", "Align middles"], ["bottom", "Align bottoms"], ["hspace", "Space evenly across"], ["vspace", "Space evenly down"]]
        .forEach(([k, t]) => al.append(button(t, () => align(k))));
      panel.append(al);
      panel.append(field("Colour", swatches(null, (c) => doc.nodes.forEach((n) => { if (sel.has(n.id)) n.color = c; }))));
      const acts = document.createElement("div"); acts.className = "btn-row";
      acts.append(button("Duplicate", () => { copySelection(); paste(); }), button("Delete", deleteSelection, "btn-danger"));
      panel.append(acts);
    } else if (selEdge) {
      const e = doc.edges.find((x) => x.id === selEdge);
      if (!e) return;
      panel.append(heading(`Connection: ${(nodes[e.from] || {}).label || "?"} → ${(nodes[e.to] || {}).label || "?"}`));
      const lab = input("label", e.label, { multiline: true }); bindEdit(lab, (v) => (e.label = v)); panel.append(field("Label (bus, signal or rail, e.g. I²C 0x48, 3V3 500 mA)", lab));
      const kind = select("kind", e.kind, Object.entries(STYLE.edges).map(([k, s]) => [k, s.label])); bindEdit(kind, (v) => (e.kind = v)); panel.append(field("Type", kind));
      const arr = select("arrow", e.arrow, [["end", "Arrow at the end →"], ["start", "Arrow at the start ←"], ["both", "Both ends ↔"], ["none", "No arrows"]]); bindEdit(arr, (v) => (e.arrow = v)); panel.append(field("Direction", arr));
      const rt = select("route", e.route, [["ortho", "Right angles"], ["straight", "Straight line"]]); bindEdit(rt, (v) => (e.route = v)); panel.append(field("Route", rt));
      const sides = [["", "Automatic"], ["top", "Top"], ["right", "Right"], ["bottom", "Bottom"], ["left", "Left"]];
      const row = document.createElement("div"); row.className = "dg-row";
      const fs = select("fromSide", e.fromSide || "", sides); bindEdit(fs, (v) => { if (v) e.fromSide = v; else delete e.fromSide; });
      const ts = select("toSide", e.toSide || "", sides); bindEdit(ts, (v) => { if (v) e.toSide = v; else delete e.toSide; });
      row.append(field("Leaves from", fs), field("Arrives at", ts)); panel.append(row);
      const acts = document.createElement("div"); acts.className = "btn-row";
      acts.append(button("Reverse", () => { pushHistory(); [e.from, e.to] = [e.to, e.from]; [e.fromSide, e.toSide] = [e.toSide, e.fromSide]; if (!e.fromSide) delete e.fromSide; if (!e.toSide) delete e.toSide; changed(); buildPanel(); }),
        button("Delete", deleteSelection, "btn-danger"));
      panel.append(acts);
    } else {
      panel.append(heading("Diagram"));
      const p = document.createElement("p"); p.className = "small muted";
      p.textContent = `${doc.nodes.length} blocks, ${doc.edges.length} connections.`;
      panel.append(p);
      const lg = document.createElement("label"); lg.className = "dg-check";
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = doc.page.legend !== false;
      cb.addEventListener("change", () => { pushHistory(); doc.page.legend = cb.checked; changed(); });
      lg.append(cb, document.createTextNode(" Show a legend of connection types in exports"));
      panel.append(lg);
      const tips = document.createElement("div"); tips.className = "dg-tips small";
      tips.innerHTML = "<strong>How to draw</strong><ul>" +
        "<li>Drag a shape from the left, or double-click the canvas.</li>" +
        "<li>Hover a block and drag one of its <b>+</b> handles onto another block to connect them. Drop on empty space to add a new block.</li>" +
        "<li>Pick the connection type on the left before connecting (power, bus, analog…).</li>" +
        "<li>Shift-click or drag a box to select several; align them here.</li>" +
        "<li>Scroll to zoom. Drag with the right mouse button or hold Space to move around.</li>" +
        "<li><kbd>Del</kbd> delete · <kbd>Ctrl</kbd>+<kbd>Z</kbd> undo · <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicate · arrows nudge · <kbd>Alt</kbd> turns off the grid snap · <kbd>Ctrl</kbd>+<kbd>S</kbd> save</li></ul>";
      panel.append(tips);
    }
  }
  function focusLabel() { const f = panel.querySelector("[name=label]"); if (f) { f.focus(); f.select(); } }

  // ---------------------------------------------------------------- save, drafts and exports
  const draftKey = `wb-diagram-${META.id}`;
  function setStatus(text) {
    statusEl.textContent = text || (dirty ? `v${savedVersion} · unsaved changes` : `v${savedVersion} · saved`);
    statusEl.classList.toggle("warn-text", dirty && !text);
  }
  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body) });
  }
  function save(force) {
    const noteEl = root.querySelector("[data-save-note]");
    setStatus("Saving…");
    post(META.saveUrl, { data: doc, note: noteEl ? noteEl.value : "", base: savedVersion, force: !!force }).then(async (r) => {
      const res = await r.json().catch(() => ({}));
      if (r.status === 409 && res.conflict) {
        if (window.confirm(res.error + " Save your drawing as a new version anyway? (Theirs stays in the version history.)")) return save(true);
        setStatus("Not saved"); return;
      }
      if (!r.ok || !res.ok) { setStatus("Couldn't save: " + (res.error || r.status)); return; }
      savedVersion = res.version; dirty = false;
      if (noteEl) noteEl.value = "";
      try { localStorage.removeItem(draftKey); } catch (e) { /* ignore */ }
      setStatus(res.unchanged ? `v${savedVersion} · no changes to save` : `v${savedVersion} · saved` + (res.reopened ? " (new draft after approval)" : ""));
    }).catch(() => setStatus("Couldn't save — check your connection"));
  }
  function download(blob, name) {
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  const fname = (ext) => (META.name || "diagram").replace(/[^\w.-]+/g, "-").replace(/-+/g, "-") + "." + ext;
  function standaloneSvg() {
    const { prims } = G.layout(doc);
    const [x, y, w, h] = G.bounds(prims);
    return `<svg xmlns="${NS}" viewBox="${x} ${y} ${w} ${h}" width="${Math.round(w)}" height="${Math.round(h)}" font-family="Helvetica, Arial, sans-serif"><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="#fff"/>${layers.drawing.innerHTML}</svg>`;
  }
  function exportPng() {
    const text = standaloneSvg(), img = new Image(), src = URL.createObjectURL(new Blob([text], { type: "image/svg+xml" }));
    img.onload = () => {
      const c = document.createElement("canvas"), s = 2; c.width = img.naturalWidth * s; c.height = img.naturalHeight * s;
      const ctx = c.getContext("2d"); ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height); ctx.drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(src); c.toBlob((b) => download(b, fname("png")), "image/png");
    };
    img.src = src;
  }
  function exportPdf(page) {
    post(META.pdfUrl, { data: doc, page }).then((r) => r.ok ? r.blob() : Promise.reject(r.status)).then((b) => download(b, fname("pdf"))).catch((s) => setStatus("PDF failed (" + s + ")"));
  }

  const actions = {
    save: () => save(), undo, redo, fit, "zoom-in": () => zoomAt(1.25), "zoom-out": () => zoomAt(0.8),
    png: exportPng, svg: () => download(new Blob([standaloneSvg()], { type: "image/svg+xml" }), fname("svg")),
    pdf: () => exportPdf("A4"), pdf3: () => exportPdf("A3"),
    "align-left": () => align("left"), "align-center": () => align("center"), "align-top": () => align("top"), "align-middle": () => align("middle"),
    delete: deleteSelection,
  };
  root.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => { const a = actions[b.dataset.action]; if (a) a(); const m = b.closest("details"); if (m) m.open = false; }));
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  root.querySelectorAll("[data-leave]").forEach((a) => a.addEventListener("click", (e) => { if (dirty && !window.confirm("Leave without saving your changes?")) e.preventDefault(); else dirty = false; }));

  // palette previews: draw each shape small
  root.querySelectorAll("[data-shape]").forEach((b) => {
    const t = b.dataset.shape, s = STYLE.shapes[t];
    const n = { id: "", type: t, x: 4, y: t === "battery" ? 9 : 4, w: 48, h: t === "frame" ? 30 : 26, label: "", sub: "" };
    const ic = el("svg", { viewBox: "0 0 56 36", width: 56, height: 36, "aria-hidden": "true" });
    if (t === "text") el("text", { x: 28, y: 23, "font-size": 13, "text-anchor": "middle", fill: "#374151", "font-weight": "bold" }, ic).textContent = "Aa";
    else G.nodePrims(n).forEach((p) => { if (p[0] === "path") el("path", { d: G.d(p[1]), fill: p[2] || "none", stroke: p[3] || "none", "stroke-width": p[4] ? Math.min(p[4], 1.6) : 0, "stroke-dasharray": p[5] || "" }, ic); });
    b.prepend(ic);
    b.title = s.hint;
  });
  root.querySelectorAll("[data-kind]").forEach((b) => {
    const k = STYLE.edges[b.dataset.kind];
    const ic = el("svg", { viewBox: "0 0 34 10", width: 34, height: 10, "aria-hidden": "true" });
    el("path", { d: "M 1 5 H 33", stroke: k.color, "stroke-width": k.width, "stroke-dasharray": k.dash || "" }, ic);
    b.prepend(ic);
  });

  // ---------------------------------------------------------------- start
  new ResizeObserver(() => updateViewBox()).observe(canvasBox);
  buildPanel();
  setStatus();
  fit();
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey) || "null");
    if (draft && draft.base === savedVersion && JSON.stringify(draft.data) !== JSON.stringify(doc)) {
      const bar = root.querySelector("[data-draft]");
      bar.hidden = false;
      bar.querySelector("[data-when]").textContent = new Date(draft.t).toLocaleString();
      bar.querySelector("[data-restore]").addEventListener("click", () => { pushHistory(); doc = draft.data; changed(); buildPanel(); fit(); bar.hidden = true; });
      bar.querySelector("[data-discard]").addEventListener("click", () => { localStorage.removeItem(draftKey); bar.hidden = true; });
    } else if (draft) localStorage.removeItem(draftKey);
  } catch (e) { /* storage unavailable */ }
})();
