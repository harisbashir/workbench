"""Turn a set of Gerber and drill files into a board picture (SVG) and facts about it.

render_board() recognises each layer (from Gerber X2 attributes when present,
otherwise from the usual file names of KiCad, Eagle, Altium, EasyEDA…), then
builds one SVG with three views: the top and bottom of the board as they'll
look when made, and every layer on its own for checking.
"""
import io
import json
import math
import re
import zipfile

from .gerber import BBox, board_shape, escape, f, parse_excellon, parse_gerber

MAX_FILES = 300
MAX_TOTAL = 120 * 1024 * 1024

# Solder-mask colours as (over bare board, over copper, silkscreen)
MASK_COLOURS = {
    "green": ("#1d5a36", "#2c7a48", "#f4f4f4"),
    "black": ("#141414", "#262626", "#f4f4f4"),
    "blue": ("#10357a", "#1f4ea3", "#f4f4f4"),
    "red": ("#8c1a1a", "#b0302c", "#f4f4f4"),
    "purple": ("#46216b", "#5d3389", "#f4f4f4"),
    "yellow": ("#c9a414", "#dcbc3a", "#1b1b1b"),
    "white": ("#e6e6e2", "#f3f3ef", "#1b1b1b"),
}
FINISHES = {"hasl": "#cfd2d4", "enig": "#d6b057"}
FR4 = "#b9a878"

LAYER_COLOURS = {
    ("copper", "top"): "#d24a3c", ("copper", "bottom"): "#3d86d6", ("copper", "inner"): "#c9b53a",
    ("mask", "top"): "#b44ee6", ("mask", "bottom"): "#18b5a5",
    ("silk", "top"): "#efe9a8", ("silk", "bottom"): "#e2a9a0",
    ("paste", "top"): "#9c9c9c", ("paste", "bottom"): "#5aa0a0",
    ("outline", None): "#e8e6df", ("drill", None): "#f7f7f7", ("other", None): "#8d8f96",
}
INNER_COLOURS = ["#c9b53a", "#c261c2", "#3ec2c2", "#78c23e", "#c2863e", "#6e6ee0"]

KIND_ORDER = {"other": 0, "outline": 1, "copper": 2, "paste": 3, "mask": 4, "silk": 5, "drill": 6}


# ------------------------------------------------------------------ recognising --

EXT_RULES = {
    "gtl": ("copper", "top"), "gbl": ("copper", "bottom"), "cmp": ("copper", "top"), "sol": ("copper", "bottom"),
    "top": ("copper", "top"), "bot": ("copper", "bottom"),
    "gts": ("mask", "top"), "gbs": ("mask", "bottom"), "stc": ("mask", "top"), "sts": ("mask", "bottom"),
    "smt": ("mask", "top"), "smb": ("mask", "bottom"),
    "gto": ("silk", "top"), "gbo": ("silk", "bottom"), "plc": ("silk", "top"), "pls": ("silk", "bottom"),
    "sst": ("silk", "top"), "ssb": ("silk", "bottom"),
    "gtp": ("paste", "top"), "gbp": ("paste", "bottom"), "crc": ("paste", "top"), "crs": ("paste", "bottom"),
    "spt": ("paste", "top"), "spb": ("paste", "bottom"),
    "gko": ("outline", None), "gm1": ("outline", None), "gml": ("outline", None), "gmo": ("outline", None),
    "mil": ("outline", None), "dim": ("outline", None), "out": ("outline", None), "oln": ("outline", None),
    "drl": ("drill", None), "drd": ("drill", None), "xln": ("drill", None), "exc": ("drill", None), "nc": ("drill", None),
    "gd1": ("other", None), "gg1": ("other", None), "gpt": ("other", None), "gpb": ("other", None),
}
NAME_RULES = [  # (regex on the lower-case name, kind, side)
    (r"(^|[-_. ])f[._]cu|top[._ -]?(copper|layer)|toplayer|copper[._ -]?top|[-_]l1\b|\.art$.*top", "copper", "top"),
    (r"(^|[-_. ])b[._]cu|bot(tom)?[._ -]?(copper|layer)|bottomlayer|copper[._ -]?bot", "copper", "bottom"),
    (r"in(\d+)[._]cu|inner[._ -]?(layer)?(\d+)|[._-]g(\d+)$|\.g(\d+)l?$|midlayer(\d+)|inner(\d+)", "copper", "inner"),
    (r"f[._]mask|top[._ -]?(solder)?[._ -]?mask|topsolder|soldermask[._ -]?top|smask[._ -]?top", "mask", "top"),
    (r"b[._]mask|bot(tom)?[._ -]?(solder)?[._ -]?mask|bottomsolder|soldermask[._ -]?bot", "mask", "bottom"),
    (r"f[._]silk|top[._ -]?silk|topoverlay|silk[._ -]?top|top[._ -]?legend", "silk", "top"),
    (r"b[._]silk|bot(tom)?[._ -]?silk|bottomoverlay|silk[._ -]?bot|bot(tom)?[._ -]?legend", "silk", "bottom"),
    (r"f[._]paste|top[._ -]?paste|toppaste|paste[._ -]?top", "paste", "top"),
    (r"b[._]paste|bot(tom)?[._ -]?paste|bottompaste|paste[._ -]?bot", "paste", "bottom"),
    (r"edge[._]cuts|board[._ -]?outline|boardoutline|outline|profile|keepout|mechanical1?\b|edge", "outline", None),
    (r"npth|pth|drill|\.drl$|\.xln$", "drill", None),
    (r"f[._]fab|b[._]fab|courtyard|crtyd|dwgs|cmts|eco\d|user|fab|assembly|drawing|margin", "other", None),
]


def identify(name, function="", is_drill=False):
    """Returns (kind, side, inner number) for a layer file."""
    if function:
        parts = [p.strip() for p in function.split(",")]
        head = parts[0].lower()
        side = None
        for p in parts[1:]:
            pl = p.lower()
            if pl.startswith("top"):
                side = "top"
            elif pl.startswith("bot"):
                side = "bottom"
            elif pl.startswith("inr"):
                side = "inner"
        if head == "copper":
            n = int(re.sub(r"\D", "", parts[1]) or 0) if len(parts) > 1 else 0
            return "copper", side or "inner", n
        if head == "soldermask":
            return "mask", side or "top", 0
        if head == "legend":
            return "silk", side or "top", 0
        if head == "paste":
            return "paste", side or "top", 0
        if head == "profile":
            return "outline", None, 0
        if head in ("plated", "nonplated", "drill"):
            return "drill", None, 0
        return "other", side, 0
    if is_drill:
        return "drill", None, 0
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    stem = low.rsplit(".", 1)[0]
    if ext in EXT_RULES and ext not in ("gbr", "pho", "art", "ger"):
        kind, side = EXT_RULES[ext]
        return kind, side, 0
    m = re.match(r"g(\d+)l?$", ext) or re.match(r"gp(\d+)$", ext)
    if m:
        return "copper", "inner", int(m.group(1)) + 1
    for pattern, kind, side in NAME_RULES:
        m = re.search(pattern, stem if kind != "drill" else low)
        if m:
            n = 0
            if kind == "copper" and side == "inner":
                n = next((int(g) for g in m.groups() if g and g.isdigit()), 1) + 1
            return kind, side, n
    return "other", None, 0


def label_for(kind, side, n, plated=None):
    names = {"copper": "copper", "mask": "solder mask", "silk": "silkscreen", "paste": "paste"}
    if kind == "copper" and side == "inner":
        return f"Inner copper {max(n - 1, 1)}"
    if kind in names:
        return f"{'Top' if side == 'top' else 'Bottom'} {names[kind]}"
    if kind == "outline":
        return "Board outline"
    if kind == "drill":
        return "Drill holes" + ("" if plated is None else (" (plated)" if plated else " (non-plated)"))
    return "Drawing / other"


def looks_like(name, data):
    """'gerber', 'drill', 'job' or None."""
    low = name.lower()
    if low.endswith(".gbrjob"):
        return "job"
    head = data[:4000].decode("latin-1", "replace")
    if re.search(r"(^|\n)\s*M48\b", head) or (re.search(r"(^|\n)\s*(METRIC|INCH)\b", head) and re.search(r"\nT\d+C", head)):
        return "drill"
    if "%FS" in head or "%MO" in head or re.search(r"(^|\n)G04", head) or "%ADD" in head:
        return "gerber"
    return None


def collect(entries):
    """Expand zip files; returns [(file name, bytes)] of everything worth looking at."""
    out, total = [], 0
    for name, data in entries:
        if name.lower().endswith(".zip"):
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                continue
            infos = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename
                     and not i.filename.split("/")[-1].startswith(".")]
            if len(infos) > MAX_FILES:
                raise ValueError(f"The zip has more than {MAX_FILES} files.")
            for info in infos:
                total += info.file_size
                if total > MAX_TOTAL:
                    raise ValueError("The Gerber files are too large to show (over 120 MB unzipped).")
                if info.filename.lower().endswith(".zip"):
                    continue
                out.append((info.filename.split("/")[-1], z.read(info)))
        else:
            total += len(data)
            out.append((name, data))
    return out


# ---------------------------------------------------------------------- drawing --

def render_board(entries, prefix="g"):
    """entries: [(name, bytes)] (zips allowed). Returns {"svg": …, "layers": […], "info": {…}, "warnings": […]}."""
    files = collect(entries)
    layers, warnings, job = [], [], {}
    for name, data in files:
        kind = looks_like(name, data)
        if kind is None:
            continue
        text = data.decode("latin-1")
        if kind == "job":
            try:
                job = json.loads(text)
            except ValueError:
                pass
            continue
        i = len(layers)
        try:
            L = parse_excellon(text, name) if kind == "drill" else parse_gerber(text, name, prefix=f"{prefix}{i}")
        except Exception as exc:  # a broken file shouldn't stop the rest from showing
            warnings.append(f"Couldn't read {name}: {exc}")
            continue
        if not L.segments or L.bbox.empty:
            continue
        k, side, n = identify(name, L.function, L.is_drill)
        L.kind, L.side, L.n = k, side, n
        L.label = label_for(k, side, n, L.stats.get("plated") if L.is_drill else None)
        L.index = i
        layers.append(L)
        warnings.extend(f"{name}: {w}" for w in sorted(L.warnings))
    if not layers:
        raise ValueError("No Gerber or drill files were found.")

    def pick(kind, side=None):
        return [L for L in layers if L.kind == kind and (side is None or L.side == side)]

    outline = next(iter(pick("outline")), None)
    shape = board_shape(outline) if outline else ""
    frame = BBox()
    if outline and not outline.center_bbox.empty:
        frame.merge(outline.center_bbox)
    else:
        for L in layers:
            if L.kind in ("copper", "mask", "silk", "drill"):
                frame.merge(L.bbox)
        if frame.empty:
            for L in layers:
                frame.merge(L.bbox)
        warnings.append("No board outline was found, so the board is drawn as a rectangle.")
    if not shape:
        if outline:
            warnings.append("The board outline isn't a closed shape, so the board is drawn as a rectangle.")
        shape = (f"M{f(frame.minx)} {f(frame.miny)}H{f(frame.maxx)}V{f(frame.maxy)}H{f(frame.minx)}Z")

    view = BBox()
    view.merge(frame)
    for L in layers:
        if L.kind != "other":
            view.merge(L.bbox)
    margin = max(view.width, view.height) * 0.03 + 1
    mx, my, mw, mh = view.minx - margin, view.miny - margin, view.width + 2 * margin, view.height + 2 * margin
    box = f'x="{f(mx)}" y="{f(my)}" width="{f(mw)}" height="{f(mh)}"'

    defs = []
    for L in layers:
        body = ""
        for k, (dark, els) in enumerate(L.segments):
            content = "".join(els)
            if dark:
                body += content
            else:
                mid = f"{prefix}{L.index}m{k}"
                defs.append(f'<mask id="{mid}" maskUnits="userSpaceOnUse" {box}><rect {box} fill="#fff"/>'
                            f'<g color="#000" fill="#000">{content}</g></mask>')
                body = f'<g mask="url(#{mid})">{body}</g>'
        defs.extend(L.defs)
        defs.append(f'<g id="{prefix}L{L.index}" fill="currentColor">{body}</g>')
    defs.append(f'<path id="{prefix}shape" fill-rule="evenodd" d="{shape}"/>')
    defs.append(f'<clipPath id="{prefix}clip"><use href="#{prefix}shape" clip-rule="evenodd"/></clipPath>')
    drills = pick("drill")
    defs.append(f'<mask id="{prefix}holes" maskUnits="userSpaceOnUse" {box}><rect {box} fill="#fff"/><g color="#000" fill="#000">'
                + "".join(f'<use href="#{prefix}L{L.index}"/>' for L in drills) + "</g></mask>")

    mask_base, mask_cu, silk = MASK_COLOURS["green"]
    finish = FINISHES["hasl"]

    def side_view(side):
        cu, mask, sk = pick("copper", side), pick("mask", side), pick("silk", side)
        parts = []
        if mask:
            mid = f"{prefix}open-{side}"
            defs.append(f'<mask id="{mid}" maskUnits="userSpaceOnUse" {box}><rect {box} fill="#000"/><g color="#fff" fill="#fff">'
                        + "".join(f'<use href="#{prefix}L{L.index}"/>' for L in mask) + "</g></mask>")
            parts.append(f'<use href="#{prefix}shape" fill="{mask_base}" data-paint="mask-base"/>')
            parts += [f'<use href="#{prefix}L{L.index}" color="{mask_cu}" data-paint="mask-copper"/>' for L in cu]
            parts.append(f'<g mask="url(#{mid})"><use href="#{prefix}shape" fill="{FR4}"/>'
                         + "".join(f'<use href="#{prefix}L{L.index}" color="{finish}" data-paint="finish"/>' for L in cu) + "</g>")
        else:
            parts.append(f'<use href="#{prefix}shape" fill="{FR4}"/>')
            parts += [f'<use href="#{prefix}L{L.index}" color="{finish}" data-paint="finish"/>' for L in cu]
        parts += [f'<use href="#{prefix}L{L.index}" color="{silk}" data-paint="silk"/>' for L in sk]
        return "".join(parts)

    mirror = f'translate({f(frame.minx + frame.maxx)} 0) scale(-1 1)'
    views = {
        "top": f'<g data-view="top" clip-path="url(#{prefix}clip)" mask="url(#{prefix}holes)">{side_view("top")}</g>',
        "bottom": f'<g data-view="bottom" transform="{mirror}"><g clip-path="url(#{prefix}clip)" mask="url(#{prefix}holes)">'
                  f'{side_view("bottom")}</g></g>',
    }
    ordered = sorted(layers, key=lambda L: (L.side == "top", KIND_ORDER.get(L.kind, 0), -L.n if L.kind == "copper" else 0))
    inner_i = 0
    layer_rows, layer_uses = [], []
    for L in ordered:
        if L.kind == "copper" and L.side == "inner":
            colour = INNER_COLOURS[inner_i % len(INNER_COLOURS)]
            inner_i += 1
        else:
            colour = LAYER_COLOURS.get((L.kind, L.side if L.kind in ("copper", "mask", "silk", "paste") else None), "#999")
        opacity = {"mask": 0.45, "paste": 0.6, "copper": 0.8}.get(L.kind, 0.9)
        layer_uses.append(f'<use href="#{prefix}L{L.index}" color="{colour}" opacity="{opacity}" data-layer="{L.index}"'
                          + (' display="none"' if L.kind in ("paste", "other") else "") + "/>")
        layer_rows.insert(0, {"id": L.index, "file": L.filename, "label": L.label, "kind": L.kind, "side": L.side,
                           "colour": colour, "shown": L.kind not in ("paste", "other")})
    views["layers"] = f'<g data-view="layers">{"".join(layer_uses)}</g>'

    # --- facts -------------------------------------------------------------
    copper = pick("copper")
    holes = {"plated": 0, "nonplated": 0, "slots": 0, "sizes": {}}
    for L in drills:
        holes["plated" if L.stats.get("plated", True) else "nonplated"] += L.stats["holes"]
        holes["slots"] += L.stats["slots"]
        for d, n in L.stats["tools"].items():
            holes["sizes"][d] = holes["sizes"].get(d, 0) + n
    traces = [L.stats["min_trace"] for L in copper if L.stats["min_trace"]]
    info = {
        "width": round(frame.width, 2), "height": round(frame.height, 2),
        "copper_layers": len(copper) or None,
        "holes": holes["plated"] + holes["nonplated"], "plated": holes["plated"], "nonplated": holes["nonplated"],
        "slots": holes["slots"],
        "drill_sizes": sorted(((d, n) for d, n in holes["sizes"].items()), key=lambda t: t[0]),
        "min_drill": min(holes["sizes"]) if holes["sizes"] else None,
        "min_trace": round(min(traces), 3) if traces else None,
        "pads_top": sum(L.stats["flashes"] for L in pick("copper", "top")),
        "pads_bottom": sum(L.stats["flashes"] for L in pick("copper", "bottom")),
        "has_outline": bool(outline),
        "sides": [s for s in ("top", "bottom") if pick("copper", s) or pick("silk", s)],
    }
    specs = (job or {}).get("GeneralSpecs", {})
    if specs:
        info["thickness"] = specs.get("BoardThickness")
        info["finish"] = specs.get("Finish")
        if not info["copper_layers"]:
            info["copper_layers"] = specs.get("LayerNumber")
    info["area_cm2"] = round(frame.width * frame.height / 100, 1)

    vb = f'{f(mx)} {f(-(my + mh))} {f(mw)} {f(mh)}'

    def document(which):
        hide = 'display="none" data-view="'
        body = "".join(views[v] if v == which[0] else views[v].replace('data-view="', hide, 1) for v in which)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" data-board-width="{f(frame.width)}" '
                f'data-board-height="{f(frame.height)}" data-origin="{f(frame.minx)} {f(frame.miny)}">'
                f'<defs>{"".join(defs)}</defs><g transform="scale(1 -1)">{body}</g></svg>')

    return {
        "svg": document(("top", "bottom", "layers")),
        "thumb_top": document(("top",)),
        "viewbox": vb,
        "layers": layer_rows,
        "info": info,
        "warnings": warnings[:30],
        "files": [{"file": L.filename, "label": L.label} for L in layers],
        "unused": [n for n, d in files if looks_like(n, d) is None],
    }


def mm(v):
    return f"{v:.2f}".rstrip("0").rstrip(".") if v is not None else "—"


def escape_label(s):
    return escape(s)


__all__ = ["render_board", "identify", "collect", "looks_like", "MASK_COLOURS", "FINISHES", "math"]
