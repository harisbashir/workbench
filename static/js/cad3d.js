/* Workbench 3D viewer — shows converted CAD models (board models, enclosures).
 *
 * Mouse: drag to rotate, right-drag or Shift+drag to move, wheel to zoom, double-click to centre on a point.
 * Touch: one finger rotates, two fingers zoom and move.
 * Tools: standard views, fit, wireframe, section plane, point-to-point measure, parts list, PNG screenshot.
 * Written for WebGL2 without any libraries.
 */
(function () {
  "use strict";

  // ---- small matrix helpers (column-major, like WebGL) -------------------------------------
  const M = {
    persp(fovy, aspect, near, far) {
      const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
      return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
    },
    ortho(l, r, b, t, n, f) {
      return new Float32Array([2 / (r - l), 0, 0, 0, 0, 2 / (t - b), 0, 0, 0, 0, -2 / (f - n), 0,
        -(r + l) / (r - l), -(t + b) / (t - b), -(f + n) / (f - n), 1]);
    },
    lookAt(e, c, u) {
      let zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2];
      let l = Math.hypot(zx, zy, zz) || 1; zx /= l; zy /= l; zz /= l;
      let xx = u[1] * zz - u[2] * zy, xy = u[2] * zx - u[0] * zz, xz = u[0] * zy - u[1] * zx;
      l = Math.hypot(xx, xy, xz) || 1; xx /= l; xy /= l; xz /= l;
      const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
      return new Float32Array([xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
        -(xx * e[0] + xy * e[1] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1]);
    },
    mul(a, b) {
      const o = new Float32Array(16);
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) {
        let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k]; o[i * 4 + j] = s;
      }
      return o;
    },
    invert(m) {
      const a = m, o = new Float32Array(16);
      const b00 = a[0] * a[5] - a[1] * a[4], b01 = a[0] * a[6] - a[2] * a[4], b02 = a[0] * a[7] - a[3] * a[4],
        b03 = a[1] * a[6] - a[2] * a[5], b04 = a[1] * a[7] - a[3] * a[5], b05 = a[2] * a[7] - a[3] * a[6],
        b06 = a[8] * a[13] - a[9] * a[12], b07 = a[8] * a[14] - a[10] * a[12], b08 = a[8] * a[15] - a[11] * a[12],
        b09 = a[9] * a[14] - a[10] * a[13], b10 = a[9] * a[15] - a[11] * a[13], b11 = a[10] * a[15] - a[11] * a[14];
      let d = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
      if (!d) return null; d = 1 / d;
      o[0] = (a[5] * b11 - a[6] * b10 + a[7] * b09) * d; o[1] = (a[2] * b10 - a[1] * b11 - a[3] * b09) * d;
      o[2] = (a[13] * b05 - a[14] * b04 + a[15] * b03) * d; o[3] = (a[10] * b04 - a[9] * b05 - a[11] * b03) * d;
      o[4] = (a[6] * b08 - a[4] * b11 - a[7] * b07) * d; o[5] = (a[0] * b11 - a[2] * b08 + a[3] * b07) * d;
      o[6] = (a[14] * b02 - a[12] * b05 - a[15] * b01) * d; o[7] = (a[8] * b05 - a[10] * b02 + a[11] * b01) * d;
      o[8] = (a[4] * b10 - a[5] * b08 + a[7] * b06) * d; o[9] = (a[1] * b08 - a[0] * b10 - a[3] * b06) * d;
      o[10] = (a[12] * b04 - a[13] * b02 + a[15] * b00) * d; o[11] = (a[9] * b02 - a[8] * b04 - a[11] * b00) * d;
      o[12] = (a[5] * b07 - a[4] * b09 - a[6] * b06) * d; o[13] = (a[0] * b09 - a[1] * b07 + a[2] * b06) * d;
      o[14] = (a[13] * b01 - a[12] * b03 - a[14] * b00) * d; o[15] = (a[8] * b03 - a[9] * b01 + a[10] * b00) * d;
      return o;
    },
    xform(m, v) {
      const x = v[0], y = v[1], z = v[2], w = m[3] * x + m[7] * y + m[11] * z + m[15];
      return [(m[0] * x + m[4] * y + m[8] * z + m[12]) / w, (m[1] * x + m[5] * y + m[9] * z + m[13]) / w, (m[2] * x + m[6] * y + m[10] * z + m[14]) / w];
    },
  };

  const VS = `#version 300 es
  in vec3 aPos; uniform mat4 uMVP; out vec3 vPos;
  void main() { vPos = aPos; gl_Position = uMVP * vec4(aPos, 1.0); }`;
  const FS = `#version 300 es
  precision highp float;
  in vec3 vPos; uniform vec4 uColor; uniform vec3 uEye; uniform vec3 uLight; uniform vec4 uClip; uniform int uFlat;
  out vec4 outColor;
  void main() {
    if (uClip.w > -1e29 && dot(vPos, uClip.xyz) > uClip.w) discard;
    if (uFlat == 1) { outColor = uColor; return; }
    vec3 n = normalize(cross(dFdx(vPos), dFdy(vPos)));
    vec3 v = normalize(uEye - vPos);
    if (dot(n, v) < 0.0) n = -n;                       // light both sides (open meshes, section cuts)
    float key = max(dot(n, normalize(uLight)), 0.0);
    float fill = max(dot(n, normalize(vec3(-0.4, -0.6, 0.3))), 0.0);
    float rim = pow(1.0 - max(dot(n, v), 0.0), 3.0);
    vec3 h = normalize(normalize(uLight) + v);
    float spec = pow(max(dot(n, h), 0.0), 48.0) * 0.25;
    vec3 c = uColor.rgb * (0.30 + 0.62 * key + 0.18 * fill) + vec3(spec) + rim * 0.10;
    outColor = vec4(c, uColor.a);
  }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }

  function parseMesh(buf) {
    const dv = new DataView(buf);
    const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
    if (magic !== "WBM1") throw new Error("Not a Workbench model file");
    const hlen = dv.getUint32(4, true);
    const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, hlen)));
    let off = 8 + hlen;
    const totalV = header.parts.reduce((s, p) => s + p.vertices, 0);
    const pos = new Float32Array(buf, off, totalV * 3); off += totalV * 12;
    const idx = new Uint32Array(buf, off, header.triangles * 3);
    header.parts.forEach((p) => {
      p.pos = pos.subarray(p.vertex_offset * 3, (p.vertex_offset + p.vertices) * 3);
      p.idx = idx.subarray(p.index_offset * 3, (p.index_offset + p.triangles) * 3);
      p.visible = true;
    });
    return header;
  }

  const fmt = (n) => (Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(2));

  class Viewer {
    constructor(root) {
      this.root = root;
      this.canvas = root.querySelector("canvas");
      this.overlay = root.querySelector("[data-overlay]");
      this.readout = root.querySelector("[data-readout]");
      this.info = root.querySelector("[data-info]");
      this.partsBox = root.querySelector("[data-parts]");
      this.name = root.dataset.name || "model";
      this.yaw = -0.75; this.pitch = 0.55; this.dist = 100; this.target = [0, 0, 0];
      this.ortho = false; this.wire = false; this.section = null; this.measure = null; this.marks = [];
      this.ready = false;
    }

    message(text, isError) {
      this.overlay.hidden = !text;
      this.overlay.textContent = text || "";
      this.overlay.classList.toggle("error", !!isError);
    }

    start() {
      const st = this.root.dataset.status;
      if (st === "ready") return this.load();
      if (st === "pending" || st === "converting") {
        this.message("Preparing the 3D view… large STEP files can take a minute.");
        return this.poll();
      }
      this.message(this.root.dataset.message || "This file can't be shown in 3D.", true);
    }

    poll() {
      fetch(this.root.dataset.statusUrl, { credentials: "same-origin" }).then((r) => r.json()).then((s) => {
        if (s.status === "ready") return this.load();
        if (s.status === "pending" || s.status === "converting") return setTimeout(() => this.poll(), 2500);
        this.message(s.message || "This file can't be shown in 3D.", true);
      }).catch(() => setTimeout(() => this.poll(), 5000));
    }

    load() {
      this.message("Loading model…");
      fetch(this.root.dataset.meshUrl, { credentials: "same-origin" }).then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.arrayBuffer();
      }).then((buf) => {
        this.model = parseMesh(buf);
        this.init();
      }).catch((e) => this.message("Couldn't load the model (" + e.message + ").", true));
    }

    init() {
      const gl = this.canvas.getContext("webgl2", { antialias: true, preserveDrawingBuffer: true });
      if (!gl) return this.message("Your browser doesn't support WebGL2, which the 3D viewer needs.", true);
      this.gl = gl;
      const prog = gl.createProgram();
      gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
      gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(prog);
      this.prog = prog;
      this.loc = {};
      ["uMVP", "uColor", "uEye", "uLight", "uClip", "uFlat"].forEach((n) => (this.loc[n] = gl.getUniformLocation(prog, n)));
      this.aPos = gl.getAttribLocation(prog, "aPos");
      this.model.parts.forEach((p) => {
        p.vao = gl.createVertexArray(); gl.bindVertexArray(p.vao);
        const vb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, p.pos, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(this.aPos); gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 0, 0);
        p.ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, p.ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, p.idx, gl.STATIC_DRAW);
        p.vb = vb;
      });
      gl.bindVertexArray(null);
      this.markVao = gl.createVertexArray(); this.markBuf = gl.createBuffer();
      const [lo, hi] = this.model.bbox;
      this.lo = lo; this.hi = hi;
      this.center = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
      this.radius = Math.max(Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2, 0.5);
      this.showInfo();
      this.buildParts();
      this.bind();
      this.view("iso");
      this.message("");
      this.ready = true;
      new ResizeObserver(() => this.draw()).observe(this.canvas);
    }

    showInfo() {
      const s = [0, 1, 2].map((i) => this.hi[i] - this.lo[i]);
      if (this.info) {
        this.info.textContent = `${fmt(s[0])} × ${fmt(s[1])} × ${fmt(s[2])} mm · ${this.model.triangles.toLocaleString()} triangles` +
          (this.model.parts.length > 1 ? ` · ${this.model.parts.length} parts` : "") +
          (this.model.notes && this.model.notes.length ? " · " + this.model.notes.join(" ") : "");
      }
    }

    buildParts() {
      if (!this.partsBox) return;
      const list = this.partsBox.querySelector("ul");
      list.textContent = "";
      this.model.parts.forEach((p, i) => {
        const li = document.createElement("li");
        const label = document.createElement("label");
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = true;
        cb.addEventListener("change", () => { p.visible = cb.checked; this.draw(); });
        const sw = document.createElement("span"); sw.className = "cad-swatch";
        sw.style.setProperty("--swatch", `rgb(${p.color.slice(0, 3).map((c) => Math.round(c * 255)).join(",")})`);
        const nm = document.createElement("span"); nm.textContent = p.name + (p.triangles ? "" : ""); nm.title = `${p.triangles.toLocaleString()} triangles`;
        label.append(cb, sw, nm); li.append(label); list.append(li);
        p.checkbox = cb;
        void i;
      });
    }

    // ---- camera ----
    eye() {
      const cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
      return [this.target[0] + this.dist * cp * Math.cos(this.yaw), this.target[1] + this.dist * cp * Math.sin(this.yaw), this.target[2] + this.dist * sp];
    }

    matrices() {
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
        this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      }
      const aspect = w / Math.max(h, 1), eye = this.eye();
      const up = Math.abs(Math.cos(this.pitch)) < 1e-4 ? [Math.cos(this.yaw) * -Math.sign(this.pitch), Math.sin(this.yaw) * -Math.sign(this.pitch), 0] : [0, 0, 1];
      const view = M.lookAt(eye, this.target, up);
      const near = Math.max(this.dist - this.radius * 4, this.radius * 0.002), far = this.dist + this.radius * 4;
      let proj;
      if (this.ortho) {
        const hh = this.dist * Math.tan(0.35);
        proj = M.ortho(-hh * aspect, hh * aspect, -hh, hh, near * 0.01, far * 2);
      } else proj = M.persp(0.7, aspect, near, far);
      return { view, proj, mvp: M.mul(proj, view), eye };
    }

    view(name) {
      const V = { iso: [-0.75, 0.55], top: [-Math.PI / 2, Math.PI / 2 - 1e-4], bottom: [-Math.PI / 2, -Math.PI / 2 + 1e-4],
        front: [-Math.PI / 2, 0], back: [Math.PI / 2, 0], left: [Math.PI, 0], right: [0, 0] };
      [this.yaw, this.pitch] = V[name] || V.iso;
      this.fit();
    }

    fit() {
      this.target = this.center.slice();
      this.dist = this.radius / Math.sin(0.35) * 1.08;
      this.draw();
    }

    // ---- drawing ----
    draw() {
      if (!this.gl) return;
      cancelAnimationFrame(this._raf);
      this._raf = requestAnimationFrame(() => this.render());
    }

    render() {
      const gl = this.gl, m = this.matrices();
      this.mats = m;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clearColor(0.13, 0.15, 0.18, 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST);
      gl.useProgram(this.prog);
      gl.uniformMatrix4fv(this.loc.uMVP, false, m.mvp);
      gl.uniform3fv(this.loc.uEye, m.eye);
      const e = m.eye, t = this.target;
      gl.uniform3f(this.loc.uLight, e[0] - t[0] + this.radius, e[1] - t[1] + this.radius * 0.5, e[2] - t[2] + this.radius * 2);
      gl.uniform4fv(this.loc.uClip, this.section ? this.section : [0, 0, 0, -1e30]);
      gl.uniform1i(this.loc.uFlat, 0);
      const transparent = [];
      this.model.parts.forEach((p) => {
        if (!p.visible) return;
        if (p.color[3] < 0.99) { transparent.push(p); return; }
        this.drawPart(p, p.color);
      });
      if (transparent.length) {
        gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
        transparent.forEach((p) => this.drawPart(p, p.color));
        gl.depthMask(true); gl.disable(gl.BLEND);
      }
      if (this.wire) {
        gl.uniform1i(this.loc.uFlat, 1);
        gl.enable(gl.POLYGON_OFFSET_FILL);
        this.model.parts.forEach((p) => {
          if (!p.visible) return;
          if (!p.eb) this.buildEdges(p);
          gl.bindVertexArray(p.vao);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, p.eb);
          gl.uniform4f(this.loc.uColor, 0, 0, 0, 0.55);
          gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
          gl.drawElements(gl.LINES, p.eCount, gl.UNSIGNED_INT, 0);
          gl.disable(gl.BLEND);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, p.ib);
        });
        gl.disable(gl.POLYGON_OFFSET_FILL);
      }
      this.drawMarks();
      gl.bindVertexArray(null);
    }

    drawPart(p, c) {
      const gl = this.gl;
      gl.uniform4f(this.loc.uColor, c[0], c[1], c[2], c[3]);
      gl.bindVertexArray(p.vao);
      gl.drawElements(gl.TRIANGLES, p.idx.length, gl.UNSIGNED_INT, 0);
    }

    buildEdges(p) {
      const t = p.idx, e = new Uint32Array(t.length * 2);
      for (let i = 0, j = 0; i < t.length; i += 3) {
        e[j++] = t[i]; e[j++] = t[i + 1]; e[j++] = t[i + 1]; e[j++] = t[i + 2]; e[j++] = t[i + 2]; e[j++] = t[i];
      }
      p.eb = this.gl.createBuffer();
      this.gl.bindVertexArray(p.vao);
      this.gl.bindBuffer(this.gl.ELEMENT_ARRAY_BUFFER, p.eb);
      this.gl.bufferData(this.gl.ELEMENT_ARRAY_BUFFER, e, this.gl.STATIC_DRAW);
      p.eCount = e.length;
    }

    drawMarks() {
      if (!this.marks.length) return;
      const gl = this.gl, pts = [];
      const s = this.radius * 0.012;
      this.marks.forEach((a) => { pts.push(a[0] - s, a[1], a[2], a[0] + s, a[1], a[2], a[0], a[1] - s, a[2], a[0], a[1] + s, a[2], a[0], a[1], a[2] - s, a[0], a[1], a[2] + s); });
      if (this.marks.length === 2) pts.push(...this.marks[0], ...this.marks[1]);
      gl.bindVertexArray(this.markVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.markBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(this.aPos); gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.uniform1i(this.loc.uFlat, 1);
      gl.uniform4fv(this.loc.uClip, [0, 0, 0, -1e30]);
      gl.uniform4f(this.loc.uColor, 1.0, 0.82, 0.2, 1);
      gl.disable(gl.DEPTH_TEST);
      gl.drawArrays(gl.LINES, 0, pts.length / 3);
      gl.enable(gl.DEPTH_TEST);
    }

    // ---- picking (for measuring and double-click centring) ----
    ray(clientX, clientY) {
      const r = this.canvas.getBoundingClientRect();
      const x = ((clientX - r.left) / r.width) * 2 - 1, y = -(((clientY - r.top) / r.height) * 2 - 1);
      const inv = M.invert(this.mats.mvp);
      const a = M.xform(inv, [x, y, -1]), b = M.xform(inv, [x, y, 1]);
      const d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], l = Math.hypot(...d);
      return { o: a, d: [d[0] / l, d[1] / l, d[2] / l] };
    }

    pick(clientX, clientY) {
      const { o, d } = this.ray(clientX, clientY);
      let best = Infinity;
      const clip = this.section;
      this.model.parts.forEach((p) => {
        if (!p.visible) return;
        const P = p.pos, T = p.idx;
        for (let i = 0; i < T.length; i += 3) {
          const a = T[i] * 3, b = T[i + 1] * 3, c = T[i + 2] * 3;
          const e1x = P[b] - P[a], e1y = P[b + 1] - P[a + 1], e1z = P[b + 2] - P[a + 2];
          const e2x = P[c] - P[a], e2y = P[c + 1] - P[a + 1], e2z = P[c + 2] - P[a + 2];
          const px = d[1] * e2z - d[2] * e2y, py = d[2] * e2x - d[0] * e2z, pz = d[0] * e2y - d[1] * e2x;
          const det = e1x * px + e1y * py + e1z * pz;
          if (Math.abs(det) < 1e-12) continue;
          const inv = 1 / det;
          const tx = o[0] - P[a], ty = o[1] - P[a + 1], tz = o[2] - P[a + 2];
          const u = (tx * px + ty * py + tz * pz) * inv;
          if (u < 0 || u > 1) continue;
          const qx = ty * e1z - tz * e1y, qy = tz * e1x - tx * e1z, qz = tx * e1y - ty * e1x;
          const v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv;
          if (v < 0 || u + v > 1) continue;
          const t = (e2x * qx + e2y * qy + e2z * qz) * inv;
          if (t > 0 && t < best) {
            if (clip) {
              const hx = o[0] + d[0] * t, hy = o[1] + d[1] * t, hz = o[2] + d[2] * t;
              if (hx * clip[0] + hy * clip[1] + hz * clip[2] > clip[3]) continue;
            }
            best = t;
          }
        }
      });
      return best < Infinity ? [o[0] + d[0] * best, o[1] + d[1] * best, o[2] + d[2] * best] : null;
    }

    // ---- interaction ----
    bind() {
      const c = this.canvas;
      let drag = null;
      const pointers = new Map();
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      c.addEventListener("pointerdown", (e) => {
        c.setPointerCapture(e.pointerId);
        pointers.set(e.pointerId, [e.clientX, e.clientY]);
        drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.button === 1 || e.shiftKey, moved: false };
      });
      c.addEventListener("pointermove", (e) => {
        if (!drag) return;
        const prev = pointers.get(e.pointerId);
        pointers.set(e.pointerId, [e.clientX, e.clientY]);
        if (pointers.size === 2) {  // pinch zoom + pan
          const pts = [...pointers.values()];
          const dist = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]);
          if (this._pinch) { this.dist *= this._pinch / dist; this.dist = Math.max(this.dist, this.radius * 0.05); }
          this._pinch = dist;
          this.draw(); return;
        }
        const dx = e.clientX - prev[0], dy = e.clientY - prev[1];
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
        if (drag.pan) this.pan(dx, dy);
        else {
          this.yaw -= dx * 0.008;
          this.pitch = Math.max(-Math.PI / 2 + 1e-4, Math.min(Math.PI / 2 - 1e-4, this.pitch + dy * 0.008));
        }
        this.draw();
      });
      const end = (e) => {
        pointers.delete(e.pointerId);
        if (pointers.size < 2) this._pinch = null;
        if (drag && !drag.moved && this.measure && e.button === 0) this.addMark(e.clientX, e.clientY);
        if (!pointers.size) drag = null;
      };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        this.dist *= Math.exp(e.deltaY * 0.0012);
        this.dist = Math.max(this.dist, this.radius * 0.05);
        this.draw();
      }, { passive: false });
      c.addEventListener("dblclick", (e) => {
        const p = this.pick(e.clientX, e.clientY);
        if (p) { this.target = p; this.dist *= 0.6; this.draw(); }
      });

      const on = (sel, ev, fn) => this.root.querySelectorAll(sel).forEach((el) => el.addEventListener(ev, fn));
      on("[data-view]", "click", (e) => this.view(e.currentTarget.dataset.view));
      on("[data-fit]", "click", () => this.fit());
      on("[data-wire]", "click", (e) => { this.wire = !this.wire; e.currentTarget.classList.toggle("active", this.wire); this.draw(); });
      on("[data-ortho]", "click", (e) => { this.ortho = !this.ortho; e.currentTarget.classList.toggle("active", this.ortho); this.draw(); });
      on("[data-measure]", "click", (e) => {
        this.measure = !this.measure; this.marks = [];
        e.currentTarget.classList.toggle("active", this.measure);
        c.classList.toggle("measuring", this.measure);
        this.setReadout(this.measure ? "Click two points on the model." : "");
        this.draw();
      });
      on("[data-parts-toggle]", "click", (e) => {
        this.partsBox.hidden = !this.partsBox.hidden; e.currentTarget.classList.toggle("active", !this.partsBox.hidden);
      });
      on("[data-parts-all]", "click", () => { this.model.parts.forEach((p) => { p.visible = true; if (p.checkbox) p.checkbox.checked = true; }); this.draw(); });
      on("[data-shot]", "click", () => this.screenshot());
      on("[data-full]", "click", () => {
        if (document.fullscreenElement) document.exitFullscreen();
        else this.root.requestFullscreen && this.root.requestFullscreen();
      });
      document.addEventListener("fullscreenchange", () => setTimeout(() => this.draw(), 50));
      const axis = this.root.querySelector("[data-section-axis]"), slider = this.root.querySelector("[data-section-pos]");
      const flip = this.root.querySelector("[data-section-flip]");
      const updateSection = () => {
        const a = axis.value;
        if (!a) { this.section = null; slider.disabled = true; flip.disabled = true; this.draw(); return; }
        slider.disabled = false; flip.disabled = false;
        const i = { x: 0, y: 1, z: 2 }[a], f = parseFloat(slider.value) / 1000;
        const pos = this.lo[i] + (this.hi[i] - this.lo[i]) * f, s = flip.classList.contains("active") ? -1 : 1;
        const n = [0, 0, 0]; n[i] = s;
        this.section = [n[0], n[1], n[2], s * pos];
        this.draw();
      };
      if (axis) {
        axis.addEventListener("change", updateSection);
        slider.addEventListener("input", updateSection);
        flip.addEventListener("click", () => { flip.classList.toggle("active"); updateSection(); });
      }
    }

    pan(dx, dy) {
      const h = this.canvas.clientHeight || 1, scale = (2 * this.dist * Math.tan(0.35)) / h;
      const cy = Math.cos(this.yaw), sy = Math.sin(this.yaw), sp = Math.sin(this.pitch), cp = Math.cos(this.pitch);
      const right = [-sy, cy, 0], up = [-sp * cy, -sp * sy, cp];
      for (let i = 0; i < 3; i++) this.target[i] += (-dx * right[i] + dy * up[i]) * scale;
    }

    addMark(x, y) {
      const p = this.pick(x, y);
      if (!p) return;
      if (this.marks.length >= 2) this.marks = [];
      this.marks.push(p);
      if (this.marks.length === 2) {
        const [a, b] = this.marks, d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
        this.setReadout(`${fmt(Math.hypot(...d))} mm   (Δx ${fmt(d[0])}  Δy ${fmt(d[1])}  Δz ${fmt(d[2])})`);
      } else this.setReadout(`Point at x ${fmt(p[0])}  y ${fmt(p[1])}  z ${fmt(p[2])} — click a second point.`);
      this.draw();
    }

    setReadout(t) { if (this.readout) { this.readout.textContent = t; this.readout.hidden = !t; } }

    screenshot() {
      this.render();
      this.canvas.toBlob((blob) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = this.name.replace(/\.[^.]+$/, "") + ".png";
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      }, "image/png");
    }
  }

  function init() {
    document.querySelectorAll("[data-cad-viewer]").forEach((el) => {
      if (el._viewer) return;
      el._viewer = new Viewer(el);
      el._viewer.start();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  window.WorkbenchCad = { init };
})();
