/* Block diagram layout — a line-by-line port of apps/diagrams/geometry.py.
 * Both must produce the same drawing, so an export matches what was drawn in the editor.
 * Exposes window.DiagramGeometry = { layout, bounds, textWidth, wrap, ... }.
 */
(function (global) {
  "use strict";
  const KAPPA = 0.5522847498;
  let S = null; // style, set by init()

  const init = (style) => { S = style; };

  function textWidth(s, size, bold) {
    const table = bold ? S.widths_bold : S.widths;
    let total = 0;
    for (const ch of s) {
      const o = ch.codePointAt(0);
      total += o >= 32 && o <= 126 ? table[o - 32] : 556;
    }
    return (total * size) / 1000;
  }

  const units = (ch, table) => { const o = ch.codePointAt(0); return o >= 32 && o <= 126 ? table[o - 32] : 556; };

  // Greedy word wrap, linear in the text length; stops after maxLines. Same as geometry.wrap().
  function wrap(text, width, size, bold, maxLines) {
    const table = bold ? S.widths_bold : S.widths, fits = (u) => (u * size) / 1000 <= width, space = units(" ", table);
    const lines = [];
    const full = () => maxLines != null && lines.length >= maxLines;
    const done = () => { while (lines.length > 1 && lines[lines.length - 1] === "") lines.pop(); return lines; };
    for (const para of (text || "").split("\n")) {
      let line = "", lu = 0;
      for (const word of para.split(" ")) {
        const w = [...word];
        let wu = 0;
        for (const ch of w) wu += units(ch, table);
        const cu = !line ? wu : lu + space + wu;
        if (fits(cu)) { line = !line ? word : line + " " + word; lu = cu; continue; }
        if (line) { lines.push(line); if (full()) return done(); }
        line = ""; lu = 0;
        let start = 0;
        while (!fits(wu) && w.length - start > 1) {
          let acc = 0, k = start;
          while (k < w.length) { const u = units(w[k], table); if (!fits(acc + u)) break; acc += u; k++; }
          if (k === start) { acc = units(w[k], table); k++; }
          lines.push(w.slice(start, k).join(""));
          if (full()) return done();
          start = k; wu -= acc;
        }
        line = w.slice(start).join(""); lu = wu;
      }
      lines.push(line);
      if (full()) return done();
    }
    return done();
  }

  function rectPath(x, y, w, h, r) {
    r = Math.max(0, Math.min(r || 0, w / 2, h / 2));
    if (r === 0) return [["M", x, y], ["L", x + w, y], ["L", x + w, y + h], ["L", x, y + h], ["Z"]];
    const k = r * KAPPA;
    return [["M", x + r, y], ["L", x + w - r, y], ["C", x + w - r + k, y, x + w, y + r - k, x + w, y + r],
      ["L", x + w, y + h - r], ["C", x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h],
      ["L", x + r, y + h], ["C", x + r - k, y + h, x, y + h - r + k, x, y + h - r],
      ["L", x, y + r], ["C", x, y + r - k, x + r - k, y, x + r, y], ["Z"]];
  }

  function ellipsePath(cx, cy, rx, ry) {
    const kx = rx * KAPPA, ky = ry * KAPPA;
    return [["M", cx + rx, cy], ["C", cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry],
      ["C", cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy], ["C", cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry],
      ["C", cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy], ["Z"]];
  }

  function polyPath(points, closed = true) {
    const d = [["M", points[0][0], points[0][1]]].concat(points.slice(1).map((p) => ["L", p[0], p[1]]));
    return closed ? d.concat([["Z"]]) : d;
  }

  function nodeColor(n) {
    const shape = S.shapes[n.type] || S.shapes.block;
    return S.colors[n.color || shape.color] || S.colors.gray;
  }

  function textInset(n) {
    const t = n.type;
    return { mcu: 18, diamond: n.w * 0.22, ellipse: n.w * 0.14, connector: 14 }[t] ?? 8;
  }

  function nodePrims(n) {
    const t = n.type || "block";
    const x = +n.x, y = +n.y, w = +n.w, h = +n.h;
    const c = nodeColor(n), meta = { node: n.id || "" }, out = [];
    const P = (d, fill, stroke, width, dash) => out.push(["path", d, fill, stroke, width, dash || "", meta]);
    if (t === "frame") P(rectPath(x, y, w, h, 6), (n.color == null || n.color === "gray") ? "#fafbfc" : c.fill, c.stroke, 1.4, "7 4");
    else if (t === "text") { /* no outline */ }
    else if (t === "mcu") {
      P(rectPath(x, y, w, h, 3), c.fill, c.stroke, 1.8);
      const pins = Math.max(2, Math.floor(h / 18));
      for (let i = 0; i < pins; i++) {
        const py = y + (h * (i + 1)) / (pins + 1);
        P([["M", x - 6, py], ["L", x, py]], null, c.stroke, 1.6);
        P([["M", x + w, py], ["L", x + w + 6, py]], null, c.stroke, 1.6);
      }
      const cx = x + w / 2;
      P([["M", cx - 7, y], ["C", cx - 7, y + 7 * 1.3, cx + 7, y + 7 * 1.3, cx + 7, y]], null, c.stroke, 1.2);
    } else if (t === "power") {
      P(rectPath(x, y, w, h, 8), c.fill, c.stroke, 1.6);
      P(polyPath([[x + 10, y + 6], [x + 6, y + 15], [x + 10, y + 15], [x + 8, y + 22], [x + 15, y + 11], [x + 11, y + 11], [x + 14, y + 6]]), c.stroke, null, 0);
    } else if (t === "connector") {
      const n1 = y + h * 0.3, n2 = y + h * 0.7;
      P(polyPath([[x, y], [x + w - 9, y], [x + w - 9, n1], [x + w, n1], [x + w, n2], [x + w - 9, n2], [x + w - 9, y + h], [x, y + h]]), c.fill, c.stroke, 1.6);
    } else if (t === "sensor") P(rectPath(x, y, w, h, Math.min(h / 2, 18)), c.fill, c.stroke, 1.6);
    else if (t === "ellipse") P(ellipsePath(x + w / 2, y + h / 2, w / 2, h / 2), c.fill, c.stroke, 1.6);
    else if (t === "diamond") P(polyPath([[x + w / 2, y], [x + w, y + h / 2], [x + w / 2, y + h], [x, y + h / 2]]), c.fill, c.stroke, 1.6);
    else if (t === "memory") {
      const ry = Math.min(8, h / 6), k = (w / 2) * KAPPA;
      P([["M", x, y + ry], ["L", x, y + h - ry],
        ["C", x, y + h - ry + ry * KAPPA, x + w / 2 - k, y + h, x + w / 2, y + h],
        ["C", x + w / 2 + k, y + h, x + w, y + h - ry + ry * KAPPA, x + w, y + h - ry],
        ["L", x + w, y + ry], ["C", x + w, y + ry - ry * KAPPA, x + w / 2 + k, y, x + w / 2, y],
        ["C", x + w / 2 - k, y, x, y + ry - ry * KAPPA, x, y + ry], ["Z"]], c.fill, c.stroke, 1.6);
      P([["M", x, y + ry], ["C", x, y + ry + ry * KAPPA, x + w / 2 - k, y + 2 * ry, x + w / 2, y + 2 * ry],
        ["C", x + w / 2 + k, y + 2 * ry, x + w, y + ry + ry * KAPPA, x + w, y + ry]], null, c.stroke, 1.6);
    } else if (t === "battery") {
      const nub = w * 0.3;
      P(rectPath(x + (w - nub) / 2, y - 5, nub, 6, 1.5), c.stroke, null, 0);
      P(rectPath(x, y, w, h, 4), c.fill, c.stroke, 1.6);
    } else if (t === "board") {
      P(rectPath(x, y, w, h, 4), c.fill, c.stroke, 2.6);
      [[x + 8, y + 8], [x + w - 8, y + 8], [x + 8, y + h - 8], [x + w - 8, y + h - 8]].forEach(([hx, hy]) => P(ellipsePath(hx, hy, 2.6, 2.6), "#ffffff", c.stroke, 1.2));
    } else P(rectPath(x, y, w, h, 4), c.fill, c.stroke, 1.6);
    return out.concat(nodeText(n, c));
  }

  function nodeText(n, c) {
    c = c || nodeColor(n);
    const t = n.type || "block", F = S.font;
    const x = +n.x, y = +n.y, w = +n.w, h = +n.h;
    const label = (n.label || "").trim(), sub = (n.sub || "").trim(), meta = { node: n.id || "" }, out = [];
    if (t === "frame") {
      if (label) out.push(["text", x + 10, y + 10 + F.frame, label, F.frame, true, c.stroke, "start", meta]);
      return out;
    }
    const width = Math.max(w - 2 * textInset(n), 20), ls = F.label, ss = F.sub, lh = F.line;
    const ll = label ? wrap(label, width, ls, true, Math.max(1, Math.floor(h / (ls * lh)))) : [];
    const room = h - ll.length * ls * lh - (ll.length ? 3 : 0);
    const sl = sub ? wrap(sub, width, ss, false, Math.max(1, Math.floor(room / (ss * lh)))) : [];
    const total = ll.length * ls * lh + (sl.length ? sl.length * ss * lh + (ll.length ? 3 : 0) : 0);
    const cy = y + h / 2 + (t === "power" && !sl.length ? 4 : 0);
    const color = t !== "text" ? c.text : "#111827", subc = n.color === "dark" ? "#d1d5db" : "#4b5563";
    let yy = cy - total / 2;
    ll.forEach((line) => { yy += ls * lh; out.push(["text", x + w / 2, yy - ls * (lh - 1) - 1.5, line, ls, true, color, "middle", meta]); });
    if (sl.length) {
      yy += ll.length ? 3 : 0;
      sl.forEach((line) => { yy += ss * lh; out.push(["text", x + w / 2, yy - ss * (lh - 1) - 1.2, line, ss, false, subc, "middle", meta]); });
    }
    return out;
  }

  const SIDES = ["top", "right", "bottom", "left"];
  const DIR = { top: [0, -1], right: [1, 0], bottom: [0, 1], left: [-1, 0] };
  const center = (n) => [+n.x + +n.w / 2, +n.y + +n.h / 2];

  function autoSides(a, b) {
    const [ax, ay] = center(a), [bx, by] = center(b), dx = bx - ax, dy = by - ay;
    const sw = (+a.w + +b.w) || 1, sh = (+a.h + +b.h) || 1;
    if (Math.abs(dx) / sw >= Math.abs(dy) / sh) return dx >= 0 ? ["right", "left"] : ["left", "right"];
    return dy >= 0 ? ["bottom", "top"] : ["top", "bottom"];
  }

  function edgeSides(e, nodes) {
    let [s1, s2] = autoSides(nodes[e.from], nodes[e.to]);
    if (SIDES.includes(e.fromSide)) s1 = e.fromSide;
    if (SIDES.includes(e.toSide)) s2 = e.toSide;
    return [s1, s2];
  }

  function anchorSlots(edges, nodes) {
    const use = new Map(), order = [];
    edges.forEach((e, i) => {
      if (!nodes[e.from] || !nodes[e.to] || e.from === e.to) return;
      const [s1, s2] = edgeSides(e, nodes);
      const k1 = e.from + "\u0000" + s1, k2 = e.to + "\u0000" + s2;
      if (!use.has(k1)) use.set(k1, []); use.get(k1).push([i, 0]);
      if (!use.has(k2)) use.set(k2, []); use.get(k2).push([i, 1]);
      order.push([i, s1, s2]);
    });
    const slots = new Map();
    use.forEach((lst, key) => {
      const [nid, side] = key.split("\u0000"), n = nodes[nid];
      const [cx, cy] = center(n);
      const otherPos = ([i, end]) => { const e = edges[i]; const [ox, oy] = center(nodes[end === 0 ? e.to : e.from]); return side === "top" || side === "bottom" ? ox : oy; };
      lst = lst.slice().sort((p, q) => (otherPos(p) - otherPos(q)) || (p[0] - q[0]));
      const k = lst.length, x = +n.x, y = +n.y, w = +n.w, h = +n.h;
      lst.forEach(([i, end], j) => {
        const f = (j + 1) / (k + 1);
        let p = side === "top" ? [x + w * f, y] : side === "bottom" ? [x + w * f, y + h] : side === "left" ? [x, y + h * f] : [x + w, y + h * f];
        if (n.type === "diamond") p = { top: [cx, y], bottom: [cx, y + h], left: [x, cy], right: [x + w, cy] }[side];
        slots.set(i + ":" + end, p);
      });
    });
    return { order, slots };
  }

  function round2(v) { return Math.round(v * 100) / 100; }

  function simplify(pts) {
    const out = [];
    pts.forEach((p) => {
      p = [round2(p[0]), round2(p[1])];
      const l = out[out.length - 1];
      if (l && Math.abs(l[0] - p[0]) < 0.01 && Math.abs(l[1] - p[1]) < 0.01) return;
      out.push(p);
    });
    let i = 1;
    while (i < out.length - 1) {
      const a = out[i - 1], b = out[i], c = out[i + 1];
      if ((Math.abs(a[0] - b[0]) < 0.01 && Math.abs(b[0] - c[0]) < 0.01) || (Math.abs(a[1] - b[1]) < 0.01 && Math.abs(b[1] - c[1]) < 0.01)) out.splice(i, 1);
      else i++;
    }
    return out;
  }

  function route(p1, s1, p2, s2, style, stub = 16) {
    if (style === "straight") return [p1, p2];
    const d1 = DIR[s1], d2 = DIR[s2];
    const a = [p1[0] + d1[0] * stub, p1[1] + d1[1] * stub], b = [p2[0] + d2[0] * stub, p2[1] + d2[1] * stub];
    const h1 = s1 === "left" || s1 === "right", h2 = s2 === "left" || s2 === "right";
    let pts;
    if (h1 && h2) { const mx = (a[0] + b[0]) / 2; pts = [p1, a, [mx, a[1]], [mx, b[1]], b, p2]; }
    else if (!h1 && !h2) { const my = (a[1] + b[1]) / 2; pts = [p1, a, [a[0], my], [b[0], my], b, p2]; }
    else if (h1) pts = [p1, a, [b[0], a[1]], b, p2];
    else pts = [p1, a, [a[0], b[1]], b, p2];
    return simplify(pts);
  }

  function midpoint(pts) {
    const lens = []; for (let i = 0; i < pts.length - 1; i++) lens.push(Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]));
    let half = lens.reduce((s, v) => s + v, 0) / 2;
    for (let i = 0; i < lens.length; i++) {
      if (half <= lens[i] && lens[i] > 0) { const f = half / lens[i]; return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f]; }
      half -= lens[i];
    }
    return pts[Math.floor(pts.length / 2)];
  }

  function arrow(tip, prev, width) {
    const dx = tip[0] - prev[0], dy = tip[1] - prev[1], ln = Math.hypot(dx, dy) || 1, ux = dx / ln, uy = dy / ln;
    const L = 7 + width * 1.6, W = 3.5 + width * 1.0, bx = tip[0] - ux * L, by = tip[1] - uy * L;
    return [tip, [bx - uy * W, by + ux * W], [bx + uy * W, by - ux * W]];
  }

  function shorten(tip, prev, by) {
    const dx = tip[0] - prev[0], dy = tip[1] - prev[1], ln = Math.hypot(dx, dy);
    if (ln <= by || ln === 0) return tip;
    return [tip[0] - (dx / ln) * by, tip[1] - (dy / ln) * by];
  }

  function edgePrims(edges, nodes) {
    const { order, slots } = anchorSlots(edges, nodes), out = [], labels = [], paths = {};
    order.forEach(([i, s1, s2]) => {
      const e = edges[i], k = S.edges[e.kind] || S.edges.signal;
      const pts = route(slots.get(i + ":0"), s1, slots.get(i + ":1"), s2, e.route || "ortho");
      const arrowMode = e.arrow || "end", meta = { edge: e.id || "" };
      const line = pts.slice(), heads = [];
      if ((arrowMode === "end" || arrowMode === "both") && pts.length >= 2) { heads.push(arrow(pts[pts.length - 1], pts[pts.length - 2], k.width)); line[line.length - 1] = shorten(pts[pts.length - 1], pts[pts.length - 2], 5 + k.width); }
      if ((arrowMode === "start" || arrowMode === "both") && pts.length >= 2) { heads.push(arrow(pts[0], pts[1], k.width)); line[0] = shorten(pts[0], pts[1], 5 + k.width); }
      out.push(["path", polyPath(line, false), null, k.color, k.width, k.dash, meta]);
      heads.forEach((hd) => out.push(["path", polyPath(hd), k.color, null, 0, "", meta]));
      paths[e.id] = pts;
      const text = (e.label || "").trim();
      if (text) {
        const [mx, my] = midpoint(pts), size = S.font.edge, lh = S.font.line;
        const lines = text.split("\n").slice(0, 3);
        const tw = Math.max(...lines.map((t) => textWidth(t, size, false))), th = lines.length * size * lh;
        labels.push(["label_bg", mx - tw / 2 - 4, my - th / 2 - 2, tw + 8, th + 4, meta]);
        lines.forEach((t, j) => labels.push(["text", mx, my - th / 2 + (j + 1) * size * lh - size * (lh - 1) - 1.5, t, size, false,
          (e.kind && e.kind !== "signal") ? k.color : "#374151", "middle", meta]));
      }
    });
    return { prims: out.concat(labels), paths };
  }

  function layout(data) {
    const nodes = {}; (data.nodes || []).forEach((n) => (nodes[n.id] = n));
    const frames = (data.nodes || []).filter((n) => n.type === "frame"), blocks = (data.nodes || []).filter((n) => n.type !== "frame");
    let prims = [];
    frames.forEach((n) => (prims = prims.concat(nodePrims(n))));
    const ep = edgePrims(data.edges || [], nodes);
    prims = prims.concat(ep.prims.filter((p) => p[0] === "path"));
    blocks.forEach((n) => (prims = prims.concat(nodePrims(n))));
    prims = prims.concat(ep.prims.filter((p) => p[0] !== "path"));
    return { prims, paths: ep.paths };
  }

  function bounds(prims, margin = 24) {
    const xs = [], ys = [];
    prims.forEach((p) => {
      if (p[0] === "path") p[1].forEach((c) => { for (let i = 1; i < c.length; i += 2) { xs.push(c[i]); ys.push(c[i + 1]); } });
      else if (p[0] === "text") { const w = textWidth(p[3], p[4], p[5]), x0 = p[7] === "middle" ? p[1] - w / 2 : p[1]; xs.push(x0, x0 + w); ys.push(p[2] - p[4], p[2] + 3); }
      else if (p[0] === "label_bg") { xs.push(p[1], p[1] + p[3]); ys.push(p[2], p[2] + p[4]); }
    });
    if (!xs.length) return [0, 0, 400, 240];
    const x0 = Math.min(...xs), y0 = Math.min(...ys);
    return [x0 - margin, y0 - margin, Math.max(...xs) - x0 + 2 * margin, Math.max(...ys) - y0 + 2 * margin];
  }

  function d(cmds) {
    return cmds.map((c) => (c[0] === "Z" ? "Z" : c[0] + " " + c.slice(1).map((v) => +(+v).toFixed(2)).join(" "))).join(" ");
  }

  global.DiagramGeometry = { init, layout, bounds, textWidth, wrap, nodePrims, edgeSides, autoSides, SIDES, d, center };
})(window);
