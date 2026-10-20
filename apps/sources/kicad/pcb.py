"""Reading and drawing KiCad 6–9 PCBs (.kicad_pcb).

The board is turned into the same layers the Gerber viewer draws (gerber.LayerData), then
design.board.assemble builds the picture, so the PCB opens in the existing board viewer with
its top/bottom/layer views, solder-mask colours and measuring tool. Coordinates are converted
to Y-up like Gerbers. Fonts, pad shapes like trapezoids and custom pads are simplified.
"""
import math
from dataclasses import dataclass, field

from apps.design.board import assemble, label_for
from apps.design.gerber import BBox, Budget, LayerData, escape, f

from .sexpr import KicadError, file_version, find, find_all, has_flag, head, num, parse, points, value, xy

FIRST_VERSION = 20211014   # KiCad 6
NEWEST_KNOWN = 20251231


@dataclass
class Footprint:
    lib_id: str
    x: float
    y: float
    angle: float
    side: str
    uuid: str
    ref: str = ""
    value: str = ""
    pads: list = field(default_factory=list)       # [dict(name, kind, shape, x, y, angle, w, h, layers, drill, net, rratio)]
    graphics: list = field(default_factory=list)   # [(layer, kind, data, width, filled)] in board coordinates
    texts: list = field(default_factory=list)      # [(layer, text, x, y, angle, size)]

    def to_board(self, lx, ly):
        """A point in footprint coordinates on the board (Y down, as in KiCad)."""
        r = math.radians(self.angle)
        c, s = round(math.cos(r), 12), round(math.sin(r), 12)
        return round(self.x + lx * c + ly * s, 6), round(self.y - lx * s + ly * c, 6)


@dataclass
class Board:
    version: int
    thickness: float = 1.6
    layers: dict = field(default_factory=dict)     # name -> type ("signal", "user"…)
    nets: dict = field(default_factory=dict)
    footprints: list = field(default_factory=list)
    tracks: list = field(default_factory=list)     # (layer, x1, y1, x2, y2, width, net)
    arcs: list = field(default_factory=list)       # (layer, start, mid, end, width, net)
    vias: list = field(default_factory=list)       # (x, y, size, drill, net)
    zones: list = field(default_factory=list)      # (layer, net, [points])
    graphics: list = field(default_factory=list)   # (layer, kind, data, width, filled)
    texts: list = field(default_factory=list)      # (layer, text, x, y, angle, size)
    newer: bool = False

    def copper(self):
        names = [n for n in self.layers if n.endswith(".Cu")]
        return sorted(names, key=lambda n: (n != "F.Cu", n == "B.Cu", _inner_no(n)))

    def parts(self):
        out = [{"ref": fp.ref, "value": fp.value, "footprint": fp.lib_id, "uuid": fp.uuid, "side": fp.side,
                "x": round(fp.x, 3), "y": round(fp.y, 3), "angle": fp.angle} for fp in self.footprints if fp.ref]
        from .schematic import _ref_key
        return sorted(out, key=lambda p: _ref_key(p["ref"]))


def _inner_no(name):
    try:
        return int(name[2:-3]) if name.startswith("In") else 0
    except ValueError:
        return 0


# ------------------------------------------------------------------ loading --

def _width(node, default=0.1):
    st = find(node, "stroke")
    w = num(value(st, "width"), 0) if st is not None else num(value(node, "width"), 0)
    return w if w > 0 else default


def _filled(node):
    fill = find(node, "fill")
    return fill is not None and len(fill) > 1 and fill[1] in ("solid", "yes")


def _graphic(node, T):
    """(kind, data) of a gr_*/fp_* shape with points mapped by T, or None."""
    kind = head(node)[3:]
    if kind == "line":
        return "line", [T(*xy(node, "start")[:2]), T(*xy(node, "end")[:2])]
    if kind == "rect":
        (sx, sy), (ex, ey) = xy(node, "start")[:2], xy(node, "end")[:2]
        return "poly", [T(sx, sy), T(ex, sy), T(ex, ey), T(sx, ey)]
    if kind == "circle":
        (cx, cy), (ex, ey) = xy(node, "center")[:2], xy(node, "end")[:2]
        return "circle", [T(cx, cy), math.hypot(ex - cx, ey - cy)]
    if kind == "arc":
        if find(node, "mid") is not None:
            return "arc", [T(*xy(node, k)[:2]) for k in ("start", "mid", "end")]
        return None
    if kind == "poly":
        pts = [T(x, y) for x, y in points(node)]
        return ("poly", pts) if len(pts) >= 2 else None
    if kind == "curve":
        pts = [T(x, y) for x, y in points(node)]
        return ("curve", pts) if len(pts) == 4 else None
    return None


def _text_size(node):
    eff = find(node, "effects")
    font = find(eff, "font") if eff is not None else None
    size = find(font, "size") if font is not None else None
    return num(size[1], 1.0) if size is not None and len(size) > 1 else 1.0


def _hidden(node):
    eff = find(node, "effects")
    return has_flag(node, "hide") or (eff is not None and has_flag(eff, "hide"))


def _footprint(node):
    x, y, a = xy(node)
    layer = str(value(node, "layer", "F.Cu"))
    fp = Footprint(lib_id=str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else "", x=x, y=y, angle=a,
                   side="bottom" if layer.startswith("B.") else "top",
                   uuid=str(value(node, "uuid", "") or value(node, "tstamp", "") or ""))
    T = fp.to_board
    for p in find_all(node, "property"):  # KiCad 8+
        if len(p) > 2 and p[1] in ("Reference", "Value"):
            setattr(fp, "ref" if p[1] == "Reference" else "value", str(p[2]))
            if not _hidden(p):
                tx, ty, ta = xy(p)
                fp.texts.append((str(value(p, "layer", "")), str(p[2]), *T(tx, ty), ta, _text_size(p)))
    for t in find_all(node, "fp_text"):  # KiCad 6–7
        if len(t) > 2:
            if t[1] == "reference":
                fp.ref = str(t[2])
            elif t[1] == "value":
                fp.value = str(t[2])
            if not _hidden(t) and t[1] != "value":
                tx, ty, ta = xy(t)
                text = fp.ref if t[1] == "reference" else str(t[2])
                fp.texts.append((str(value(t, "layer", "")), text, *T(tx, ty), ta, _text_size(t)))
    for g in node[1:]:
        if isinstance(g, list) and head(g) in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve"):
            shape = _graphic(g, T)
            if shape:
                fp.graphics.append((str(value(g, "layer", "")), shape[0], shape[1], _width(g), _filled(g)))
    for pad in find_all(node, "pad"):
        px, py, pa = xy(pad)
        size = find(pad, "size")
        w, h = (num(size[1]), num(size[2])) if size is not None and len(size) > 2 else (0.0, 0.0)
        drill = find(pad, "drill")
        d = None
        if drill is not None:
            vals = [v for v in drill[1:] if not isinstance(v, list)]
            if vals and vals[0] == "oval":
                d = ("oval", num(vals[1]) if len(vals) > 1 else 0, num(vals[2]) if len(vals) > 2 else num(vals[1]) if len(vals) > 1 else 0)
            elif vals:
                d = ("circle", num(vals[0]), num(vals[0]))
        layers = find(pad, "layers")
        net = find(pad, "net")
        bx, by = T(px, py)
        fp.pads.append({"name": str(pad[1]) if len(pad) > 1 else "", "kind": str(pad[2]) if len(pad) > 2 else "smd",
                        "shape": str(pad[3]) if len(pad) > 3 else "rect", "x": bx, "y": by, "angle": pa, "w": w, "h": h,
                        "layers": [str(v) for v in layers[1:]] if layers is not None else [], "drill": d,
                        "net": str(net[2]) if net is not None and len(net) > 2 else "",
                        "rratio": num(value(pad, "roundrect_rratio"), 0.25)})
    return fp


def load_pcb(text):
    tree = parse(text)
    if head(tree) != "kicad_pcb":
        raise KicadError("This isn't a KiCad PCB.")
    version = file_version(tree)
    if version and version < FIRST_VERSION:
        raise KicadError("This KiCad version isn't supported for previews. Open and save it in KiCad 6 or newer.")
    b = Board(version=version, newer=version > NEWEST_KNOWN)
    general = find(tree, "general")
    if general is not None:
        b.thickness = num(value(general, "thickness"), 1.6) or 1.6
    for layer in find(tree, "layers")[1:] if find(tree, "layers") is not None else []:
        if isinstance(layer, list) and len(layer) > 2:
            b.layers[str(layer[1])] = str(layer[2])
    for net in find_all(tree, "net"):
        if len(net) > 2:
            b.nets[int(num(net[1]))] = str(net[2])
    for node in find_all(tree, "footprint") + find_all(tree, "module"):
        b.footprints.append(_footprint(node))
    for seg in find_all(tree, "segment"):
        b.tracks.append((str(value(seg, "layer", "")), *xy(seg, "start")[:2], *xy(seg, "end")[:2], _width(seg, 0.2),
                         int(num(value(seg, "net"), 0))))
    for arc in find_all(tree, "arc"):
        b.arcs.append((str(value(arc, "layer", "")), xy(arc, "start")[:2], xy(arc, "mid")[:2], xy(arc, "end")[:2],
                       _width(arc, 0.2), int(num(value(arc, "net"), 0))))
    for via in find_all(tree, "via"):
        x, y, _ = xy(via)
        b.vias.append((x, y, num(value(via, "size"), 0.6), num(value(via, "drill"), 0.3), int(num(value(via, "net"), 0))))
    for zone in find_all(tree, "zone"):
        net = int(num(value(zone, "net"), 0))
        for fpoly in find_all(zone, "filled_polygon"):
            pts = points(fpoly)
            if len(pts) >= 3:
                b.zones.append((str(value(fpoly, "layer", value(zone, "layer", ""))), net, pts))
    ident = lambda x, y: (x, y)  # noqa: E731
    for g in tree[1:]:
        if isinstance(g, list) and head(g) in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve"):
            shape = _graphic(g, ident)
            if shape:
                b.graphics.append((str(value(g, "layer", "")), shape[0], shape[1], _width(g), _filled(g)))
        elif isinstance(g, list) and head(g) == "gr_text" and len(g) > 1 and not _hidden(g):
            x, y, a = xy(g)
            b.texts.append((str(value(g, "layer", "")), str(g[1]), x, y, a, _text_size(g)))
    return b


# ------------------------------------------------------------------ drawing --

LAYER_KIND = {"Cu": "copper", "Mask": "mask", "Paste": "paste", "SilkS": "silk"}


def layer_kind(name):
    """(kind, side, n) for a KiCad layer name, matching the Gerber viewer's layer kinds."""
    if name == "Edge.Cuts":
        return "outline", None, 0
    prefix, _, kind = name.partition(".")
    k = LAYER_KIND.get(kind)
    if k is None:
        return "other", None, 0
    if prefix == "F":
        return k, "top", 1
    if prefix == "B":
        return k, "bottom", 99
    if prefix.startswith("In") and k == "copper":
        return "copper", "inner", _inner_no(name) + 1
    return "other", None, 0


def _xyup(x, y):
    return x, -y


def _arc_d(p1, p2, p3):
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return f"M{f(x1)} {f(y1)}L{f(x3)} {f(y3)}", None
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    cross = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    sweep = 1 if cross > 0 else 0
    a1, a3 = math.atan2(y1 - uy, x1 - ux), math.atan2(y3 - uy, x3 - ux)
    span = (a3 - a1) % (2 * math.pi) if sweep else (a1 - a3) % (2 * math.pi)
    return f"M{f(x1)} {f(y1)}A{f(r)} {f(r)} 0 {1 if span > math.pi else 0} {sweep} {f(x3)} {f(y3)}", (ux, uy, r)


class _Layer:
    def __init__(self, name):
        self.data = LayerData(name)
        self.els = []

    def add(self, el, *pts, r=0.0):
        self.els.append(el)
        for x, y in pts:
            self.data.bbox.add(x, y, r)


def _pad_shape(pad, grow=0.0):
    """SVG element for a pad (Y up), plus its bounding radius."""
    x, y = _xyup(pad["x"], pad["y"])
    w, h = pad["w"] + 2 * grow, pad["h"] + 2 * grow
    rot = f' transform="rotate({f(pad["angle"])} {f(x)} {f(y)})"' if pad["angle"] % 360 else ""
    shape = pad["shape"]
    if shape == "circle":
        return f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(w / 2)}"/>', max(w, h) / 2
    if shape == "oval":
        if abs(w - h) < 1e-9:
            return f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(w / 2)}"/>', w / 2
        if w > h:
            d = f"M{f(x - (w - h) / 2)} {f(y)}H{f(x + (w - h) / 2)}"
            thick = h
        else:
            d = f"M{f(x)} {f(y - (h - w) / 2)}V{f(y + (h - w) / 2)}"
            thick = w
        return f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="{f(thick)}" stroke-linecap="round"{rot}/>', max(w, h) / 2
    rx = min(w, h) * pad["rratio"] if shape == "roundrect" else 0
    rxa = f' rx="{f(rx)}"' if rx else ""
    return (f'<rect x="{f(x - w / 2)}" y="{f(y - h / 2)}" width="{f(w)}" height="{f(h)}"{rxa}{rot}/>',
            math.hypot(w, h) / 2)


def _pad_layers(pad, names):
    out = set()
    for spec in pad["layers"]:
        if spec.startswith("*."):
            suffix = spec[1:]
            out |= {n for n in names if n.endswith(suffix)}
        elif spec == "F&B.Cu":
            out |= {"F.Cu", "B.Cu"}
        else:
            out.add(spec)
    return out


def _shape_el(kind, data, width, filled):
    """SVG element for a graphic in Y-up coordinates, plus the points for the bounding box."""
    pts = [_xyup(*p) for p in data] if kind != "circle" else None
    sw = f'stroke="currentColor" stroke-width="{f(width)}" stroke-linecap="round" stroke-linejoin="round"'
    if kind == "line":
        (a, b), (c, d) = pts
        return f'<path d="M{f(a)} {f(b)}L{f(c)} {f(d)}" fill="none" {sw}/>', pts
    if kind == "circle":
        (cx, cy), r = _xyup(*data[0]), data[1]
        fill = "currentColor" if filled else "none"
        return f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r)}" fill="{fill}" {sw}/>', [(cx - r, cy - r), (cx + r, cy + r)]
    if kind == "arc":
        d, _ = _arc_d(*pts)
        return f'<path d="{d}" fill="none" {sw}/>', pts
    if kind == "poly":
        d = "M" + "L".join(f"{f(a)} {f(b)}" for a, b in pts) + "Z"
        return f'<path d="{d}" fill="{"currentColor" if filled else "none"}" {sw}/>', pts
    if kind == "curve":
        d = f"M{f(pts[0][0])} {f(pts[0][1])}C" + " ".join(f"{f(a)} {f(b)}" for a, b in pts[1:])
        return f'<path d="{d}" fill="none" {sw}/>', pts
    return "", []


def _text_el(text, x, y, angle, size):
    x, y = _xyup(x, y)
    return (f'<text transform="translate({f(x)} {f(y)}) scale(1 -1) rotate({f(-angle)})" font-size="{f(size)}" '
            f'font-family="Liberation Sans, Arial, sans-serif" text-anchor="middle" dominant-baseline="central" '
            f'fill="currentColor">{escape(text)}</text>')


def _outline(board):
    """Board shape as an SVG path (Y up) from Edge.Cuts, and its BBox. Loose lines and arcs are joined into loops."""
    frame, closed, pieces = BBox(), [], []
    items = [(k, d) for layer, k, d, w, fl in board.graphics if layer == "Edge.Cuts"]
    for fp in board.footprints:
        items += [(k, d) for layer, k, d, w, fl in fp.graphics if layer == "Edge.Cuts"]
    for kind, data in items:
        if kind == "circle":
            (cx, cy), r = _xyup(*data[0]), data[1]
            closed.append(f"M{f(cx - r)} {f(cy)}A{f(r)} {f(r)} 0 1 0 {f(cx + r)} {f(cy)}A{f(r)} {f(r)} 0 1 0 {f(cx - r)} {f(cy)}Z")
            frame.add(cx - r, cy - r)
            frame.add(cx + r, cy + r)
        elif kind == "poly":
            pts = [_xyup(*p) for p in data]
            closed.append("M" + "L".join(f"{f(a)} {f(b)}" for a, b in pts) + "Z")
            for p in pts:
                frame.add(*p)
        elif kind in ("line", "arc"):
            pts = [_xyup(*p) for p in data]
            pieces.append((kind, pts))
            for p in pts:
                frame.add(*p)
    # join loose pieces end to end
    tol = 0.01
    while pieces:
        kind, pts = pieces.pop(0)
        chain = [(kind, pts)]
        start, end = pts[0], pts[-1]
        grown = True
        while grown and math.hypot(end[0] - start[0], end[1] - start[1]) > tol:
            grown = False
            for i, (k2, p2) in enumerate(pieces):
                if math.hypot(p2[0][0] - end[0], p2[0][1] - end[1]) <= tol:
                    chain.append((k2, p2))
                elif math.hypot(p2[-1][0] - end[0], p2[-1][1] - end[1]) <= tol:
                    chain.append((k2, list(reversed(p2))))
                else:
                    continue
                end = chain[-1][1][-1]
                pieces.pop(i)
                grown = True
                break
        if math.hypot(end[0] - start[0], end[1] - start[1]) > tol:
            continue  # not a closed loop
        d = f"M{f(start[0])} {f(start[1])}"
        for k, p in chain:
            if k == "line":
                d += f"L{f(p[-1][0])} {f(p[-1][1])}"
            else:
                arc, _ = _arc_d(*p)
                d += arc[arc.index("A"):] if "A" in arc else f"L{f(p[-1][0])} {f(p[-1][1])}"
        closed.append(d + "Z")
    return "".join(closed), frame


def pcb_layers(board, budget=None):
    budget = budget or Budget()
    names = list(board.layers) or ["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
    copper = board.copper()
    layers = {}

    def L(name):
        if name not in layers:
            layers[name] = _Layer(name)
        return layers[name]

    drill = _Layer("Drill holes")
    drill.data.is_drill = True
    drill.data.stats.update({"plated": True, "holes": 0, "slots": 0, "tools": {}})
    traces = []
    for layer, x1, y1, x2, y2, w, net in board.tracks:
        budget.add()
        a, b = _xyup(x1, y1), _xyup(x2, y2)
        L(layer).add(f'<path d="M{f(a[0])} {f(a[1])}L{f(b[0])} {f(b[1])}" fill="none" stroke="currentColor" stroke-width="{f(w)}" '
                     'stroke-linecap="round"/>', a, b, r=w / 2)
        traces.append((layer, w))
    for layer, s, m, e, w, net in board.arcs:
        budget.add()
        pts = [_xyup(*p) for p in (s, m, e)]
        d, _ = _arc_d(*pts)
        L(layer).add(f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="{f(w)}" stroke-linecap="round"/>', *pts, r=w / 2)
        traces.append((layer, w))
    for layer, net, pts in board.zones:
        budget.add(len(pts) // 50 + 1)
        up = [_xyup(*p) for p in pts]
        L(layer).add('<path d="M' + "L".join(f"{f(a)} {f(b)}" for a, b in up) + 'Z" fill="currentColor"/>', *up)
    for x, y, size, dr, net in board.vias:
        budget.add()
        cx, cy = _xyup(x, y)
        for name in copper:
            L(name).add(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(size / 2)}"/>', (cx, cy), r=size / 2)
        drill.add(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(dr / 2)}"/>', (cx, cy), r=dr / 2)
        drill.data.stats["holes"] += 1
        drill.data.stats["tools"][round(dr, 3)] = drill.data.stats["tools"].get(round(dr, 3), 0) + 1
    for layer, kind, data, w, filled in board.graphics:
        budget.add()
        el, pts = _shape_el(kind, data, w, filled)
        if el:
            L(layer).add(el, *pts, r=w / 2)
    for layer, text, x, y, a, size in board.texts:
        L(layer).add(_text_el(text, x, y, a, size), _xyup(x, y))
    for fp in board.footprints:
        for layer, kind, data, w, filled in fp.graphics:
            budget.add()
            el, pts = _shape_el(kind, data, w, filled)
            if el:
                L(layer).add(el, *pts, r=w / 2)
        for layer, text, x, y, a, size in fp.texts:
            if layer:
                L(layer).add(_text_el(text, x, y, a, size), _xyup(x, y))
        for pad in fp.pads:
            budget.add()
            on = _pad_layers(pad, names)
            for name in on:
                el, r = _pad_shape(pad)
                L(name).add(el, _xyup(pad["x"], pad["y"]), r=r)
                if name in copper:
                    L(name).data.stats["flashes"] += 1
            if pad["drill"] and pad["kind"] in ("thru_hole", "np_thru_hole"):
                shape, dw, dh = pad["drill"]
                hole = dict(pad, shape="circle" if shape == "circle" else "oval", w=dw, h=dh or dw)
                el, r = _pad_shape(hole)
                drill.add(el, _xyup(pad["x"], pad["y"]), r=r)
                drill.data.stats["holes"] += 1
                if shape == "oval" and abs(dw - (dh or dw)) > 1e-9:
                    drill.data.stats["slots"] += 1
                drill.data.stats["tools"][round(dw, 3)] = drill.data.stats["tools"].get(round(dw, 3), 0) + 1

    out, warnings = [], []
    for i, (name, lay) in enumerate(sorted(layers.items(), key=lambda kv: kv[0])):
        if not lay.els or lay.data.bbox.empty:
            continue
        kind, side, n = layer_kind(name)
        d = lay.data
        d.kind, d.side, d.n = kind, side, n
        d.label = label_for(kind, side, n) if kind != "other" else name
        d.segments = [(True, lay.els)]
        widths = [w for layer, w in traces if layer == name]
        d.stats["min_trace"] = min(widths) if widths else None
        if kind == "outline":
            d.center_bbox.merge(d.bbox)
        out.append(d)
    if drill.els:
        d = drill.data
        d.kind, d.side, d.n, d.label = "drill", None, 0, "Drill holes"
        d.segments = [(True, drill.els)]
        out.append(d)
    for i, d in enumerate(out):
        d.index = i
    shape, frame = _outline(board)
    if not shape or frame.empty:
        frame = BBox()
        for d in out:
            if d.kind in ("copper", "silk", "mask", "drill"):
                frame.merge(d.bbox)
        if frame.empty:
            for d in out:
                frame.merge(d.bbox)
        if frame.empty:
            frame.add(0, 0)
            frame.add(10, 10)
        warnings.append("No closed board outline (Edge.Cuts) was found, so the board is drawn as a rectangle.")
        shape = f"M{f(frame.minx)} {f(frame.miny)}H{f(frame.maxx)}V{f(frame.maxy)}H{f(frame.minx)}Z"
    return out, shape, frame, warnings


HIGHLIGHT_COLOURS = {"added": "#2e9e44", "removed": "#d23c3c", "changed": "#e09a1f"}


def _overlay(board, highlight):
    """Boxes around highlighted footprints, in Y-up coordinates."""
    out = []
    for fp in board.footprints:
        state = highlight.get(fp.uuid)
        if not state:
            continue
        box = BBox()
        for pad in fp.pads:
            box.add(*_xyup(pad["x"], pad["y"]), max(pad["w"], pad["h"]) / 2)
        for layer, kind, data, w, filled in fp.graphics:
            if "CrtYd" in layer or "SilkS" in layer or "Fab" in layer:
                for p in (data if kind != "circle" else [data[0]]):
                    box.add(*_xyup(*p))
        if box.empty:
            box.add(*_xyup(fp.x, fp.y), 1.0)
        m = 0.4
        colour = HIGHLIGHT_COLOURS.get(state, "#e09a1f")
        out.append(f'<rect class="hl-{state}" x="{f(box.minx - m)}" y="{f(box.miny - m)}" width="{f(box.width + 2 * m)}" '
                   f'height="{f(box.height + 2 * m)}" fill="{colour}" fill-opacity="0.25" stroke="{colour}" stroke-width="0.25"/>')
    return "".join(out)


def render_pcb(text, prefix="k", highlight=None, board=None):
    """The board picture and facts, with the same keys as design.board.render_board."""
    board = board or load_pcb(text)
    budget = Budget()
    layers, shape, frame, warnings = pcb_layers(board, budget)
    if not layers:
        raise KicadError("The PCB has nothing to draw yet.")
    if board.newer:
        warnings.append("Made with a newer KiCad than Workbench knows; some details may be missing.")
    result = assemble(layers, shape, frame, warnings, {"GeneralSpecs": {"BoardThickness": board.thickness}}, prefix=prefix,
                      has_outline=not any("outline" in w for w in warnings), overlay=_overlay(board, highlight or {}))
    result["info"]["copper_layers"] = len([d for d in layers if d.kind == "copper"]) or None
    result["info"]["vias"] = len(board.vias)
    result["info"]["parts_top"] = sum(1 for fp in board.footprints if fp.side == "top" and fp.ref)
    result["info"]["parts_bottom"] = sum(1 for fp in board.footprints if fp.side == "bottom" and fp.ref)
    result["parts"] = board.parts()
    result["unused"] = []
    return result
