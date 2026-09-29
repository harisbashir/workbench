"""Standalone SVG and vector PDF of a diagram, with an optional legend and title block."""
import zlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from . import geometry as G

FONT_FAMILY = "Helvetica, Arial, 'Liberation Sans', sans-serif"


def _d(cmds):
    out = []
    for c in cmds:
        if c[0] == "Z":
            out.append("Z")
        else:
            out.append(c[0] + " " + " ".join(f"{v:.2f}".rstrip("0").rstrip(".") for v in c[1:]))
    return " ".join(out)


def legend_prims(data, x, y):
    """Legend of the connection types used, starting at (x, y). Returns (prims, width, height)."""
    items = G.legend_items(data)
    if not items or not data.get("page", {}).get("legend", True):
        return [], 0, 0
    prims = [("text", x, y + 12, "Legend", 11, True, "#374151", "start", {})]
    yy = y + 22
    width = 60
    for key, k in items:
        prims.append(("path", [("M", x, yy + 5), ("L", x + 34, yy + 5)], None, k["color"], k["width"], k["dash"], {}))
        label = k["label"].replace("²", "2")
        prims.append(("text", x + 42, yy + 9, label, 10, False, "#374151", "start", {}))
        width = max(width, 42 + G.text_width(label, 10))
        yy += 16
    return prims, width, yy - y


def to_svg(data, *, highlight=None, legend=True, interactive=False):
    prims = G.layout(data)
    x, y, w, h = G.bounds(prims)
    if legend:
        lp, lw, lh = legend_prims(data, x + 24, y + h - 8)
        if lp:
            prims = prims + lp
            h += lh + 8
            w = max(w, lw + 48)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:.1f} {y:.1f} {w:.1f} {h:.1f}" width="{w:.0f}" height="{h:.0f}" '
             f'font-family="{FONT_FAMILY}">',
             f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#ffffff"/>']
    for p in prims:
        meta = p[-1] if isinstance(p[-1], dict) else {}
        attrs = ""
        if interactive and meta.get("node"):
            attrs = f' data-node="{escape(meta["node"])}"'
        if p[0] == "path":
            _, d, fill, stroke, width, dash, _m = p
            a = [f'd="{_d(d)}"', f'fill="{fill or "none"}"']
            if stroke:
                a.append(f'stroke="{stroke}" stroke-width="{width:g}" stroke-linejoin="round" stroke-linecap="round"')
            if dash:
                a.append(f'stroke-dasharray="{dash}"')
            parts.append(f"<path {' '.join(a)}{attrs}/>")
        elif p[0] == "text":
            _, tx, ty, text, size, bold, color, anchor, _m = p
            weight = ' font-weight="bold"' if bold else ""
            parts.append(f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{size}" fill="{color}" text-anchor="{anchor}"'
                         f'{weight}{attrs}>{escape(text)}</text>')
        elif p[0] == "label_bg":
            _, bx, by, bw, bh = p
            parts.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bw:.2f}" height="{bh:.2f}" rx="3" fill="#ffffff" fill-opacity="0.92"/>')
    if highlight:
        n = next((n for n in data.get("nodes", []) if n["id"] == highlight), None)
        if n:
            parts.append(f'<rect x="{n["x"] - 5}" y="{n["y"] - 5}" width="{n["w"] + 10}" height="{n["h"] + 10}" rx="8" '
                         'fill="none" stroke="#f59e0b" stroke-width="3" stroke-dasharray="6 3"/>')
    parts.append("</svg>")
    return "\n".join(parts)


# --- PDF ------------------------------------------------------------------------------------------

PAGES = {"A4": (841.89, 595.28), "A3": (1190.55, 841.89)}  # landscape, points
_REPLACE = {"Ω": "Ohm", "→": "->", "←": "<-", "↔": "<->", "≤": "<=", "≥": ">=", "²": "2", "³": "3", "×": "x",
            "≈": "~", "✓": "v", "…": "..."}


def _pdf_text(s):
    for a, b in _REPLACE.items():
        s = s.replace(a, b)
    raw = s.encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class _Canvas:
    def __init__(self, page_h):
        self.ops = []
        self.page_h = page_h

    def path(self, cmds, fill, stroke, width, dash, tx):
        ops = []
        for c in cmds:
            if c[0] == "M":
                x, y = tx(c[1], c[2])
                ops.append(f"{x:.2f} {y:.2f} m")
            elif c[0] == "L":
                x, y = tx(c[1], c[2])
                ops.append(f"{x:.2f} {y:.2f} l")
            elif c[0] == "C":
                pts = [tx(c[i], c[i + 1]) for i in (1, 3, 5)]
                ops.append(" ".join(f"{a:.2f} {b:.2f}" for a, b in pts) + " c")
            else:
                ops.append("h")
        self.ops.append("q")
        if fill:
            self.ops.append("%.3f %.3f %.3f rg" % _rgb(fill))
        if stroke:
            self.ops.append("%.3f %.3f %.3f RG" % _rgb(stroke))
            self.ops.append(f"{width * self.scale:.2f} w 1 J 1 j")
            self.ops.append(f"[{' '.join(f'{float(v) * self.scale:.2f}' for v in dash.split())}] 0 d" if dash else "[] 0 d")
        self.ops.extend(ops)
        self.ops.append("B" if fill and stroke else ("f" if fill else "S"))
        self.ops.append("Q")

    def text(self, x, y, s, size, bold, color, anchor):
        w = G.text_width(s, size, bold)
        if anchor == "middle":
            x -= w * self.scale / 2
        self.ops.append("BT /%s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td (%s) Tj ET" % (
            "F2" if bold else "F1", size * self.scale, *_rgb(color), x, y, _pdf_text(s).decode("latin-1")))


def to_pdf(data, *, title_block=None, page=None):
    """A vector PDF on A4 or A3 landscape with a legend and a title block."""
    prims = G.layout(data)
    bx, by, bw, bh = G.bounds(prims, margin=8)
    if page not in PAGES:
        page = "A3" if max(bw, bh * 1.41) > 1500 else "A4"
    pw, ph = PAGES[page]
    margin, tb_h = 28, 62 if title_block else 0
    lp, lw, lh = legend_prims(data, 0, 0)
    avail_w, avail_h = pw - 2 * margin, ph - 2 * margin - tb_h - (lh + 10 if lp else 0)
    scale = min(avail_w / bw, avail_h / bh, 1.6)
    ox = margin + (avail_w - bw * scale) / 2
    oy = margin + (avail_h - bh * scale) / 2  # distance from the top of the page

    def tx(x, y):
        return ox + (x - bx) * scale, ph - (oy + (y - by) * scale)

    cv = _Canvas(ph)
    cv.scale = scale
    cv.ops.append("1 1 1 rg 0 0 %.2f %.2f re f" % (pw, ph))
    for p in prims:
        if p[0] == "path":
            cv.path(p[1], p[2], p[3], p[4], p[5], tx)
        elif p[0] == "text":
            x, y = tx(p[1], p[2])
            cv.text(x, y, p[3], p[4], p[5], p[6], p[7])
        elif p[0] == "label_bg":
            x, y = tx(p[1], p[2] + p[4])
            cv.ops.append("q 1 1 1 rg %.2f %.2f %.2f %.2f re f Q" % (x, y, p[3] * scale, p[4] * scale))
    # legend and title block at the bottom, drawn at 1:1
    cv.scale = 1.0
    if lp:
        top = margin + tb_h + 8 + lh  # legend sits just above the title block

        def ltx(x, y):
            return margin + x, top - y
        for p in lp:
            if p[0] == "path":
                cv.path(p[1], p[2], p[3], p[4], p[5], ltx)
            else:
                x, y = ltx(p[1], p[2])
                cv.text(x, y, p[3], p[4], p[5], p[6], p[7])
    if title_block:
        _title_block(cv, pw, margin, tb_h, title_block)
    stream = zlib.compress("\n".join(cv.ops).encode("latin-1"))
    return _assemble(pw, ph, stream, title_block.get("Title", "Diagram") if title_block else "Diagram")


def _title_block(cv, pw, margin, h, tb):
    """ISO-7200-style title block: label/value cells along the bottom of the sheet."""
    x0, y0, w = margin, margin, pw - 2 * margin
    cv.ops.append("q 0.2 0.2 0.2 RG 0.8 w %.2f %.2f %.2f %.2f re S Q" % (x0, y0, w, h))
    fields = list(tb.items())
    title = fields.pop(0) if fields else ("Title", "")
    tw = w * 0.34
    cv.ops.append("q 0.2 0.2 0.2 RG 0.6 w %.2f %.2f m %.2f %.2f l S Q" % (x0 + tw, y0, x0 + tw, y0 + h))
    cv.text(x0 + 8, y0 + h - 14, title[0].upper(), 6.5, False, "#6b7280", "start")
    cv.text(x0 + 8, y0 + h - 32, str(title[1])[:60], 13, True, "#111827", "start")
    rows, cols = 2, (len(fields) + 1) // 2 or 1
    cw = (w - tw) / cols
    for i, (k, v) in enumerate(fields):
        c, r = i // rows, i % rows
        cx, cy = x0 + tw + c * cw, y0 + h - (r + 1) * h / rows
        cv.ops.append("q 0.6 0.6 0.6 RG 0.4 w %.2f %.2f %.2f %.2f re S Q" % (cx, cy, cw, h / rows))
        cv.text(cx + 5, cy + h / rows - 10, k.upper(), 6, False, "#6b7280", "start")
        val = str(v)
        while G.text_width(val, 9.5, False) > cw - 10 and len(val) > 4:
            val = val.rstrip(".")[:-1] + "..."
        cv.text(cx + 5, cy + 7, val, 9.5, False, "#111827", "start")


def _assemble(pw, ph, stream, title):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> "
         f"/Contents 4 0 R >>").encode(),
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Title <FEFF" + title.encode("utf-16-be").hex().upper().encode() + b"> /Producer (Workbench) /CreationDate (D:"
        + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ").encode() + b") >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, len(objs), xref)
    return bytes(out)
