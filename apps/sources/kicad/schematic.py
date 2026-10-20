"""Reading and drawing KiCad 6–9 schematics (.kicad_sch).

Drawn as SVG in millimetres with Y pointing down, like KiCad's schematic editor. The look is
close to KiCad's default colours but not identical (fonts and some details are simplified).
Only presentation attributes and classes are used, never style attributes, so the SVG can be
put straight into a page that has a strict Content-Security-Policy.
"""
import math
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as _xml_escape

from apps.design.gerber import Budget

from .sexpr import KicadError, file_version, find, find_all, has_flag, head, num, parse, points, value, xy

FIRST_VERSION = 20211014   # KiCad 6
NEWEST_KNOWN = 20251231
PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
         "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
         "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4)}

WIRE, BUS, BODY, BODY_FILL, PIN, PIN_TEXT = "#008400", "#000084", "#840000", "#ffffc2", "#840000", "#a90000"
FIELD, LABEL, GLOBAL, SHEET, NOTE, FRAME = "#006464", "#0f0f0f", "#840000", "#840084", "#0000c2", "#840000"
FONT = "Liberation Sans, Arial, Helvetica, sans-serif"


CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")  # not allowed in XML


def esc(text):
    return _xml_escape(CONTROL.sub("", str(text)), {'"': "&quot;"})


def fmt(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


@dataclass
class LibSymbol:
    name: str
    power: bool = False
    units: dict = field(default_factory=dict)      # (unit, style) -> [graphic nodes]
    hide_pin_numbers: bool = False
    hide_pin_names: bool = False
    pin_name_offset: float = 0.508


@dataclass
class Symbol:
    lib_id: str
    x: float
    y: float
    angle: float
    mirror: str | None
    unit: int
    uuid: str
    ref: str = ""
    value: str = ""
    footprint: str = ""
    dnp: bool = False
    power: bool = False
    fields: list = field(default_factory=list)      # [(name, text, x, y, angle, size, justify, hidden)]


@dataclass
class Sheet:
    name: str
    file: str
    x: float
    y: float
    w: float
    h: float
    uuid: str = ""
    pins: list = field(default_factory=list)        # [(name, shape, x, y, angle)]


@dataclass
class Schematic:
    version: int
    paper: tuple
    title: str = ""
    title_block: dict = field(default_factory=dict)
    lib: dict = field(default_factory=dict)
    symbols: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    buses: list = field(default_factory=list)
    junctions: list = field(default_factory=list)
    no_connects: list = field(default_factory=list)
    labels: list = field(default_factory=list)      # [(kind, text, x, y, angle, size, shape)]
    texts: list = field(default_factory=list)       # [(text, x, y, angle, size, justify)]
    sheets: list = field(default_factory=list)
    graphics: list = field(default_factory=list)    # sheet-level polylines, rectangles…
    newer: bool = False

    def nets(self):
        names = {t for kind, t, *_ in self.labels}
        names |= {s.value for s in self.symbols if s.power and s.value}
        return names

    def parts(self):
        """One entry per part; a part drawn as several units (U5A, U5B…) is listed once."""
        seen, out = set(), []
        for s in self.symbols:
            if s.power or s.ref.startswith("#") or s.ref in seen:
                continue
            seen.add(s.ref)
            out.append({"ref": s.ref, "value": s.value, "footprint": s.footprint, "uuid": s.uuid, "dnp": s.dnp, "lib": s.lib_id})
        return sorted(out, key=lambda p: _ref_key(p["ref"]))


def _ref_key(ref):
    m = re.match(r"([A-Za-z_#]*)(\d{0,9})", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, ref)


# ------------------------------------------------------------------ loading --

def _effects(node):
    eff = find(node, "effects")
    font = find(eff, "font") if eff is not None else None
    size = find(font, "size") if font is not None else None
    h = num(size[1], 1.27) if size is not None and len(size) > 1 else 1.27
    just = find(eff, "justify") if eff is not None else None
    justify = [j for j in just[1:] if isinstance(j, str)] if just is not None else []
    return h or 1.27, justify, has_flag(eff, "hide") if eff is not None else False


def _properties(node):
    out = []
    for p in find_all(node, "property"):
        if len(p) < 3:
            continue
        x, y, a = xy(p)
        size, justify, hidden = _effects(p)
        out.append((str(p[1]), str(p[2]), x, y, a, size, justify, hidden or has_flag(p, "hide")))
    return out


def _lib_symbol(node):
    name = str(node[1]) if len(node) > 1 else ""
    ls = LibSymbol(name=name, power=find(node, "power") is not None)
    pn = find(node, "pin_numbers")
    ls.hide_pin_numbers = pn is not None and has_flag(pn, "hide")
    pnames = find(node, "pin_names")
    if pnames is not None:
        ls.hide_pin_names = has_flag(pnames, "hide")
        ls.pin_name_offset = num(value(pnames, "offset"), 0.508)
    for sub in find_all(node, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", str(sub[1]) if len(sub) > 1 else "")
        key = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
        ls.units.setdefault(key, []).extend(c for c in sub[1:] if isinstance(c, list))
    return ls


def load_schematic(text, budget=None):
    stripped = text.lstrip()
    if stripped.startswith("EESchema"):
        raise KicadError("This KiCad version isn't supported for previews. Open and save it in KiCad 6 or newer.")
    tree = parse(text, budget)
    if head(tree) != "kicad_sch":
        raise KicadError("This isn't a KiCad schematic.")
    version = file_version(tree)
    if version and version < FIRST_VERSION:
        raise KicadError("This KiCad version isn't supported for previews. Open and save it in KiCad 6 or newer.")
    paper = find(tree, "paper")
    size = (297.0, 210.0)
    if paper is not None and len(paper) > 1:
        if paper[1] == "User" and len(paper) > 3:
            size = (num(paper[2], 297) or 297, num(paper[3], 210) or 210)
        else:
            size = tuple(float(v) for v in PAPER.get(str(paper[1]), (297, 210)))
        if "portrait" in paper[1:]:
            size = (size[1], size[0])
    sch = Schematic(version=version, paper=size, newer=version > NEWEST_KNOWN)
    tb = find(tree, "title_block")
    if tb is not None:
        for key in ("title", "date", "rev", "company"):
            v = value(tb, key)
            if v:
                sch.title_block[key] = str(v)
        sch.title = sch.title_block.get("title", "")
    libs = find(tree, "lib_symbols")
    for ls in find_all(libs, "symbol") if libs is not None else []:
        sym = _lib_symbol(ls)
        sch.lib[sym.name] = sym
    for node in find_all(tree, "symbol"):
        x, y, a = xy(node)
        mirror = value(node, "mirror")
        lib_id = str(value(node, "lib_id", "") or "")
        s = Symbol(lib_id=lib_id, x=x, y=y, angle=a, mirror=str(mirror) if mirror else None,
                   unit=int(num(value(node, "unit"), 1)) or 1, uuid=str(value(node, "uuid", "") or ""),
                   dnp=value(node, "dnp") == "yes", fields=_properties(node))
        props = {f[0]: f[1] for f in s.fields}
        s.ref, s.value, s.footprint = props.get("Reference", ""), props.get("Value", ""), props.get("Footprint", "")
        lib = sch.lib.get(lib_id)
        s.power = bool(lib and lib.power)
        sch.symbols.append(s)
    for node in find_all(tree, "wire"):
        sch.wires.append(points(node))
    for node in find_all(tree, "bus"):
        sch.buses.append(points(node))
    for node in find_all(tree, "polyline"):
        sch.graphics.append(("polyline", points(node)))
    for node in find_all(tree, "junction"):
        sch.junctions.append(xy(node)[:2])
    for node in find_all(tree, "no_connect"):
        sch.no_connects.append(xy(node)[:2])
    for kind in ("label", "global_label", "hierarchical_label"):
        for node in find_all(tree, kind):
            x, y, a = xy(node)
            size, justify, _ = _effects(node)
            sch.labels.append((kind, str(node[1]) if len(node) > 1 else "", x, y, a, size, str(value(node, "shape", "") or "")))
    for node in find_all(tree, "text"):
        x, y, a = xy(node)
        size, justify, hidden = _effects(node)
        if not hidden:
            sch.texts.append((str(node[1]) if len(node) > 1 else "", x, y, a, size, justify))
    for node in find_all(tree, "sheet"):
        x, y, _ = xy(node)
        sz = find(node, "size")
        w, h = (num(sz[1]), num(sz[2])) if sz is not None and len(sz) > 2 else (20.0, 10.0)
        props = {f[0]: f[1] for f in _properties(node)}
        sh = Sheet(name=props.get("Sheetname", props.get("Sheet name", "")), file=props.get("Sheetfile", props.get("Sheet file", "")),
                   x=x, y=y, w=w, h=h, uuid=str(value(node, "uuid", "") or ""))
        for pin in find_all(node, "pin"):
            px, py, pa = xy(pin)
            sh.pins.append((str(pin[1]) if len(pin) > 1 else "", str(pin[2]) if len(pin) > 2 and not isinstance(pin[2], list) else "",
                            px, py, pa))
        sch.sheets.append(sh)
    return sch


# ---------------------------------------------------------------- geometry --

def place(pt, x, y, angle, mirror):
    """A library point (Y up) placed on the sheet (Y down) for a symbol at (x, y), rotated and mirrored.
    Like KiCad: mirror in the library, then rotate counter-clockwise, then flip Y for the sheet."""
    px, py = pt
    if mirror == "x":
        py = -py
    elif mirror == "y":
        px = -px
    r = math.radians(angle)
    c, s = round(math.cos(r), 12), round(math.sin(r), 12)
    px, py = px * c - py * s, px * s + py * c
    return x + px, y - py


def _arc_path(p1, p2, p3):
    """SVG path for an arc through three points."""
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return f"M{fmt(x1)} {fmt(y1)}L{fmt(x3)} {fmt(y3)}"
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    cross = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    sweep = 1 if cross > 0 else 0
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (p1, p2, p3))
    span = (a3 - a1) % (2 * math.pi) if sweep else (a1 - a3) % (2 * math.pi)
    large = 1 if span > math.pi else 0
    return f"M{fmt(x1)} {fmt(y1)}A{fmt(r)} {fmt(r)} 0 {large} {sweep} {fmt(x3)} {fmt(y3)}"


def _anchor(justify):
    return "start" if "left" in justify else "end" if "right" in justify else "middle"


def _baseline(justify):
    return "hanging" if "top" in justify else "auto" if "bottom" in justify else "central"


def _text(x, y, text, size, colour, angle=0.0, justify=(), cls=""):
    rot = f' transform="rotate({fmt(-angle)} {fmt(x)} {fmt(y)})"' if angle % 360 else ""
    klass = f' class="{cls}"' if cls else ""
    return (f'<text x="{fmt(x)}" y="{fmt(y)}" font-size="{fmt(size)}" font-family="{FONT}" fill="{colour}" '
            f'text-anchor="{_anchor(justify)}" dominant-baseline="{_baseline(justify)}"{klass}{rot}>{esc(text)}</text>')


def _stroke_width(node, default=0.1524):
    st = find(node, "stroke")
    w = num(value(st, "width"), 0) if st is not None else num(value(node, "width"), 0)
    return w if w > 0 else default


def _fill(node):
    fl = find(node, "fill")
    kind = value(fl, "type", "none") if fl is not None else "none"
    return {"outline": BODY, "background": BODY_FILL}.get(str(kind), "none")


# ------------------------------------------------------------------ drawing --

def _draw_lib(sym, lib, out, budget):
    style = 1
    for (unit, st), items in sorted(lib.units.items()):
        if unit not in (0, sym.unit) or st not in (0, style):
            continue
        for g in items:
            budget.add()
            kind = head(g)
            T = lambda p: place(p, sym.x, sym.y, sym.angle, sym.mirror)  # noqa: E731
            w = _stroke_width(g)
            if kind == "rectangle":
                s, e = xy(g, "start")[:2], xy(g, "end")[:2]
                pts = [T(p) for p in ((s[0], s[1]), (e[0], s[1]), (e[0], e[1]), (s[0], e[1]))]
                out.append(f'<polygon points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="{_fill(g)}" '
                           f'stroke="{BODY}" stroke-width="{fmt(w)}"/>')
            elif kind == "polyline":
                pts = [T(p) for p in points(g)]
                if len(pts) >= 2:
                    fill = _fill(g)
                    tag = "polygon" if fill != "none" else "polyline"
                    out.append(f'<{tag} points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="{fill}" '
                               f'stroke="{BODY}" stroke-width="{fmt(w)}" stroke-linejoin="round" stroke-linecap="round"/>')
            elif kind == "circle":
                cx, cy = T(xy(g, "center")[:2])
                r = num(value(g, "radius"), 0)
                out.append(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(r)}" fill="{_fill(g)}" stroke="{BODY}" stroke-width="{fmt(w)}"/>')
            elif kind == "arc":
                p = [T(xy(g, k)[:2]) for k in ("start", "mid", "end")]
                out.append(f'<path d="{_arc_path(*p)}" fill="none" stroke="{BODY}" stroke-width="{fmt(w)}"/>')
            elif kind == "bezier":
                pts = [T(p) for p in points(g)]
                if len(pts) == 4:
                    d = f"M{fmt(pts[0][0])} {fmt(pts[0][1])}C" + " ".join(f"{fmt(a)} {fmt(b)}" for a, b in pts[1:])
                    out.append(f'<path d="{d}" fill="none" stroke="{BODY}" stroke-width="{fmt(w)}"/>')
            elif kind == "text" and len(g) > 1:
                x, y = T(xy(g)[:2])
                size, justify, hidden = _effects(g)
                if not hidden:
                    out.append(_text(x, y, g[1], size, BODY, 0, justify))
            elif kind == "pin":
                if has_flag(g, "hide"):
                    continue
                px, py, pa = xy(g)
                length = num(value(g, "length"), 2.54)
                ex, ey = px + length * math.cos(math.radians(pa)), py + length * math.sin(math.radians(pa))
                (ax, ay), (bx, by) = T((px, py)), T((ex, ey))
                out.append(f'<line x1="{fmt(ax)}" y1="{fmt(ay)}" x2="{fmt(bx)}" y2="{fmt(by)}" stroke="{PIN}" stroke-width="0.1524"/>')
                number = find(g, "number")
                if number is not None and len(number) > 1 and not lib.hide_pin_numbers:
                    mx, my = (ax + bx) / 2, (ay + by) / 2
                    out.append(_text(mx, my - 0.4, number[1], 1.0, PIN_TEXT, 0, ("bottom",)))
                name = find(g, "name")
                if name is not None and len(name) > 1 and name[1] not in ("~", "") and not lib.hide_pin_names and length > 0:
                    dx, dy = bx - ax, by - ay
                    d = math.hypot(dx, dy) or 1
                    off = lib.pin_name_offset or 0.508
                    nx, ny = bx + dx / d * (off + 0.3), by + dy / d * (off + 0.3)
                    vertical = abs(dy) > abs(dx)
                    # rotated text reads upwards, so a pin pointing up (dy < 0) needs it to start at the pin end
                    just = ("left",) if (dy < 0 if vertical else dx > 0) else ("right",)
                    out.append(_text(nx, ny, name[1], 1.1, FIELD, 90 if vertical else 0, just))


def _label(kind, text, x, y, angle, size, shape):
    if kind == "label":
        return _text(x, y - 0.3, text, size, LABEL, angle if angle in (90, 270) else 0, ("left", "bottom"))
    colour = GLOBAL if kind == "global_label" else SHEET
    h = size * 1.4
    length = max(len(text), 1) * size * 0.75 + h
    direction = -1 if int(angle) % 360 == 180 else 1   # angle 0: the label's body is to the right of its connection point
    if int(angle) % 180 == 90:
        y2 = y + (length if int(angle) % 360 == 270 else -length)
        pts = [(x, y), (x - h / 2, y + (h / 2 if y2 < y else -h / 2)), (x - h / 2, y2), (x + h / 2, y2),
               (x + h / 2, y + (h / 2 if y2 < y else -h / 2))]
        tx, ty, ta = x, (y + y2) / 2, 90
    else:
        x2 = x + direction * length
        pts = [(x, y), (x + direction * h / 2, y - h / 2), (x2, y - h / 2), (x2, y + h / 2), (x + direction * h / 2, y + h / 2)]
        tx, ty, ta = (x + x2) / 2 + direction * h / 4, y, 0
    poly = f'<polygon points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="none" stroke="{colour}" stroke-width="0.1524"/>'
    return poly + _text(tx, ty, text, size, colour, ta)


def _frame(sch, out):
    w, h = sch.paper
    out.append(f'<rect x="0" y="0" width="{fmt(w)}" height="{fmt(h)}" fill="#ffffff"/>')
    out.append(f'<rect x="10" y="10" width="{fmt(w - 20)}" height="{fmt(h - 20)}" fill="none" stroke="{FRAME}" stroke-width="0.15"/>')
    bx, by = w - 10 - 110, h - 10 - 32
    out.append(f'<rect x="{fmt(bx)}" y="{fmt(by)}" width="110" height="32" fill="none" stroke="{FRAME}" stroke-width="0.15"/>')
    tb = sch.title_block
    out.append(_text(bx + 2, by + 8, f"Title: {tb.get('title', '')}", 2.0, LABEL, 0, ("left",)))
    out.append(_text(bx + 2, by + 18, f"Rev: {tb.get('rev', '')}   Date: {tb.get('date', '')}", 1.5, LABEL, 0, ("left",)))
    out.append(_text(bx + 2, by + 26, tb.get("company", ""), 1.5, LABEL, 0, ("left",)))


def _field_orientation(angle, justify, sym):
    """How KiCad shows a symbol's field: its angle turns with the symbol (0 and 90 swap when the symbol is
    rotated by 90 or 270), text is never upside down, and mirroring flips the justification."""
    a = int(round(angle)) % 180
    if int(round(sym.angle)) % 180 == 90:
        a = 90 - a
    just = list(justify)
    swap = {"left": "right", "right": "left"} if sym.mirror == "y" else {"top": "bottom", "bottom": "top"} if sym.mirror == "x" else {}
    just = [swap.get(j, j) for j in just]
    return (90.0 if a == 90 else 0.0), tuple(just)


def draw_schematic(sch, highlight=None, budget=None, sheet_links=None):
    """SVG of one sheet. highlight: {uuid: "added"|"removed"|"changed"}. sheet_links: {file: True if it exists}."""
    budget = budget or Budget()
    highlight = highlight or {}
    out = []
    _frame(sch, out)
    for kind, pts in sch.graphics:
        budget.add()
        if len(pts) >= 2:
            out.append(f'<polyline points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="none" stroke="{NOTE}" stroke-width="0.15"/>')
    for pts in sch.wires:
        budget.add()
        if len(pts) >= 2:
            out.append(f'<polyline points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="none" stroke="{WIRE}" '
                       'stroke-width="0.1524" stroke-linecap="round"/>')
    for pts in sch.buses:
        budget.add()
        if len(pts) >= 2:
            out.append(f'<polyline points="{" ".join(f"{fmt(a)},{fmt(b)}" for a, b in pts)}" fill="none" stroke="{BUS}" '
                       'stroke-width="0.3" stroke-linecap="round"/>')
    for x, y in sch.junctions:
        budget.add()
        out.append(f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="0.45" fill="{WIRE}"/>')
    for x, y in sch.no_connects:
        budget.add()
        d = 0.635
        out.append(f'<path d="M{fmt(x - d)} {fmt(y - d)}L{fmt(x + d)} {fmt(y + d)}M{fmt(x - d)} {fmt(y + d)}L{fmt(x + d)} {fmt(y - d)}" '
                   f'stroke="{BUS}" stroke-width="0.15"/>')
    for label in sch.labels:
        budget.add()
        out.append(_label(*label))
    for text, x, y, a, size, justify in sch.texts:
        budget.add()
        out.append(_text(x, y, text, size, NOTE, a, justify or ("left", "bottom")))
    for sh in sch.sheets:
        budget.add()
        exists = sheet_links.get(sh.file) if sheet_links is not None else None
        missing = ' data-missing="1"' if exists is False else ""
        g = [f'<g class="sheet" data-sheet="{esc(sh.file)}" data-name="{esc(sh.name)}"{missing}>',
             f'<rect x="{fmt(sh.x)}" y="{fmt(sh.y)}" width="{fmt(sh.w)}" height="{fmt(sh.h)}" fill="#ffffff" fill-opacity="0.01" '
             f'stroke="{SHEET}" stroke-width="0.2"/>',
             _text(sh.x, sh.y - 0.6, sh.name, 1.5, FIELD, 0, ("left", "bottom")),
             _text(sh.x, sh.y + sh.h + 0.6, f"File: {sh.file}" + (" (not in this version)" if exists is False else ""), 1.27,
                   SHEET, 0, ("left", "top"))]
        for name, shape, px, py, pa in sh.pins:
            inside_left = abs(px - sh.x) < 0.01
            g.append(_text(px + (1 if inside_left else -1), py, name, 1.27, SHEET, 0, ("left",) if inside_left else ("right",)))
        g.append("</g>")
        out.append("".join(g))
    for sym in sch.symbols:
        budget.add()
        lib = sch.lib.get(sym.lib_id)
        state = highlight.get(sym.uuid)
        cls = "sym" + (f" hl-{state}" if state else "") + (" dnp" if sym.dnp else "")
        g = [f'<g class="{cls}" data-ref="{esc(sym.ref)}" data-value="{esc(sym.value)}" data-footprint="{esc(sym.footprint)}" '
             f'data-lib="{esc(sym.lib_id)}" data-uuid="{esc(sym.uuid)}">']
        body = []
        if lib is not None:
            _draw_lib(sym, lib, body, budget)
        g += body
        for name, text, x, y, a, size, justify, hidden in sym.fields:
            if hidden or not text or name not in ("Reference", "Value") and not name:
                continue
            if name not in ("Reference", "Value"):
                continue
            shown = text if name != "Reference" or not sym.ref.startswith("#") else ""
            if shown:
                g.append(_text(x, y, shown, size, FIELD, *_field_orientation(a, justify, sym)))
        if sym.dnp:
            g.append(f'<path d="M{fmt(sym.x - 3)} {fmt(sym.y - 3)}L{fmt(sym.x + 3)} {fmt(sym.y + 3)}M{fmt(sym.x - 3)} {fmt(sym.y + 3)}'
                     f'L{fmt(sym.x + 3)} {fmt(sym.y - 3)}" stroke="#d00000" stroke-width="0.3"/>')
        g.append("</g>")
        out.append("".join(g))
    w, h = sch.paper
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {fmt(w)} {fmt(h)}" class="kicad-sch" '
            f'data-width="{fmt(w)}" data-height="{fmt(h)}">' + "".join(out) + "</svg>")
