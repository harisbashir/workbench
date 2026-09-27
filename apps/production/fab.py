"""Files for PCB assembly houses (JLCPCB, Seeed Fusion) and the in-house part list.

The idea: the assembler places every part marked "fitted by assembly house";
parts marked "in house" (usually through-hole connectors, or parts the
assembler doesn't stock) are left out of their BOM and placement file, and
become the finishing checklist of the build.
"""
import csv
import io
import re


ASSEMBLERS = {
    "jlc": {"name": "JLCPCB", "bom": "BOM for JLCPCB", "cpl": "CPL for JLCPCB",
            "note": "Upload the Gerber zip, then choose PCB Assembly and upload the BOM and CPL files. "
                    "Check the part placement preview: some footprints need a rotation correction on JLCPCB."},
    "seeed": {"name": "Seeed Fusion", "bom": "BOM for Seeed Fusion", "cpl": "Pick & place for Seeed Fusion",
              "note": "Upload the Gerber zip, choose PCB Assembly (Fusion PCBA) and add the BOM and pick-and-place files."},
}

HEADER_ALIASES = {
    "ref": ("ref", "designator", "reference", "part", "refdes", "component"),
    "val": ("val", "value", "comment"),
    "package": ("package", "footprint"),
    "x": ("posx", "midx", "centerx", "centerx(mm)", "x", "refx", "locationx"),
    "y": ("posy", "midy", "centery", "centery(mm)", "y", "refy", "locationy"),
    "rot": ("rot", "rotation", "angle"),
    "side": ("side", "layer", "tb"),
}


def _norm(h):
    """'Center-X(mil)' -> 'centerx'."""
    return re.sub(r"[^a-z]", "", re.sub(r"\(.*?\)", "", h.lower())) if h else ""


def _unit_scale(h):
    h = (h or "").lower()
    return 0.0254 if "mil" in h else (25.4 if "(in" in h else 1.0)


def _num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", v or "")
    if not m:
        return None
    val = float(m.group())
    if "mil" in (v or "").lower():
        val *= 0.0254
    elif (v or "").strip().lower().endswith("in"):
        val *= 25.4
    return val


def parse_placements(data):
    """Read a pick-and-place file: KiCad .pos (CSV or ASCII), JLC CPL, Altium or EasyEDA.

    Returns (rows, warnings); rows are dicts with ref, val, package, x, y, rot, side.
    """
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    text = text.replace("\r\n", "\n")
    warnings = []
    lines = [l for l in text.split("\n") if l.strip()]
    if lines and lines[0].startswith("###") or any(l.startswith("# Ref") for l in lines[:10]):
        # KiCad ASCII .pos: "# Ref Val Package PosX PosY Rot Side" then whitespace-separated rows
        rows = []
        for l in lines:
            if l.startswith("#") or l.startswith("##"):
                continue
            parts = l.split()
            if len(parts) >= 7:
                rows.append({"ref": parts[0], "val": parts[1], "package": parts[2], "x": _num(parts[3]),
                             "y": _num(parts[4]), "rot": _num(parts[5]) or 0.0, "side": parts[6].lower()})
        return rows, warnings
    # Find the header row (Altium files have a preamble)
    start = 0
    for i, l in enumerate(lines[:30]):
        n = [_norm(c) for c in re.split(r"[,;\t]", l)]
        if any(c in HEADER_ALIASES["ref"] for c in n) and any(c in HEADER_ALIASES["x"] for c in n):
            start = i
            break
    body = "\n".join(lines[start:])
    try:
        dialect = csv.Sniffer().sniff(body[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(body), dialect)
    header = next(reader, [])
    idx = {}
    for key, aliases in HEADER_ALIASES.items():
        for i, h in enumerate(header):
            if _norm(h) in aliases and key not in idx:
                idx[key] = i
    if "ref" not in idx or "x" not in idx or "y" not in idx:
        return [], ["This doesn't look like a pick-and-place file (no Designator / X / Y columns)."]
    xscale = _unit_scale(header[idx["x"]])
    yscale = _unit_scale(header[idx["y"]])
    rows = []
    for r in reader:
        if not r or len(r) <= max(idx.values()):
            continue

        def col(k):
            return r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        side = col("side").lower()
        side = "bottom" if side.startswith(("b", "bot")) else "top"
        x, y = col("x"), col("y")
        rows.append({"ref": col("ref"), "val": col("val"), "package": col("package"),
                     "x": _num(x) * (1 if "mil" in x.lower() else xscale) if _num(x) is not None else None,
                     "y": _num(y) * (1 if "mil" in y.lower() else yscale) if _num(y) is not None else None,
                     "rot": _num(col("rot")) or 0.0, "side": side})
    return [r for r in rows if r["ref"] and r["x"] is not None and r["y"] is not None], warnings


def lcsc_number(part):
    """The part's LCSC/JLC number, if its supplier is LCSC or JLCPCB."""
    sup = (part.supplier.name if part.supplier_id else "").lower()
    if ("lcsc" in sup or "jlc" in sup) and part.supplier_sku:
        return part.supplier_sku
    m = re.search(r"\bC\d{3,8}\b", part.notes or "")
    return m.group() if m else ""


def plan(revision, placements=None):
    """Split a revision's BOM for assembly. Returns a dict with fab/house lines and checks."""
    # The bare board itself is made by the fab as part of the order, not placed.
    lines = [l for l in revision.bom_lines.select_related("part", "part__supplier") if l.part.category != "pcb"]
    fab = [l for l in lines if not l.dnp and l.fitted_by == "fab"]
    house = [l for l in lines if not l.dnp and l.fitted_by == "house"]
    dnp = [l for l in lines if l.dnp]
    issues = []
    fab_refs = {r for l in fab for r in l.refs}
    house_refs = {r for l in house for r in l.refs}
    dnp_refs = {r for l in dnp for r in l.refs}
    for l in fab:
        if not l.refs:
            issues.append(("warn", f"{l.part.ipn} has no reference designators, so it can't be placed."))
        if len(l.refs) and len(l.refs) != l.quantity:
            issues.append(("warn", f"{l.part.ipn}: quantity {l.quantity} but {len(l.refs)} references."))
    placed = {}
    if placements is not None:
        placed = {p["ref"]: p for p in placements}
        missing = sorted(fab_refs - set(placed), key=_ref_key)
        if missing:
            issues.append(("bad", f"Not in the pick-and-place file: {', '.join(missing[:12])}{'…' if len(missing) > 12 else ''}"))
        extra = sorted(set(placed) - fab_refs - house_refs - dnp_refs, key=_ref_key)
        if extra:
            issues.append(("warn", f"In the pick-and-place file but not on the BOM (left out): {', '.join(extra[:12])}"
                                   f"{'…' if len(extra) > 12 else ''}"))
    return {"fab": fab, "house": house, "dnp": dnp, "issues": issues, "placed": placed,
            "fab_refs": fab_refs, "house_refs": house_refs, "dnp_refs": dnp_refs,
            "no_number": [l for l in fab if not lcsc_number(l.part) and not l.part.mpn]}


def _ref_key(ref):
    m = re.match(r"([A-Za-z_]*)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref)


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8-sig")


def bom_csv(p, assembler):
    if assembler == "seeed":
        rows = [["Designator", "Manufacturer Part Number or Seeed SKU", "Qty", "Manufacturer", "Description"]]
        for l in p["fab"]:
            rows.append([",".join(l.refs) or l.references, l.part.mpn or lcsc_number(l.part), l.quantity,
                         l.part.manufacturer, l.part.description])
        return _csv(rows)
    rows = [["Comment", "Designator", "Footprint", "LCSC Part #"]]
    for l in p["fab"]:
        footprint = (l.part.footprint or "").split(":")[-1]
        rows.append([l.part.value or l.part.description, ",".join(l.refs) or l.references, footprint, lcsc_number(l.part)])
    return _csv(rows)


def cpl_csv(p, placements):
    rows = [["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]]
    for ref in sorted(p["fab_refs"], key=_ref_key):
        pl = p["placed"].get(ref)
        if not pl:
            continue
        rows.append([ref, f"{pl['x']:.4f}mm", f"{pl['y']:.4f}mm", "Bottom" if pl["side"] == "bottom" else "Top",
                     f"{pl['rot'] % 360:g}"])
    return _csv(rows)


def house_csv(p):
    rows = [["Part number", "Description", "Value", "References", "Qty per board", "Stock", "Location", "Supplier"]]
    for l in p["house"]:
        rows.append([l.part.ipn, l.part.description, l.part.value, ", ".join(l.refs), l.quantity, l.part.stock,
                     l.part.location, l.part.supplier or ""])
    return _csv(rows)
