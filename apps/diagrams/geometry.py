"""Layout rules for block diagrams, shared by the SVG and PDF exports.

The browser editor (static/js/diagram.js) follows exactly the same rules, using the
STYLE dictionary below (passed to the page as JSON), so an export looks like the drawing.

`layout(data)` turns the stored JSON into a list of drawing primitives:
    ("path", d, fill, stroke, width, dash, meta)  d = [("M", x, y), ("L", x, y), ("C", x1, y1, x2, y2, x, y), ("Z",)]
    ("text", x, y, text, size, bold, color, anchor, meta)   anchor: "start" | "middle"
    ("label_bg", x, y, w, h)                                 white box behind edge labels
"""
import math

GRID = 10
KAPPA = 0.5522847498

COLORS = {
    "gray": {"fill": "#f3f4f6", "stroke": "#6b7280", "text": "#111827"},
    "blue": {"fill": "#e8f0fe", "stroke": "#2f6fdf", "text": "#0f2a5c"},
    "green": {"fill": "#e7f6ec", "stroke": "#2e8b57", "text": "#12391f"},
    "amber": {"fill": "#fff4e0", "stroke": "#c77c00", "text": "#4d3000"},
    "red": {"fill": "#fdecec", "stroke": "#d0443c", "text": "#5c1410"},
    "purple": {"fill": "#f2ecfd", "stroke": "#7c4dce", "text": "#321a5e"},
    "teal": {"fill": "#e3f6f5", "stroke": "#1c9c95", "text": "#0b3b38"},
    "pink": {"fill": "#fdebf3", "stroke": "#c2417f", "text": "#4f1030"},
    "white": {"fill": "#ffffff", "stroke": "#374151", "text": "#111827"},
    "dark": {"fill": "#374151", "stroke": "#111827", "text": "#ffffff"},
}

# Block shapes: label, default colour, and a hint for the palette.
SHAPES = {
    "block": {"label": "Block", "color": "gray", "w": 140, "h": 60, "hint": "Any functional block"},
    "mcu": {"label": "IC / MCU", "color": "blue", "w": 150, "h": 90, "hint": "Microcontroller, SoC, FPGA or other IC"},
    "power": {"label": "Power", "color": "amber", "w": 140, "h": 60, "hint": "Regulator, charger, power stage"},
    "connector": {"label": "Connector", "color": "white", "w": 120, "h": 50, "hint": "Connector, header, cable entry"},
    "sensor": {"label": "Sensor", "color": "green", "w": 130, "h": 50, "hint": "Sensor or transducer"},
    "memory": {"label": "Memory", "color": "purple", "w": 110, "h": 70, "hint": "Flash, EEPROM, SD card"},
    "battery": {"label": "Battery", "color": "amber", "w": 100, "h": 60, "hint": "Battery or supply input"},
    "ellipse": {"label": "External", "color": "white", "w": 130, "h": 60, "hint": "Outside the board: user, host, mains"},
    "diamond": {"label": "Decision", "color": "gray", "w": 120, "h": 80, "hint": "Selector, switch or decision"},
    "board": {"label": "Board", "color": "green", "w": 180, "h": 100, "hint": "A PCB of the product (system diagrams)"},
    "frame": {"label": "Group", "color": "gray", "w": 320, "h": 200, "hint": "Dashed frame around related blocks"},
    "text": {"label": "Text", "color": "white", "w": 140, "h": 30, "hint": "A free text note"},
}

# Connection types, following common block-diagram conventions.
EDGE_KINDS = {
    "signal": {"label": "Signal", "color": "#4b5563", "width": 1.5, "dash": ""},
    "power": {"label": "Power", "color": "#d0443c", "width": 2.6, "dash": ""},
    "ground": {"label": "Ground", "color": "#111827", "width": 2.0, "dash": ""},
    "bus": {"label": "Digital bus (SPI, I²C, UART, CAN…)", "color": "#2f6fdf", "width": 2.4, "dash": ""},
    "highspeed": {"label": "High-speed / differential (USB, Ethernet…)", "color": "#7c4dce", "width": 2.8, "dash": ""},
    "analog": {"label": "Analog", "color": "#2e8b57", "width": 1.6, "dash": "6 4"},
    "rf": {"label": "RF / antenna", "color": "#c77c00", "width": 1.6, "dash": "8 3 2 3"},
    "mechanical": {"label": "Cable / mechanical", "color": "#8b5e34", "width": 2.0, "dash": "10 4"},
}

FONT = {"label": 13, "sub": 11, "edge": 11, "frame": 12, "line": 1.25}

# Helvetica / Helvetica-Bold advance widths (per 1000 em) for ASCII 32–126.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
         556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667,
         556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556,
         556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722,
         500, 500, 500, 334, 260, 334, 584]
_HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
           556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722,
           611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556,
           611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778,
           556, 556, 500, 389, 280, 389, 584]

STYLE = {"colors": COLORS, "shapes": SHAPES, "edges": EDGE_KINDS, "font": FONT, "grid": GRID,
         "widths": _HELV, "widths_bold": _HELV_B}


def text_width(s, size, bold=False):
    table = _HELV_B if bold else _HELV
    total = 0
    for ch in s:
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else 556
    return total * size / 1000.0


def wrap(text, width, size, bold=False):
    """Greedy word wrap; very long words are broken."""
    lines = []
    for para in (text or "").split("\n"):
        words = para.split(" ")
        line = ""
        for w in words:
            cand = w if not line else line + " " + w
            if text_width(cand, size, bold) <= width:
                line = cand
                continue
            if line:
                lines.append(line)
            line = ""
            while text_width(w, size, bold) > width and len(w) > 1:  # break a long word
                k = len(w)
                while k > 1 and text_width(w[:k], size, bold) > width:
                    k -= 1
                lines.append(w[:k])
                w = w[k:]
            line = w
        lines.append(line)
    while lines and lines[-1] == "" and len(lines) > 1:
        lines.pop()
    return lines


# --- shapes ------------------------------------------------------------------------------------

def rect_path(x, y, w, h, r=0.0):
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return [("M", x, y), ("L", x + w, y), ("L", x + w, y + h), ("L", x, y + h), ("Z",)]
    k = r * KAPPA
    return [("M", x + r, y), ("L", x + w - r, y), ("C", x + w - r + k, y, x + w, y + r - k, x + w, y + r),
            ("L", x + w, y + h - r), ("C", x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h),
            ("L", x + r, y + h), ("C", x + r - k, y + h, x, y + h - r + k, x, y + h - r),
            ("L", x, y + r), ("C", x, y + r - k, x + r - k, y, x + r, y), ("Z",)]


def ellipse_path(cx, cy, rx, ry):
    kx, ky = rx * KAPPA, ry * KAPPA
    return [("M", cx + rx, cy), ("C", cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry),
            ("C", cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy),
            ("C", cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry),
            ("C", cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy), ("Z",)]


def poly_path(points, closed=True):
    d = [("M", *points[0])] + [("L", *p) for p in points[1:]]
    return d + [("Z",)] if closed else d


def node_color(n):
    return COLORS.get(n.get("color") or SHAPES.get(n.get("type"), SHAPES["block"])["color"], COLORS["gray"])


def text_inset(n):
    """Horizontal padding for the label inside each shape."""
    t = n.get("type")
    return {"mcu": 18, "diamond": n["w"] * 0.22, "ellipse": n["w"] * 0.14, "connector": 14}.get(t, 8)


def node_primitives(n):
    t = n.get("type", "block")
    x, y, w, h = float(n["x"]), float(n["y"]), float(n["w"]), float(n["h"])
    c = node_color(n)
    meta = {"node": n.get("id", "")}
    out = []
    if t == "frame":
        out.append(("path", rect_path(x, y, w, h, 6), "#fafbfc" if n.get("color") in (None, "gray") else c["fill"],
                    c["stroke"], 1.4, "7 4", meta))
    elif t == "text":
        pass
    elif t == "mcu":
        out.append(("path", rect_path(x, y, w, h, 3), c["fill"], c["stroke"], 1.8, "", meta))
        pins = max(2, int(h // 18))
        for i in range(pins):
            py = y + h * (i + 1) / (pins + 1)
            out.append(("path", [("M", x - 6, py), ("L", x, py)], None, c["stroke"], 1.6, "", meta))
            out.append(("path", [("M", x + w, py), ("L", x + w + 6, py)], None, c["stroke"], 1.6, "", meta))
        cx = x + w / 2
        out.append(("path", [("M", cx - 7, y), ("C", cx - 7, y + 7 * 1.3, cx + 7, y + 7 * 1.3, cx + 7, y)], None, c["stroke"], 1.2, "", meta))
    elif t == "power":
        out.append(("path", rect_path(x, y, w, h, 8), c["fill"], c["stroke"], 1.6, "", meta))
        out.append(("path", poly_path([(x + 10, y + 6), (x + 6, y + 15), (x + 10, y + 15), (x + 8, y + 22),
                                       (x + 15, y + 11), (x + 11, y + 11), (x + 14, y + 6)]), c["stroke"], None, 0, "", meta))
    elif t == "connector":
        n1, n2 = y + h * 0.3, y + h * 0.7
        out.append(("path", poly_path([(x, y), (x + w - 9, y), (x + w - 9, n1), (x + w, n1), (x + w, n2), (x + w - 9, n2),
                                       (x + w - 9, y + h), (x, y + h)]), c["fill"], c["stroke"], 1.6, "", meta))
    elif t == "sensor":
        out.append(("path", rect_path(x, y, w, h, min(h / 2, 18)), c["fill"], c["stroke"], 1.6, "", meta))
    elif t == "ellipse":
        out.append(("path", ellipse_path(x + w / 2, y + h / 2, w / 2, h / 2), c["fill"], c["stroke"], 1.6, "", meta))
    elif t == "diamond":
        out.append(("path", poly_path([(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]),
                    c["fill"], c["stroke"], 1.6, "", meta))
    elif t == "memory":
        ry = min(8.0, h / 6)
        k = w / 2 * KAPPA
        body = [("M", x, y + ry), ("L", x, y + h - ry),
                ("C", x, y + h - ry + ry * KAPPA, x + w / 2 - k, y + h, x + w / 2, y + h),
                ("C", x + w / 2 + k, y + h, x + w, y + h - ry + ry * KAPPA, x + w, y + h - ry),
                ("L", x + w, y + ry), ("C", x + w, y + ry - ry * KAPPA, x + w / 2 + k, y, x + w / 2, y),
                ("C", x + w / 2 - k, y, x, y + ry - ry * KAPPA, x, y + ry), ("Z",)]
        out.append(("path", body, c["fill"], c["stroke"], 1.6, "", meta))
        out.append(("path", [("M", x, y + ry), ("C", x, y + ry + ry * KAPPA, x + w / 2 - k, y + 2 * ry, x + w / 2, y + 2 * ry),
                             ("C", x + w / 2 + k, y + 2 * ry, x + w, y + ry + ry * KAPPA, x + w, y + ry)], None, c["stroke"], 1.6, "", meta))
    elif t == "battery":
        nub = w * 0.3
        out.append(("path", rect_path(x + (w - nub) / 2, y - 5, nub, 6, 1.5), c["stroke"], None, 0, "", meta))
        out.append(("path", rect_path(x, y, w, h, 4), c["fill"], c["stroke"], 1.6, "", meta))
    elif t == "board":
        out.append(("path", rect_path(x, y, w, h, 4), c["fill"], c["stroke"], 2.6, "", meta))
        for (hx, hy) in ((x + 8, y + 8), (x + w - 8, y + 8), (x + 8, y + h - 8), (x + w - 8, y + h - 8)):
            out.append(("path", ellipse_path(hx, hy, 2.6, 2.6), "#ffffff", c["stroke"], 1.2, "", meta))
    else:  # block
        out.append(("path", rect_path(x, y, w, h, 4), c["fill"], c["stroke"], 1.6, "", meta))
    out.extend(node_text(n, c))
    return out


def node_text(n, c=None):
    c = c or node_color(n)
    t = n.get("type", "block")
    x, y, w, h = float(n["x"]), float(n["y"]), float(n["w"]), float(n["h"])
    label, sub = (n.get("label") or "").strip(), (n.get("sub") or "").strip()
    meta = {"node": n.get("id", "")}
    out = []
    if t == "frame":
        if label:
            out.append(("text", x + 10, y + 10 + FONT["frame"], label, FONT["frame"], True, c["stroke"], "start", meta))
        return out
    inset = text_inset(n)
    width = max(w - 2 * inset, 20)
    lsize, ssize, lh = FONT["label"], FONT["sub"], FONT["line"]
    llines = wrap(label, width, lsize, True) if label else []
    slines = wrap(sub, width, ssize, False) if sub else []
    total = len(llines) * lsize * lh + (len(slines) * ssize * lh + (3 if llines else 0) if slines else 0)
    cy = y + h / 2 + (4 if t == "power" and not slines else 0)
    top = cy - total / 2
    color = c["text"] if t != "text" else "#111827"
    subc = "#d1d5db" if (n.get("color") == "dark") else "#4b5563"
    yy = top
    for line in llines:
        yy += lsize * lh
        out.append(("text", x + w / 2, yy - lsize * (lh - 1) - 1.5, line, lsize, True, color, "middle", meta))
    if slines:
        yy += 3 if llines else 0
        for line in slines:
            yy += ssize * lh
            out.append(("text", x + w / 2, yy - ssize * (lh - 1) - 1.2, line, ssize, False, subc, "middle", meta))
    return out


# --- edges ---------------------------------------------------------------------------------------

SIDES = ("top", "right", "bottom", "left")
_DIR = {"top": (0, -1), "right": (1, 0), "bottom": (0, 1), "left": (-1, 0)}


def _center(n):
    return float(n["x"]) + float(n["w"]) / 2, float(n["y"]) + float(n["h"]) / 2


def auto_sides(a, b):
    ax, ay = _center(a)
    bx, by = _center(b)
    dx, dy = bx - ax, by - ay
    sw = (float(a["w"]) + float(b["w"])) or 1
    sh = (float(a["h"]) + float(b["h"])) or 1
    if abs(dx) / sw >= abs(dy) / sh:
        return ("right", "left") if dx >= 0 else ("left", "right")
    return ("bottom", "top") if dy >= 0 else ("top", "bottom")


def edge_sides(e, nodes):
    a, b = nodes.get(e.get("from")), nodes.get(e.get("to"))
    s1, s2 = auto_sides(a, b)
    if e.get("fromSide") in SIDES:
        s1 = e["fromSide"]
    if e.get("toSide") in SIDES:
        s2 = e["toSide"]
    return s1, s2


def anchor_slots(edges, nodes):
    """Spread several connections on the same side of a block evenly along it."""
    use = {}
    order = []
    for i, e in enumerate(edges):
        if e.get("from") not in nodes or e.get("to") not in nodes or e.get("from") == e.get("to"):
            continue
        s1, s2 = edge_sides(e, nodes)
        use.setdefault((e["from"], s1), []).append((i, 0))
        use.setdefault((e["to"], s2), []).append((i, 1))
        order.append((i, s1, s2))
    slots = {}
    for (nid, side), lst in use.items():
        n = nodes[nid]
        cx, cy = _center(n)
        # sort by where the other end is, so lines don't cross needlessly
        def other_pos(item):
            i, end = item
            e = edges[i]
            o = nodes[e["to"] if end == 0 else e["from"]]
            ox, oy = _center(o)
            return ox if side in ("top", "bottom") else oy
        lst = sorted(lst, key=lambda it: (other_pos(it), it[0]))
        k = len(lst)
        for j, (i, end) in enumerate(lst):
            f = (j + 1) / (k + 1)
            x, y, w, h = float(n["x"]), float(n["y"]), float(n["w"]), float(n["h"])
            if side == "top":
                p = (x + w * f, y)
            elif side == "bottom":
                p = (x + w * f, y + h)
            elif side == "left":
                p = (x, y + h * f)
            else:
                p = (x + w, y + h * f)
            if n.get("type") == "diamond":  # connect diamonds at their tips
                p = {"top": (cx, y), "bottom": (cx, y + h), "left": (x, cy), "right": (x + w, cy)}[side]
            slots[(i, end)] = p
    return order, slots


def _simplify(pts):
    out = []
    for p in pts:
        p = (round(p[0], 2), round(p[1], 2))
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (abs(a[0] - b[0]) < 0.01 and abs(b[0] - c[0]) < 0.01) or (abs(a[1] - b[1]) < 0.01 and abs(b[1] - c[1]) < 0.01):
            out.pop(i)
        else:
            i += 1
    return out


def route(p1, s1, p2, s2, style="ortho", stub=16):
    if style == "straight":
        return [p1, p2]
    d1, d2 = _DIR[s1], _DIR[s2]
    a = (p1[0] + d1[0] * stub, p1[1] + d1[1] * stub)
    b = (p2[0] + d2[0] * stub, p2[1] + d2[1] * stub)
    h1, h2 = s1 in ("left", "right"), s2 in ("left", "right")
    if h1 and h2:
        mx = (a[0] + b[0]) / 2
        pts = [p1, a, (mx, a[1]), (mx, b[1]), b, p2]
    elif not h1 and not h2:
        my = (a[1] + b[1]) / 2
        pts = [p1, a, (a[0], my), (b[0], my), b, p2]
    elif h1:
        pts = [p1, a, (b[0], a[1]), b, p2]
    else:
        pts = [p1, a, (a[0], b[1]), b, p2]
    return _simplify(pts)


def _midpoint(pts):
    lengths = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    half = sum(lengths) / 2
    for i, seg in enumerate(lengths):
        if half <= seg and seg > 0:
            f = half / seg
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f)
        half -= seg
    return pts[len(pts) // 2]


def _arrow(tip, prev, width):
    dx, dy = tip[0] - prev[0], tip[1] - prev[1]
    ln = math.hypot(dx, dy) or 1
    ux, uy = dx / ln, dy / ln
    L, W = 7 + width * 1.6, 3.5 + width * 1.0
    bx, by = tip[0] - ux * L, tip[1] - uy * L
    return [tip, (bx - uy * W, by + ux * W), (bx + uy * W, by - ux * W)]


def edge_primitives(edges, nodes):
    order, slots = anchor_slots(edges, nodes)
    out, labels = [], []
    for i, s1, s2 in order:
        e = edges[i]
        k = EDGE_KINDS.get(e.get("kind"), EDGE_KINDS["signal"])
        p1, p2 = slots[(i, 0)], slots[(i, 1)]
        pts = route(p1, s1, p2, s2, e.get("route", "ortho"))
        arrow = e.get("arrow", "end")
        meta = {"edge": e.get("id", "")}
        line = list(pts)
        heads = []
        if arrow in ("end", "both") and len(pts) >= 2:
            heads.append(_arrow(pts[-1], pts[-2], k["width"]))
            line[-1] = _shorten(pts[-1], pts[-2], 5 + k["width"])
        if arrow in ("start", "both") and len(pts) >= 2:
            heads.append(_arrow(pts[0], pts[1], k["width"]))
            line[0] = _shorten(pts[0], pts[1], 5 + k["width"])
        out.append(("path", poly_path(line, closed=False), None, k["color"], k["width"], k["dash"], meta))
        for hd in heads:
            out.append(("path", poly_path(hd), k["color"], None, 0, "", meta))
        text = (e.get("label") or "").strip()
        if text:
            mx, my = _midpoint(pts)
            size = FONT["edge"]
            lines = text.split("\n")[:3]
            tw = max(text_width(t, size) for t in lines)
            th = len(lines) * size * FONT["line"]
            labels.append(("label_bg", mx - tw / 2 - 4, my - th / 2 - 2, tw + 8, th + 4))
            for j, t in enumerate(lines):
                labels.append(("text", mx, my - th / 2 + (j + 1) * size * FONT["line"] - size * (FONT["line"] - 1) - 1.5,
                               t, size, False, k["color"] if e.get("kind") not in ("signal", None) else "#374151", "middle", meta))
    return out + labels


def _shorten(tip, prev, by):
    dx, dy = tip[0] - prev[0], tip[1] - prev[1]
    ln = math.hypot(dx, dy)
    if ln <= by or ln == 0:
        return tip
    return (tip[0] - dx / ln * by, tip[1] - dy / ln * by)


# --- whole drawing ---------------------------------------------------------------------------------

def clean(data):
    """Validate and normalise diagram JSON from the browser. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("Diagram data must be an object.")
    nodes, edges, seen = [], [], set()
    for n in data.get("nodes", [])[:2000]:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", ""))[:40]
        if not nid or nid in seen:
            continue
        seen.add(nid)
        t = n.get("type") if n.get("type") in SHAPES else "block"
        try:
            x, y = float(n.get("x", 0)), float(n.get("y", 0))
            w, h = max(float(n.get("w", 120)), 20), max(float(n.get("h", 50)), 16)
        except (TypeError, ValueError):
            raise ValueError("A block has an invalid position or size.")
        if not all(math.isfinite(v) and abs(v) < 1e6 for v in (x, y, w, h)):
            raise ValueError("A block is too far away or too large.")
        item = {"id": nid, "type": t, "x": round(x, 2), "y": round(y, 2), "w": round(min(w, 5000), 2), "h": round(min(h, 5000), 2),
                "label": str(n.get("label", ""))[:200], "sub": str(n.get("sub", ""))[:300]}
        if n.get("color") in COLORS:
            item["color"] = n["color"]
        if n.get("notes"):
            item["notes"] = str(n["notes"])[:2000]
        if n.get("ref"):
            item["ref"] = str(n["ref"])[:20]
        nodes.append(item)
    for e in data.get("edges", [])[:4000]:
        if not isinstance(e, dict) or e.get("from") not in seen or e.get("to") not in seen or e.get("from") == e.get("to"):
            continue
        item = {"id": str(e.get("id", ""))[:40] or f"e{len(edges) + 1}", "from": e["from"], "to": e["to"],
                "kind": e.get("kind") if e.get("kind") in EDGE_KINDS else "signal",
                "label": str(e.get("label", ""))[:120],
                "arrow": e.get("arrow") if e.get("arrow") in ("none", "end", "start", "both") else "end",
                "route": "straight" if e.get("route") == "straight" else "ortho"}
        for key in ("fromSide", "toSide"):
            if e.get(key) in SIDES:
                item[key] = e[key]
        edges.append(item)
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    return {"v": 1, "nodes": nodes, "edges": edges, "page": {"legend": bool(page.get("legend", True))}}


def layout(data):
    """All primitives, frames first (behind), then edges, then blocks, then edge labels."""
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    frames = [n for n in data.get("nodes", []) if n.get("type") == "frame"]
    blocks = [n for n in data.get("nodes", []) if n.get("type") != "frame"]
    prims = []
    for n in frames:
        prims.extend(node_primitives(n))
    edge_prims = edge_primitives(data.get("edges", []), nodes)
    prims.extend(p for p in edge_prims if p[0] == "path")
    for n in blocks:
        prims.extend(node_primitives(n))
    prims.extend(p for p in edge_prims if p[0] != "path")
    return prims


def bounds(prims, margin=24):
    xs, ys = [], []
    for p in prims:
        if p[0] == "path":
            for cmd in p[1]:
                coords = cmd[1:]
                xs.extend(coords[0::2])
                ys.extend(coords[1::2])
        elif p[0] == "text":
            w = text_width(p[3], p[4], p[5])
            x0 = p[1] - w / 2 if p[7] == "middle" else p[1]
            xs.extend([x0, x0 + w])
            ys.extend([p[2] - p[4], p[2] + 3])
        elif p[0] == "label_bg":
            xs.extend([p[1], p[1] + p[3]])
            ys.extend([p[2], p[2] + p[4]])
    if not xs:
        return (0, 0, 400, 240)
    return (min(xs) - margin, min(ys) - margin, max(xs) - min(xs) + 2 * margin, max(ys) - min(ys) + 2 * margin)


def legend_items(data):
    used = []
    for e in data.get("edges", []):
        k = e.get("kind", "signal")
        if k not in used:
            used.append(k)
    return [(k, EDGE_KINDS[k]) for k in EDGE_KINDS if k in used]
