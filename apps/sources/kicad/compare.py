"""What changed between two versions of a schematic or PCB: parts, values, footprints, nets, positions."""
import math

from .schematic import _ref_key

MOVE_TOLERANCE = 0.01  # mm


def _match(old, new, key=lambda p: p.uuid, ref=lambda p: p.ref):
    """Pairs (old, new) matched by uuid, then by reference; plus the unmatched ones on each side."""
    by_uuid = {key(p): p for p in old if key(p)}
    pairs, rest_new = [], []
    used = set()
    for p in new:
        q = by_uuid.get(key(p)) if key(p) else None
        if q is not None and id(q) not in used:
            pairs.append((q, p))
            used.add(id(q))
        else:
            rest_new.append(p)
    rest_old = [q for q in old if id(q) not in used]
    by_ref = {ref(q): q for q in rest_old if ref(q)}
    added = []
    for p in rest_new:
        q = by_ref.pop(ref(p), None)
        if q is not None:
            pairs.append((q, p))
            used.add(id(q))
        else:
            added.append(p)
    removed = [q for q in old if id(q) not in used]
    return pairs, added, removed


def _sorted_refs(parts):
    return sorted((p.ref for p in parts), key=_ref_key)


def compare_schematics(a, b):
    real = lambda s: not s.power and not s.ref.startswith("#")  # noqa: E731
    pairs, added, removed = _match([s for s in a.symbols if real(s)], [s for s in b.symbols if real(s)])
    changed, hl_a, hl_b = [], {}, {}
    for old, new in pairs:
        what = []
        if old.ref != new.ref:
            what.append(f"renamed from {old.ref}")
        if old.value != new.value:
            what.append(f"value {old.value} → {new.value}")
        if old.footprint != new.footprint:
            what.append(f"footprint {old.footprint or '—'} → {new.footprint or '—'}")
        if old.dnp != new.dnp:
            what.append("now not fitted" if new.dnp else "now fitted")
        if what:
            changed.append({"ref": new.ref, "what": what})
            hl_a[old.uuid], hl_b[new.uuid] = "changed", "changed"
    for p in added:
        hl_b[p.uuid] = "added"
    for p in removed:
        hl_a[p.uuid] = "removed"
    na, nb = a.nets(), b.nets()
    return {"added": _sorted_refs(added), "removed": _sorted_refs(removed),
            "changed": sorted(changed, key=lambda c: _ref_key(c["ref"])),
            "nets_added": sorted(nb - na), "nets_removed": sorted(na - nb), "highlight_a": hl_a, "highlight_b": hl_b}


def _length(board):
    total = sum(math.hypot(x2 - x1, y2 - y1) for layer, x1, y1, x2, y2, w, net in board.tracks)
    for layer, s, m, e, w, net in board.arcs:
        total += math.hypot(m[0] - s[0], m[1] - s[1]) + math.hypot(e[0] - m[0], e[1] - m[1])  # close enough
    return round(total, 1)


def compare_pcbs(a, b):
    pairs, added, removed = _match([f for f in a.footprints if f.ref], [f for f in b.footprints if f.ref])
    changed, hl_a, hl_b = [], {}, {}
    for old, new in pairs:
        what = []
        if old.ref != new.ref:
            what.append(f"renamed from {old.ref}")
        if old.value != new.value:
            what.append(f"value {old.value} → {new.value}")
        if old.lib_id != new.lib_id:
            what.append(f"footprint {old.lib_id} → {new.lib_id}")
        dist = math.hypot(new.x - old.x, new.y - old.y)
        if dist > MOVE_TOLERANCE:
            what.append(f"moved {dist:.2f} mm")
        if abs((new.angle - old.angle) % 360) > 0.01:
            what.append(f"rotated {old.angle:g}° → {new.angle:g}°")
        if old.side != new.side:
            what.append(f"moved to the {new.side}")
        if what:
            changed.append({"ref": new.ref, "what": what})
            hl_a[old.uuid], hl_b[new.uuid] = "changed", "changed"
    for p in added:
        hl_b[p.uuid] = "added"
    for p in removed:
        hl_a[p.uuid] = "removed"
    na, nb = set(a.nets.values()) - {""}, set(b.nets.values()) - {""}
    return {"added": _sorted_refs(added), "removed": _sorted_refs(removed),
            "changed": sorted(changed, key=lambda c: _ref_key(c["ref"])),
            "nets_added": sorted(nb - na), "nets_removed": sorted(na - nb),
            "tracks": (len(a.tracks) + len(a.arcs), len(b.tracks) + len(b.arcs)), "length": (_length(a), _length(b)),
            "vias": (len(a.vias), len(b.vias)), "highlight_a": hl_a, "highlight_b": hl_b}


def compare(kind, a, b):
    return compare_schematics(a, b) if kind == "sch" else compare_pcbs(a, b)
