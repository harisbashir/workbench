"""Small 3D files in every supported format, built in code (used by the tests and the demo data)."""
import io
import json
import struct
import zipfile

import numpy as np

CUBE_V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=np.float64)
CUBE_T = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                   [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])


def box(x, y, z, sx, sy, sz):
    return CUBE_V * [sx, sy, sz] + [x, y, z], CUBE_T.copy()


def boxes(items):
    """Several boxes (x, y, z, sx, sy, sz) as one vertex/triangle list."""
    vs, ts, offset = [], [], 0
    for b in items:
        v, t = box(*b)
        ts.append(t + offset)
        vs.append(v)
        offset += len(v)
    return np.concatenate(vs), np.concatenate(ts)


def stl_binary(v=None, t=None):
    v, t = (CUBE_V * 10, CUBE_T) if v is None else (v, t)
    out = bytearray(b"workbench test".ljust(80, b" ") + struct.pack("<I", len(t)))
    for tri in t:
        out += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v[i]) for i in tri) + b"\0\0"
    return bytes(out)


def stl_ascii():
    lines = ["solid bracket"]
    for tri in CUBE_T:
        lines.append(" facet normal 0 0 0\n  outer loop")
        lines += [f"   vertex {a:.3f} {b:.3f} {c:.3f}" for a, b, c in CUBE_V[tri] * 5]
        lines.append("  endloop\n endfacet")
    lines.append("endsolid bracket")
    return "\n".join(lines).encode()


def obj():
    out = ["# test", "o lid"] + [f"v {a} {b} {c}" for a, b, c in CUBE_V * 20]
    out += ["f 1 3 2 4"]  # a quad (fan-triangulated)
    out += [f"f {a + 1}/1 {b + 1}/1 {c + 1}/1" for a, b, c in CUBE_T[2:]]
    return "\n".join(out).encode()


def make_3mf(objects):
    """objects: list of (name, vertices, triangles, '#RRGGBB[AA]', (tx, ty, tz)) → 3MF bytes."""
    mats = "".join(f'<base name="{n}" displaycolor="{c}"/>' for n, _, _, c, _ in objects)
    objs, items = [], []
    for i, (n, v, t, _c, off) in enumerate(objects, start=2):
        vs = "".join(f'<vertex x="{a:.3f}" y="{b:.3f}" z="{c:.3f}"/>' for a, b, c in v)
        ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in t)
        objs.append(f'<object id="{i}" name="{n}" type="model" pid="1" pindex="{i - 2}"><mesh><vertices>{vs}</vertices>'
                    f'<triangles>{ts}</triangles></mesh></object>')
        tr = "" if not any(off) else f' transform="1 0 0 0 1 0 0 0 1 {off[0]} {off[1]} {off[2]}"'
        items.append(f'<item objectid="{i}"{tr}/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<basematerials id="1">{mats}</basematerials>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


def three_mf():
    """A 3MF with a coloured base and a lid placed with a transform (like PrusaSlicer / Bambu exports)."""
    return make_3mf([("Base", *box(0, 0, 0, 80, 50, 25), "#2F3A45", (0, 0, 0)),
                     ("Lid", *box(0, 0, 0, 80, 50, 3), "#E0E4E8", (0, 0, 30))])


def glb(shapes, generator="workbench-test", y_up=True):
    """shapes: list of (name, vertices_mm, triangles, rgba). Writes metres, Y-up unless y_up=False."""
    bin_parts, views, accessors, meshes, nodes, materials = [], [], [], [], [], []
    offset = 0
    for i, (name, v, t, color) in enumerate(shapes):
        vv = np.asarray(v, dtype=np.float64) / 1000.0
        if y_up:
            vv = vv[:, [0, 2, 1]] * [1, 1, -1]
        vb = vv.astype("<f4").tobytes()
        ib = np.asarray(t, dtype="<u4").tobytes()
        for blob, target in ((vb, 34962), (ib, 34963)):
            views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(blob), "target": target})
            bin_parts.append(blob + b"\0" * (-len(blob) % 4))
            offset += len(blob) + (-len(blob) % 4)
        accessors.append({"bufferView": len(views) - 2, "componentType": 5126, "count": len(vv), "type": "VEC3",
                          "min": vv.min(axis=0).tolist(), "max": vv.max(axis=0).tolist()})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5125, "count": int(np.asarray(t).size), "type": "SCALAR"})
        materials.append({"pbrMetallicRoughness": {"baseColorFactor": list(color)}})
        meshes.append({"name": name, "primitives": [{"attributes": {"POSITION": len(accessors) - 2}, "indices": len(accessors) - 1, "material": i}]})
        nodes.append({"mesh": i, "name": name})
    doc = {"asset": {"version": "2.0", "generator": generator}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
           "nodes": nodes, "meshes": meshes, "materials": materials, "accessors": accessors, "bufferViews": views,
           "buffers": [{"byteLength": offset}]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    binary = b"".join(bin_parts)
    total = 12 + 8 + len(js) + 8 + len(binary)
    return (b"glTF" + struct.pack("<II", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


def vrml_board(width=50, height=32, parts=(("U1", 12, 10, 7, 7, 1, (0.1, 0.1, 0.1)), ("J1", 40, 20, 8, 6, 5, (0.9, 0.9, 0.9)))):
    """VRML like KiCad's export: a green board with components, DEF/USE materials, in millimetres."""
    def shape(v, t, mat):
        pts = ", ".join(f"{a:.3f} {b:.3f} {c:.3f}" for a, b, c in v)
        idx = ", ".join(f"{a},{b},{c},-1" for a, b, c in t)
        return f"Shape {{ appearance Appearance {{ material {mat} }} geometry IndexedFaceSet {{ solid FALSE coord Coordinate {{ point [ {pts} ] }} coordIndex [ {idx} ] }} }}"
    v, t = box(0, 0, 0, width, height, 1.6)
    body = [shape(v, t, "DEF PCB_GREEN Material { diffuseColor 0.07 0.3 0.12 specularColor 0.2 0.2 0.2 }")]
    for ref, x, y, w, h, z, col in parts:
        v, t = box(-w / 2, -h / 2, 0, w, h, z)
        body.append(f"# {ref}\nDEF {ref} Transform {{ translation {x} {y} 1.6 rotation 0 0 1 0 children [ "
                    + shape(v, t, f"Material {{ diffuseColor {col[0]} {col[1]} {col[2]} }}") + " ] }")
    body.append("Transform { translation 5 5 -1 children [ Shape { appearance Appearance { material USE PCB_GREEN } "
                "geometry IndexedFaceSet { coord Coordinate { point [ 0 0 0, 2 0 0, 2 2 0, 0 2 0 ] } coordIndex [ 0 1 2 3 -1 ] } } ] }")
    return ("#VRML V2.0 utf8\nWorldInfo { title \"test board\" }\nTransform { children [\n" + "\n".join(body) + "\n] }\n").encode()


# --- demo models -----------------------------------------------------------------------------------

def enclosure_assembly():
    """A 3D-printed enclosure (base + see-through lid) with three boards inside, as a 3MF assembly."""
    L, Wd, H, t = 110.0, 70.0, 34.0, 2.2
    base = boxes([(0, 0, 0, L, Wd, t), (0, 0, 0, t, Wd, H), (L - t, 0, 0, t, Wd, H), (0, 0, 0, L, t, H), (0, Wd - t, 0, L, t, H)]
                 + [(x, y, t, 5, 5, 6) for x in (6, L - 11) for y in (6, Wd - 11)])       # standoffs
    lid = boxes([(0, 0, 0, L, Wd, t), (t + 0.3, t + 0.3, -3, L - 2 * t - 0.6, 1.5, 3), (t + 0.3, Wd - t - 1.8, -3, L - 2 * t - 0.6, 1.5, 3)])
    power = boxes([(0, 0, 0, 50, 32, 1.6), (12, 8, 1.6, 7, 7, 1.2), (15, 20, 1.6, 6, 6, 4), (44, 11, 1.6, 4, 8, 6)])
    ctrl = boxes([(0, 0, 0, 38, 60, 1.6), (13, 22, 1.6, 10, 10, 1.4), (4, 45, 1.6, 9, 7, 3.2), (26, 6, 1.6, 8, 5, 2)])
    panel = boxes([(0, 0, 0, 2, 60, 28), (2, 10, 8, 3, 8, 8), (2, 40, 8, 3, 8, 8)])
    return make_3mf([
        ("Enclosure base", *base, "#3A4048", (0, 0, 0)),
        ("Lid", *lid, "#C9D3DD66", (0, 0, H + 18)),
        ("Power board", *power, "#1E6B3A", (8, 30, 8.2)),
        ("Control board", *ctrl, "#1F5C99", (60, 5, 8.2)),
        ("Front panel board", *panel, "#0F4D2A", (L - t - 6, 5, 4)),
    ])


def bezel_stl():
    v, t = boxes([(0, 0, 0, 70, 3, 36), (6, -2, 8, 12, 2, 12), (24, -2, 8, 12, 2, 12), (52, -2, 10, 10, 2, 16)])
    return stl_binary(v, t)


def board_vrml(parts, width=50.0, height=32.0):
    """A KiCad-style VRML board model: green PCB with a box per component. parts: (ref, x, y, package, rotation)."""
    def size(pkg):
        pkg = pkg.upper()
        for key, dims in (("LQFP", (7, 7, 1.4)), ("QFN", (3, 3, 0.9)), ("RGT", (3, 3, 0.9)), ("SOT-23-6", (3, 1.7, 1.1)),
                          ("SOT-23", (2.9, 1.3, 1.0)), ("1206", (3.2, 1.6, 1.6)), ("0805", (2.0, 1.25, 1.2)), ("0603", (1.6, 0.8, 0.8)),
                          ("SRN6045", (6, 6, 4.5)), ("USB", (9, 7.4, 3.2)), ("JST", (9.9, 4.5, 6)), ("LED", (1.6, 0.8, 0.7))):
            if key in pkg:
                return dims
        return (2, 2, 1)

    def color(pkg, ref):
        if ref.startswith("C"):
            return (0.78, 0.62, 0.38)
        if ref.startswith("R"):
            return (0.12, 0.12, 0.12)
        if ref.startswith("J"):
            return (0.92, 0.92, 0.9) if "JST" in pkg.upper() else (0.75, 0.76, 0.8)
        if ref.startswith("L"):
            return (0.35, 0.35, 0.38)
        if ref.startswith("D"):
            return (0.2, 0.85, 0.3)
        return (0.14, 0.14, 0.15)

    def shape(v, t, mat):
        pts = ", ".join(f"{a:.3f} {b:.3f} {c:.3f}" for a, b, c in v)
        idx = ", ".join(f"{a},{b},{c},-1" for a, b, c in t)
        return (f"Shape {{ appearance Appearance {{ material {mat} }} geometry IndexedFaceSet {{ solid FALSE "
                f"coord Coordinate {{ point [ {pts} ] }} coordIndex [ {idx} ] }} }}")

    v, t = box(0, 0, 0, width, height, 1.6)
    out = [shape(v, t, "DEF PCB_MASK Material { diffuseColor 0.08 0.36 0.16 specularColor 0.3 0.3 0.3 }")]
    for ref, x, y, pkg, rot in parts:
        sx, sy, sz = size(pkg)
        if rot % 180 == 90:
            sx, sy = sy, sx
        v, t = box(-sx / 2, -sy / 2, 0, sx, sy, sz)
        c = color(pkg, ref)
        out.append(f"DEF {ref} Transform {{ translation {x} {y} 1.6 children [ "
                   + shape(v, t, f"Material {{ diffuseColor {c[0]} {c[1]} {c[2]} }}") + " ] }")
    return ("#VRML V2.0 utf8\nWorldInfo { title \"KiCad board\" info [ \"units: mm\" ] }\nTransform { children [\n"
            + "\n".join(out) + "\n] }\n").encode()
