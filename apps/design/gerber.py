"""Reading Gerber (RS-274X / X2) and Excellon drill files, and drawing them as SVG.

Everything here is plain Python with no third-party libraries. The output is
SVG markup built only from numbers and fixed tag names (file names are
escaped), so it's safe to show inline.

Coordinates are converted to millimetres and kept in Gerber orientation
(Y up); the caller flips the finished drawing with scale(1,-1).
"""
import html
import math
import re

# ------------------------------------------------------------------------ helpers --


class GerberError(ValueError):
    pass


def f(v):
    """Compact number formatting for SVG (0.1 µm resolution)."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class BBox:
    def __init__(self):
        self.minx = self.miny = math.inf
        self.maxx = self.maxy = -math.inf

    def add(self, x, y, r=0.0):
        self.minx, self.maxx = min(self.minx, x - r), max(self.maxx, x + r)
        self.miny, self.maxy = min(self.miny, y - r), max(self.maxy, y + r)

    def merge(self, other):
        if other and not other.empty:
            self.add(other.minx, other.miny)
            self.add(other.maxx, other.maxy)

    @property
    def empty(self):
        return self.minx == math.inf

    @property
    def width(self):
        return 0 if self.empty else self.maxx - self.minx

    @property
    def height(self):
        return 0 if self.empty else self.maxy - self.miny

    def as_list(self):
        return None if self.empty else [self.minx, self.miny, self.maxx, self.maxy]


def arc_points(sx, sy, ex, ey, cx, cy, ccw):
    """Angle span and the axis-extreme points an arc passes through (for bounding boxes)."""
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    span = (a1 - a0) % (2 * math.pi) if ccw else (a0 - a1) % (2 * math.pi)
    if span < 1e-9 and math.hypot(ex - sx, ey - sy) < 1e-9:
        span = 2 * math.pi  # start == end: a full circle
    r = math.hypot(sx - cx, sy - cy)
    pts = []
    for k in range(4):
        ang = k * math.pi / 2
        d = (ang - a0) % (2 * math.pi) if ccw else (a0 - ang) % (2 * math.pi)
        if d <= span:
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return span, r, pts


def svg_arc(sx, sy, ex, ey, cx, cy, ccw):
    """SVG path commands for an arc from the current point (sx, sy)."""
    span, r, _ = arc_points(sx, sy, ex, ey, cx, cy, ccw)
    sweep = 1 if ccw else 0
    if r < 1e-9:
        return f"L{f(ex)} {f(ey)}"
    if span >= 2 * math.pi - 1e-9:  # full circle: two half arcs
        mx, my = 2 * cx - sx, 2 * cy - sy
        return (f"A{f(r)} {f(r)} 0 0 {sweep} {f(mx)} {f(my)}"
                f"A{f(r)} {f(r)} 0 0 {sweep} {f(ex)} {f(ey)}")
    large = 1 if span > math.pi else 0
    return f"A{f(r)} {f(r)} 0 {large} {sweep} {f(ex)} {f(ey)}"


def rotate(x, y, deg):
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def poly_path(points):
    return "M" + "L".join(f"{f(x)} {f(y)}" for x, y in points) + "Z"


def circle_path(cx, cy, r):
    return (f"M{f(cx - r)} {f(cy)}A{f(r)} {f(r)} 0 1 0 {f(cx + r)} {f(cy)}"
            f"A{f(r)} {f(r)} 0 1 0 {f(cx - r)} {f(cy)}Z")


# ------------------------------------------------------------- macro arithmetic --

_TOKEN = re.compile(r"\s*(\$\d+|\d*\.\d+|\d+\.?|[-+xX/()])")


def evaluate(expr, variables):
    """Evaluate a Gerber macro expression ($1+$2x0.5 …) without eval()."""
    tokens = []
    pos, expr = 0, expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise GerberError(f"Can't read macro expression {expr!r}")
        tokens.append(m.group(1))
        pos = m.end()
    tokens.append(None)
    i = 0

    def peek():
        return tokens[i]

    def take():
        nonlocal i
        i += 1
        return tokens[i - 1]

    def atom():
        t = take()
        if t == "(":
            v = add()
            take()
            return v
        if t == "-":
            return -atom()
        if t == "+":
            return atom()
        if t and t.startswith("$"):
            return variables.get(int(t[1:]), 0.0)
        try:
            return float(t)
        except (TypeError, ValueError):
            raise GerberError(f"Can't read macro expression {expr!r}")

    def mul():
        v = atom()
        while peek() in ("x", "X", "/"):
            op = take()
            rhs = atom()
            v = v * rhs if op in ("x", "X") else (v / rhs if rhs else 0.0)
        return v

    def add():
        v = mul()
        while peek() in ("+", "-"):
            op = take()
            rhs = mul()
            v = v + rhs if op == "+" else v - rhs
        return v

    return add()


# --------------------------------------------------------------------- apertures --

class Aperture:
    """An aperture as SVG shapes centred on (0, 0), in mm."""

    def __init__(self, shapes, radius, stroke_width=None, round_=True):
        self.shapes = shapes          # list of SVG element strings
        self.radius = radius          # for bounding boxes
        self.stroke_width = stroke_width
        self.round = round_


def standard_aperture(kind, params, scale):
    p = [v * scale for v in params]
    hole = []
    if kind == "C":
        d = p[0] if p else 0.0
        hole = p[1:3]
        shape = circle_path(0, 0, d / 2)
        el = f'<circle r="{f(d / 2)}"/>' if not hole else None
        radius, sw, rnd = d / 2, d, True
    elif kind in ("R", "O"):
        w, h = (p + [0, 0])[:2]
        hole = p[2:4]
        r = min(w, h) / 2 if kind == "O" else 0
        if kind == "R":
            shape = poly_path([(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)])
        else:
            shape = rounded_rect(w, h, r)
        el = None if hole else (f'<rect x="{f(-w / 2)}" y="{f(-h / 2)}" width="{f(w)}" height="{f(h)}"'
                                + (f' rx="{f(r)}"' if r else "") + "/>")
        radius, sw, rnd = math.hypot(w, h) / 2, min(w, h), kind == "O"
    elif kind == "P":
        d = p[0] if p else 0.0
        n = int(params[1]) if len(params) > 1 else 3
        rot = params[2] if len(params) > 2 else 0.0
        hole = p[3:5]
        pts = [rotate(d / 2, 0, rot + 360.0 * k / max(n, 3)) for k in range(max(n, 3))]
        shape = poly_path(pts)
        el = None if hole else f'<path d="{shape}"/>'
        radius, sw, rnd = d / 2, d, True
    else:
        raise GerberError(f"Unknown aperture type {kind}")
    if hole:
        if len(hole) == 1:
            cut = circle_path(0, 0, hole[0] / 2)
        else:
            hw, hh = hole[0] / 2, hole[1] / 2
            cut = poly_path([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])
        el = f'<path fill-rule="evenodd" d="{shape}{cut}"/>'
    return Aperture([el], radius, sw, rnd)


def rounded_rect(w, h, r):
    x0, y0, x1, y1 = -w / 2, -h / 2, w / 2, h / 2
    if r <= 0:
        return poly_path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    return (f"M{f(x0 + r)} {f(y0)}L{f(x1 - r)} {f(y0)}A{f(r)} {f(r)} 0 0 1 {f(x1)} {f(y0 + r)}"
            f"L{f(x1)} {f(y1 - r)}A{f(r)} {f(r)} 0 0 1 {f(x1 - r)} {f(y1)}"
            f"L{f(x0 + r)} {f(y1)}A{f(r)} {f(r)} 0 0 1 {f(x0)} {f(y1 - r)}"
            f"L{f(x0)} {f(y0 + r)}A{f(r)} {f(r)} 0 0 1 {f(x0 + r)} {f(y0)}Z")


def thermal_path(cx, cy, do, di, gap, rot):
    """A thermal relief: a ring with a cross-shaped gap, as four closed sectors."""
    ro, ri, g = do / 2, max(di / 2, 0), gap / 2
    if g >= ro:
        return ""
    out = []
    for k in range(4):
        def pt(x, y):
            x2, y2 = rotate(x, y, rot + 90 * k)
            return f"{f(cx + x2)} {f(cy + y2)}"
        xo = math.sqrt(ro * ro - g * g)
        d = f"M{pt(xo, g)}A{f(ro)} {f(ro)} 0 0 1 {pt(g, xo)}"
        if ri > g * math.sqrt(2):
            xi = math.sqrt(ri * ri - g * g)
            d += f"L{pt(g, xi)}A{f(ri)} {f(ri)} 0 0 0 {pt(xi, g)}Z"
        else:
            d += f"L{pt(g, g)}Z"
        out.append(d)
    return "".join(out)


def macro_aperture(blocks, params, scale, warnings, ap_id="m"):
    """Build an aperture from an aperture macro's primitives.

    Primitives with exposure off cut out what was drawn before them; that's
    done with an SVG mask, defined next to the aperture.
    """
    variables = {i + 1: v for i, v in enumerate(params)}
    shapes, radius = [], 0.0
    layers = []   # (exposure, element) in order
    for block in blocks:
        block = block.strip()
        if not block or re.match(r"0(\s|,|$)", block):  # comment
            continue
        if block.startswith("$"):
            name, _, expr = block.partition("=")
            variables[int(name[1:])] = evaluate(expr, variables)
            continue
        parts = [x for x in block.split(",")]
        try:
            code = int(parts[0])
        except ValueError:
            continue
        if code == 0:
            continue
        vals = [evaluate(x, variables) for x in parts[1:] if x.strip() != ""]
        if not vals:
            continue
        exposure = vals[0]
        el, r = None, 0.0
        s = scale
        if code == 1:  # circle: exposure, diameter, x, y[, rotation]
            d, x, y = vals[1] * s, vals[2] * s, vals[3] * s
            x, y = rotate(x, y, vals[4] if len(vals) > 4 else 0)
            el, r = f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(d / 2)}"/>', math.hypot(x, y) + d / 2
        elif code in (2, 20):  # vector line: exposure, width, x1, y1, x2, y2, rotation
            w, x1, y1, x2, y2 = (v * s for v in vals[1:6])
            rot = vals[6] if len(vals) > 6 else 0
            ang = math.atan2(y2 - y1, x2 - x1)
            dx, dy = -math.sin(ang) * w / 2, math.cos(ang) * w / 2
            pts = [rotate(px, py, rot) for px, py in ((x1 + dx, y1 + dy), (x2 + dx, y2 + dy), (x2 - dx, y2 - dy), (x1 - dx, y1 - dy))]
            el, r = f'<path d="{poly_path(pts)}"/>', max(math.hypot(*p) for p in pts)
        elif code == 21:  # centre line: exposure, width, height, cx, cy, rotation
            w, h, cx, cy = (v * s for v in vals[1:5])
            rot = vals[5] if len(vals) > 5 else 0
            pts = [rotate(cx + px, cy + py, rot) for px, py in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
            el, r = f'<path d="{poly_path(pts)}"/>', max(math.hypot(*p) for p in pts)
        elif code == 22:  # lower-left line (old): exposure, width, height, x, y, rotation
            w, h, x, y = (v * s for v in vals[1:5])
            rot = vals[5] if len(vals) > 5 else 0
            pts = [rotate(x + px, y + py, rot) for px, py in ((0, 0), (w, 0), (w, h), (0, h))]
            el, r = f'<path d="{poly_path(pts)}"/>', max(math.hypot(*p) for p in pts)
        elif code == 4:  # outline: exposure, n, x0, y0, … xn, yn, rotation
            n = int(vals[1])
            coords = vals[2:2 + 2 * (n + 1)]
            rot = vals[2 + 2 * (n + 1)] if len(vals) > 2 + 2 * (n + 1) else 0
            pts = [rotate(coords[k] * s, coords[k + 1] * s, rot) for k in range(0, len(coords) - 1, 2)]
            if len(pts) >= 3:
                el, r = f'<path d="{poly_path(pts)}"/>', max(math.hypot(*p) for p in pts)
        elif code == 5:  # polygon: exposure, vertices, cx, cy, diameter, rotation
            n, cx, cy, d = int(vals[1]), vals[2] * s, vals[3] * s, vals[4] * s
            rot = vals[5] if len(vals) > 5 else 0
            pts = [rotate(cx + d / 2 * math.cos(2 * math.pi * k / n), cy + d / 2 * math.sin(2 * math.pi * k / n), rot) for k in range(max(n, 3))]
            el, r = f'<path d="{poly_path(pts)}"/>', max(math.hypot(*p) for p in pts)
        elif code == 6:  # moiré: cx, cy, outer d, ring thickness, gap, rings, cross thickness, cross length, rotation
            cx, cy, d, t, gap = (v * s for v in vals[0:5])
            rings = int(vals[5])
            ct, cl = vals[6] * s, vals[7] * s
            rot = vals[8] if len(vals) > 8 else 0
            cx, cy = rotate(cx, cy, rot)
            parts_ = []
            for k in range(rings):
                ro = d / 2 - k * (t + gap)
                ri = ro - t
                if ro <= 0:
                    break
                parts_.append(f'<path fill-rule="evenodd" d="{circle_path(cx, cy, ro)}{circle_path(cx, cy, max(ri, 0))}"/>')
            for ang in (0, 90):
                pts = [rotate(px, py, rot + ang) for px, py in ((-cl / 2, -ct / 2), (cl / 2, -ct / 2), (cl / 2, ct / 2), (-cl / 2, ct / 2))]
                parts_.append(f'<path d="{poly_path([(cx + px, cy + py) for px, py in pts])}"/>')
            layers.extend((1, e) for e in parts_)
            radius = max(radius, math.hypot(cx, cy) + max(d, cl) / 2)
            continue
        elif code == 7:  # thermal: cx, cy, outer d, inner d, gap, rotation (drawn as a ring with gaps)
            cx, cy, do, di, gap = (v * s for v in vals[0:5])
            rot = vals[5] if len(vals) > 5 else 0
            cx, cy = rotate(cx, cy, rot)
            d = thermal_path(cx, cy, do, di, gap, rot)
            if d:
                layers.append((1, f'<path d="{d}"/>'))
            radius = max(radius, math.hypot(cx, cy) + do / 2)
            continue
        else:
            warnings.add(f"Macro primitive {code} isn't supported")
            continue
        if el is None:
            continue
        layers.append((1 if exposure else 0, el))
        radius = max(radius, r)
    body, defs = "", []
    R = radius * 1.05 + 0.01
    box = f'x="{f(-R)}" y="{f(-R)}" width="{f(2 * R)}" height="{f(2 * R)}"'
    for k, (exposure, el) in enumerate(layers):
        if exposure:
            body += el
        elif body:
            mid = f"{ap_id}x{k}"
            defs.append(f'<mask id="{mid}" maskUnits="userSpaceOnUse" {box}><rect {box} fill="#fff"/><g fill="#000">{el}</g></mask>')
            body = f'<g mask="url(#{mid})">{body}</g>'
    shapes = defs + [body or '<circle r="0"/>']
    return Aperture(shapes, radius, None, True)


# ------------------------------------------------------------------------- layers --

class LayerData:
    def __init__(self, filename):
        self.filename = filename
        self.segments = []            # [(dark: bool, [svg elements])]
        self.defs = []                # aperture symbols
        self.bbox = BBox()
        self.center_bbox = BBox()      # without stroke widths (board size from the outline)
        self.outline_parts = []       # (kind, points…) for board-shape detection
        self.function = ""            # X2 .FileFunction
        self.warnings = set()
        self.stats = {"flashes": 0, "draws": 0, "regions": 0, "min_trace": None, "holes": 0, "slots": 0, "tools": {}}
        self.is_drill = False


class _Segment:
    """Everything drawn with one polarity, grouped for compact SVG."""

    def __init__(self, dark):
        self.dark = dark
        self.strokes = {}   # (width, round) -> [path data]
        self.flashes = []   # (aperture id, x, y)
        self.regions = []   # path data
        self.extra = []     # already-built elements

    def empty(self):
        return not (self.strokes or self.flashes or self.regions or self.extra)

    def elements(self, prefix):
        out = []
        for d in self.regions:
            out.append(f'<path d="{d}"/>')
        for (w, rnd), ds in self.strokes.items():
            cap = "round" if rnd else "square"
            out.append(f'<path fill="none" stroke="currentColor" stroke-width="{f(w)}" stroke-linecap="{cap}" '
                       f'stroke-linejoin="round" d="{"".join(ds)}"/>')
        for ap, x, y in self.flashes:
            out.append(f'<use href="#{prefix}a{ap}" x="{f(x)}" y="{f(y)}"/>')
        out.extend(self.extra)
        return out


def _tokens(text):
    """Yield ('ext', [blocks]) for %…% commands and ('word', str) for ordinary ones."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "%":
            j = text.find("%", i + 1)
            if j < 0:
                j = n
            blocks = [b.strip() for b in text[i + 1:j].split("*")]
            yield "ext", [b for b in blocks if b]
            i = j + 1
        elif c in " \r\n\t":
            i += 1
        else:
            j = text.find("*", i)
            if j < 0:
                j = n
            word = text[i:j].strip()
            i = j + 1
            if word:
                yield "word", word


_WORD = re.compile(r"([GDMXYIJ])([+-]?[\d.]+)")


def parse_gerber(text, filename="layer.gbr", prefix="L"):
    """Parse one Gerber file into a LayerData."""
    L = LayerData(filename)
    scale = 25.4                    # inches until told otherwise (the old default)
    fmt = (2, 4)
    omit_trailing = False
    apertures, macros = {}, {}
    x = y = 0.0
    cur_ap = None
    interp = "G01"
    multi_quadrant = True
    region = False
    contour = []                    # path data of the current region contour
    contour_start = None
    seg = _Segment(True)
    L.segments_raw = [seg]
    polarity_dark = True
    min_trace = None
    last_d = None
    sr = None                       # (nx, ny, dx, dy, start index)

    def new_segment(dark):
        nonlocal seg
        if seg.empty():
            seg.dark = dark
        else:
            seg = _Segment(dark)
            L.segments_raw.append(seg)

    def coord(val):
        if "." in val:
            return float(val) * scale
        neg = val.startswith("-")
        digits = val.lstrip("+-")
        if omit_trailing:
            digits = digits.ljust(fmt[0] + fmt[1], "0")
        v = int(digits or "0") / (10 ** fmt[1])
        return (-v if neg else v) * scale

    def flush_contour():
        nonlocal contour, contour_start
        if contour and len(contour) > 1:
            d = "".join(contour) + "Z"
            seg.regions.append(d)
            L.stats["regions"] += 1
            if L.outline_parts is not None:
                L.outline_parts.append(("region", d))
        contour, contour_start = [], None

    for kind, tok in _tokens(text):
        if kind == "ext":
            head = tok[0]
            if head.startswith("AM"):
                macros[head[2:]] = tok[1:]
                continue
            for block in tok:
                if block.startswith("FS"):
                    m = re.match(r"FS([LTD]?)([AI]?)(?:N\d)?(?:G\d)?X(\d)(\d)Y(\d)(\d)", block)
                    if m:
                        omit_trailing = m.group(1) == "T"
                        fmt = (int(m.group(3)), int(m.group(4)))
                        if m.group(2) == "I":
                            L.warnings.add("Incremental coordinates aren't supported")
                elif block.startswith("MO"):
                    scale = 1.0 if block[2:4] == "MM" else 25.4
                elif block.startswith("ADD"):
                    m = re.match(r"ADD(\d+)([^,]+)(?:,(.*))?", block)
                    if not m:
                        continue
                    num, name, raw = int(m.group(1)), m.group(2), m.group(3) or ""
                    try:
                        params = [float(v) for v in raw.split("X") if v != ""]
                    except ValueError:
                        params = []
                    try:
                        if name in ("C", "R", "O", "P"):
                            ap = standard_aperture(name, params, scale)
                        elif name in macros:
                            ap = macro_aperture(macros[name], params, scale, L.warnings, f"{prefix}a{num}")
                        else:
                            L.warnings.add(f"Aperture macro {name} is missing")
                            ap = Aperture(['<circle r="0.1"/>'], 0.1, 0.2)
                    except (GerberError, IndexError, ValueError, ZeroDivisionError):
                        L.warnings.add(f"Couldn't read aperture D{num}")
                        ap = Aperture(['<circle r="0.1"/>'], 0.1, 0.2)
                    apertures[num] = ap
                    L.defs.append(f'<g id="{prefix}a{num}">{"".join(ap.shapes)}</g>')
                elif block.startswith("LP"):
                    dark = block[2:3] != "C"
                    if dark != polarity_dark:
                        polarity_dark = dark
                        new_segment(dark)
                elif block.startswith("TF"):
                    if block.startswith("TF.FileFunction"):
                        L.function = block[len("TF.FileFunction,"):]
                elif block.startswith("SR"):
                    m = re.match(r"SRX(\d+)Y(\d+)I([\d.]+)J([\d.]+)", block)
                    if sr:  # close the previous block
                        _apply_step_repeat(L, sr, prefix)
                        sr = None
                    if m and (int(m.group(1)) > 1 or int(m.group(2)) > 1):
                        new_segment(polarity_dark)
                        sr = (int(m.group(1)), int(m.group(2)), float(m.group(3)) * scale, float(m.group(4)) * scale,
                              len(L.segments_raw) - 1)
                elif block.startswith(("LM", "LR", "LS")):
                    if block not in ("LMN", "LR0", "LS1"):
                        L.warnings.add("Mirrored, rotated or scaled objects aren't supported")
                elif block.startswith("IP") and block[2:5] == "NEG":
                    L.warnings.add("Negative image polarity isn't supported")
            continue

        word = tok
        if word.startswith("G04") or word.startswith("G4 ") or word == "G4":
            m = re.search(r"#@!\s*TF\.FileFunction,(.*)$", word)
            if m:
                L.function = m.group(1).strip()
            continue
        codes = _WORD.findall(word)
        if not codes and word:
            continue
        nx, ny, ci, cj = None, None, 0.0, 0.0
        d_code = None
        for letter, val in codes:
            if letter == "G":
                g = int(float(val))
                if g in (1, 2, 3):
                    interp = f"G0{g}"
                elif g == 36:
                    region = True
                    contour, contour_start = [], None
                elif g == 37:
                    flush_contour()
                    region = False
                elif g == 74:
                    multi_quadrant = False
                elif g == 75:
                    multi_quadrant = True
                elif g == 70:
                    scale = 25.4
                elif g == 71:
                    scale = 1.0
                elif g == 91:
                    L.warnings.add("Incremental coordinates aren't supported")
            elif letter == "D":
                d = int(float(val))
                if d >= 10:
                    cur_ap = d
                else:
                    d_code = d
            elif letter == "M":
                pass
            elif letter == "X":
                nx = coord(val)
            elif letter == "Y":
                ny = coord(val)
            elif letter == "I":
                ci = coord(val)
            elif letter == "J":
                cj = coord(val)
        if nx is None and ny is None and d_code is None:
            continue
        if d_code is None and (nx is not None or ny is not None):
            d_code = last_d or 1  # deprecated: coordinates without D code repeat the last operation
        last_d = d_code
        tx = x if nx is None else nx
        ty = y if ny is None else ny

        if d_code == 2:
            if region:
                flush_contour()
            x, y = tx, ty
            continue
        if d_code == 3:
            if cur_ap in apertures and not region:
                ap = apertures[cur_ap]
                seg.flashes.append((cur_ap, tx, ty))
                L.bbox.add(tx, ty, ap.radius)
                L.stats["flashes"] += 1
            x, y = tx, ty
            continue
        if d_code != 1:
            continue
        # D01: draw a line or an arc from (x, y) to (tx, ty)
        if interp == "G01":
            piece = f"L{f(tx)} {f(ty)}"
            bb_pts = [(x, y), (tx, ty)]
            arc = None
        else:
            ccw = interp == "G03"
            if multi_quadrant:
                cx, cy = x + ci, y + cj
            else:
                cx, cy = _single_quadrant_centre(x, y, tx, ty, abs(ci), abs(cj), ccw)
            span, r, extra = arc_points(x, y, tx, ty, cx, cy, ccw)
            if not multi_quadrant and span >= 2 * math.pi - 1e-9:
                piece = f"L{f(tx)} {f(ty)}"  # single-quadrant zero-length arc
                bb_pts = [(x, y), (tx, ty)]
                arc = None
            else:
                piece = svg_arc(x, y, tx, ty, cx, cy, ccw)
                bb_pts = [(x, y), (tx, ty)] + extra
                arc = (cx, cy, ccw)
        if region:
            if contour_start is None:
                contour_start = (x, y)
                contour.append(f"M{f(x)} {f(y)}")
            contour.append(piece)
            for px, py in bb_pts:
                L.bbox.add(px, py)
                L.center_bbox.add(px, py)
        elif cur_ap in apertures:
            ap = apertures[cur_ap]
            w = ap.stroke_width if ap.stroke_width is not None else 2 * ap.radius
            key = (round(w, 5), ap.round)
            seg.strokes.setdefault(key, []).append(f"M{f(x)} {f(y)}{piece}")
            for px, py in bb_pts:
                L.bbox.add(px, py, w / 2)
                L.center_bbox.add(px, py)
            L.stats["draws"] += 1
            if w > 0 and (min_trace is None or w < min_trace):
                min_trace = w
            L.outline_parts.append(("arc", x, y, tx, ty, arc) if arc else ("line", x, y, tx, ty))
        x, y = tx, ty

    if region:
        flush_contour()
    if sr:
        _apply_step_repeat(L, sr, prefix)
    L.stats["min_trace"] = min_trace
    L.segments = [(s.dark, s.elements(prefix)) for s in L.segments_raw if not s.empty()]
    del L.segments_raw
    return L


def _apply_step_repeat(L, sr, prefix):
    nx, ny, dx, dy, start = sr
    for s in L.segments_raw[start:]:
        els = s.elements(prefix)
        copies = []
        for i in range(nx):
            for j in range(ny):
                if i == 0 and j == 0:
                    continue
                copies.append(f'<g transform="translate({f(i * dx)} {f(j * dy)})">{"".join(els)}</g>')
        s.extra.extend(copies)
    L.bbox.add(L.bbox.maxx + (nx - 1) * dx, L.bbox.maxy + (ny - 1) * dy)


def _single_quadrant_centre(sx, sy, ex, ey, i, j, ccw):
    best, best_err = (sx + i, sy + j), math.inf
    for si in (1, -1):
        for sj in (1, -1):
            cx, cy = sx + si * i, sy + sj * j
            span, r, _ = arc_points(sx, sy, ex, ey, cx, cy, ccw)
            if span > math.pi / 2 + 1e-6:
                continue
            err = abs(r - math.hypot(ex - cx, ey - cy))
            if err < best_err:
                best, best_err = (cx, cy), err
    return best


# -------------------------------------------------------------------------- drills --

def parse_excellon(text, filename="drill.drl", plated=None):
    """Parse an Excellon drill file into a LayerData with holes and slots."""
    L = LayerData(filename)
    L.is_drill = True
    scale = 25.4
    int_digits, dec_digits = 2, 4
    lead_kept = False               # "LZ": leading zeros kept, so trailing ones may be missing
    explicit_fmt = False
    tools = {}
    cur = None
    x = y = 0.0
    mode = "drill"
    route_start = None
    holes, slots = [], []
    if plated is None:
        plated = "npth" not in filename.lower()
    header = True

    def num(val):
        if "." in val:
            return float(val) * scale
        neg = val.startswith("-")
        digits = val.lstrip("+-")
        if lead_kept:
            digits = digits.ljust(int_digits + dec_digits, "0")
        v = int(digits or "0") / (10 ** dec_digits)
        return (-v if neg else v) * scale

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            m = re.search(r"TF\.FileFunction,(.*)$", line)
            if m:
                L.function = m.group(1)
                if "NonPlated" in line:
                    plated = False
            m = re.search(r"FORMAT=\{?(\d):(\d)", line)
            if m and not explicit_fmt:
                int_digits, dec_digits = int(m.group(1)), int(m.group(2))
            continue
        up = line.upper()
        if up.startswith(("METRIC", "INCH")):
            scale = 1.0 if up.startswith("METRIC") else 25.4
            if not explicit_fmt:
                int_digits, dec_digits = (3, 3) if scale == 1.0 else (2, 4)
            lead_kept = ",LZ" in up
            m = re.search(r",(0+)\.(0+)", up)
            if m:
                int_digits, dec_digits, explicit_fmt = len(m.group(1)), len(m.group(2)), True
            continue
        if up in ("M71",):
            scale = 1.0
            continue
        if up in ("M72",):
            scale = 25.4
            continue
        if up in ("%", "M95"):
            header = False
            continue
        if up.startswith("M48"):
            header = True
            continue
        if up.startswith("T"):
            m = re.match(r"T(\d+)(.*)", up)
            if m:
                t = int(m.group(1))
                rest = m.group(2)
                cm = re.search(r"C([\d.]+)", rest)
                if cm:
                    tools[t] = float(cm.group(1)) * scale
                if not header or not cm:
                    cur = t
                    if t and t not in tools:
                        tools[t] = 0.8
                        L.warnings.add("A tool size is missing; 0.8 mm is assumed")
            continue
        if up.startswith("M15"):
            route_start = (x, y)
            continue
        if up.startswith(("M16", "M17")):
            route_start = None
            continue
        if up.startswith("G00"):
            mode = "route"
        elif up.startswith("G05"):
            mode = "drill"
        coords = re.findall(r"([XY])([+-]?[\d.]+)", up.split("G85")[0])
        if not coords:
            continue
        nx, ny = x, y
        for letter, val in coords:
            if letter == "X":
                nx = num(val)
            else:
                ny = num(val)
        d = tools.get(cur, 0.8)
        if "G85" in up:  # slot: X1Y1G85X2Y2
            ex, ey = nx, ny
            for letter, val in re.findall(r"([XY])([+-]?[\d.]+)", up.split("G85")[1]):
                if letter == "X":
                    ex = num(val)
                else:
                    ey = num(val)
            slots.append((nx, ny, ex, ey, d))
            x, y = ex, ey
            continue
        if up.startswith(("G01", "G02", "G03")) and route_start is not None:
            slots.append((x, y, nx, ny, d))
        elif mode == "drill" or not up.startswith(("G00", "G01", "G02", "G03")):
            if mode == "drill":
                holes.append((nx, ny, d))
        x, y = nx, ny

    seg = _Segment(True)
    for hx, hy, d in holes:
        seg.extra.append(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="{f(d / 2)}"/>')
        L.bbox.add(hx, hy, d / 2)
    for sx, sy, ex, ey, d in slots:
        seg.extra.append(f'<path fill="none" stroke="currentColor" stroke-linecap="round" stroke-width="{f(d)}" '
                         f'd="M{f(sx)} {f(sy)}L{f(ex)} {f(ey)}"/>')
        L.bbox.add(sx, sy, d / 2)
        L.bbox.add(ex, ey, d / 2)
    L.segments = [(True, seg.elements(""))]
    sizes = {}
    for _x, _y, d in holes:
        sizes[round(d, 3)] = sizes.get(round(d, 3), 0) + 1
    L.stats.update(holes=len(holes), slots=len(slots), tools=sizes, plated=plated)
    return L


# ------------------------------------------------------------------ board outline --

def board_shape(outline_layer, tol=0.02):
    """Join the outline's lines and arcs into closed loops; returns SVG path data or ''.

    Small gaps (common in hand-drawn outlines) are bridged with straight lines,
    duplicate loops are dropped, and if no loop encloses the board a rectangle
    around the outline is used as the board edge.
    """
    if outline_layer is None:
        return ""
    regions = [p[1] for p in outline_layer.outline_parts if p[0] == "region"]
    pieces, seen_pieces = [], set()
    for p in outline_layer.outline_parts:   # drop exact duplicates (outlines drawn twice)
        if p[0] not in ("line", "arc"):
            continue
        a, b = (round(p[1], 3), round(p[2], 3)), (round(p[3], 3), round(p[4], 3))
        k = (p[0], min(a, b), max(a, b), (round(p[5][0], 3), round(p[5][1], 3)) if p[0] == "arc" else None)
        if k in seen_pieces or (p[0] == "line" and a == b):
            continue
        seen_pieces.add(k)
        pieces.append(p)
    used = [False] * len(pieces)

    def ends(p):
        return (p[1], p[2]), (p[3], p[4])

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def draw(p, reverse):
        (sx, sy), (ex, ey) = ends(p)
        if reverse:
            sx, sy, ex, ey = ex, ey, sx, sy
        if p[0] == "line":
            return f"L{f(ex)} {f(ey)}", [(ex, ey)]
        cx, cy, ccw = p[5]
        ccw = ccw if not reverse else not ccw
        _span, _r, extra = arc_points(sx, sy, ex, ey, cx, cy, ccw)
        return svg_arc(sx, sy, ex, ey, cx, cy, ccw), extra + [(ex, ey)]

    grid = {}

    def key(pt):
        return (round(pt[0] / tol), round(pt[1] / tol))

    for i, p in enumerate(pieces):
        for end in ends(p):
            k = key(end)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    grid.setdefault((k[0] + dx, k[1] + dy), []).append(i)

    closed, open_chains = [], []   # chains: (start, end, [path commands], [points])
    for i, p in enumerate(pieces):
        if used[i]:
            continue
        used[i] = True
        start, cur = ends(p)
        cmd, pts = draw(p, False)
        cmds, points = [cmd], [start] + pts
        if p[0] == "arc" and dist(start, cur) <= tol:  # a full circle
            closed.append((cmds, points, start))
            continue
        # grow forwards, then backwards from the start
        for direction in (0, 1):
            for _ in range(len(pieces)):
                if dist(cur, start) <= tol and len(cmds) > 1:
                    break
                nxt = None
                for j in grid.get(key(cur), ()):
                    if used[j]:
                        continue
                    a, b = ends(pieces[j])
                    if dist(a, cur) <= tol:
                        nxt = (j, False, b)
                        break
                    if dist(b, cur) <= tol:
                        nxt = (j, True, a)
                        break
                if nxt is None:
                    break
                j, rev, cur = nxt
                used[j] = True
                cmd, pts = draw(pieces[j], rev)
                cmds.append(cmd)
                points.extend(pts)
            if dist(cur, start) <= tol and len(cmds) > 1:
                break
            if direction == 0:
                # reverse the chain so far and keep growing from its other end
                cmds, points, start, cur = _reverse_chain(cmds, points, pieces, start, cur)
        if dist(cur, start) <= tol and len(cmds) > 1:
            closed.append((cmds, points, start))
        else:
            open_chains.append((start, cur, cmds, points))

    # Bridge gaps between open chains with straight lines.
    size = outline_layer.center_bbox
    max_gap = max(1.0, 0.08 * math.hypot(size.width, size.height)) if not size.empty else 5.0
    remaining = list(open_chains)
    while remaining:
        start, cur, cmds, points = remaining.pop(0)
        cmds, points = list(cmds), list(points)
        while True:
            if dist(cur, start) <= max_gap and len(points) > 2:
                if not remaining or dist(cur, start) <= min(min(dist(cur, c[0]), dist(cur, c[1])) for c in remaining):
                    break
            best = None
            for k, (s2, e2, c2, p2) in enumerate(remaining):
                for rev, near, far in ((False, s2, e2), (True, e2, s2)):
                    dd = dist(cur, near)
                    if dd <= max_gap and (best is None or dd < best[0]):
                        best = (dd, k, rev)
            if best is None:
                break
            _dd, k, rev = best
            s2, e2, c2, p2 = remaining.pop(k)
            if rev:
                c2, p2, s2, e2 = _reverse_chain(c2, p2, None, s2, e2)
            cmds.append(f"L{f(s2[0])} {f(s2[1])}")
            cmds.extend(c2)
            points.extend(p2)
            cur = e2
        if dist(cur, start) <= max_gap and len(points) > 2:
            closed.append((cmds, points, start))

    loops, seen = [], set()
    biggest = 0.0
    for cmds, points, start in closed:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        area = abs(sum(points[k][0] * points[k - 1][1] - points[k - 1][0] * points[k][1] for k in range(len(points)))) / 2
        sig = (round(area, 1), round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1))
        if sig in seen:
            continue
        seen.add(sig)
        biggest = max(biggest, (max(xs) - min(xs)) * (max(ys) - min(ys)))
        loops.append(f"M{f(start[0])} {f(start[1])}" + "".join(cmds) + "Z")
    loops.extend(regions)
    if not size.empty and biggest < 0.5 * size.width * size.height and not regions:
        # Nothing encloses the board: use a rectangle around the outline as the edge.
        loops.insert(0, f"M{f(size.minx)} {f(size.miny)}H{f(size.maxx)}V{f(size.maxy)}H{f(size.minx)}Z")
    return "".join(loops)


def _reverse_chain(cmds, points, pieces, start, cur):
    """Reverse a chain of path commands (lines and arcs)."""
    pts_nodes = [start]
    for c in cmds:
        nums = re.findall(r"-?[\d.]+", c)
        pts_nodes.append((float(nums[-2]), float(nums[-1])))
    new_cmds = []
    for k in range(len(cmds) - 1, -1, -1):
        c = cmds[k]
        tx, ty = pts_nodes[k]
        if c.startswith("L"):
            new_cmds.append(f"L{f(tx)} {f(ty)}")
        else:  # one or two arc commands; flip the sweep flag of each
            arcs = re.findall(r"A([-\d.]+) ([-\d.]+) 0 ([01]) ([01]) ([-\d.]+) ([-\d.]+)", c)
            nodes = [pts_nodes[k]] + [(float(a[4]), float(a[5])) for a in arcs]
            for m in range(len(arcs) - 1, -1, -1):
                r, _r2, large, sweep, _x, _y = arcs[m]
                px, py = nodes[m]
                new_cmds.append(f"A{r} {r} 0 {large} {1 - int(sweep)} {f(px)} {f(py)}")
    return new_cmds, list(reversed(points)), cur, start


def escape(text):
    return html.escape(text, quote=True)
