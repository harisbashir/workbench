"""Readers for the 3D formats Workbench can show.

    STL (binary and ASCII), OBJ, 3MF, glTF / GLB, VRML (.wrl, as exported by KiCad),
    STEP / IGES (converted with OpenCASCADE through the optional `cascadio` package).

Each reader returns a Mesh in millimetres with Z up.
"""
import base64
import io
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

from .mesh import Mesh, MeshError, Part, weld

# --- STL --------------------------------------------------------------------------------


def read_stl(data, name="Model"):
    if len(data) < 84:
        raise MeshError("The STL file is too short.")
    n = struct.unpack("<I", data[80:84])[0]
    is_binary = len(data) == 84 + n * 50
    if not is_binary and data[:5].lower() == b"solid":
        return _read_stl_ascii(data, name)
    if not is_binary:
        if len(data) < 84 + n * 50:
            raise MeshError("The STL file is shorter than it says; it may be cut off.")
    rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    arr = np.frombuffer(data, dtype=rec, count=n, offset=84)
    v, t = weld(arr["v"].reshape(-1, 3))
    return Mesh([Part(name, v, t)])


def _read_stl_ascii(data, name):
    text = data.decode("ascii", "replace")
    nums = re.findall(r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", text)
    if not nums or len(nums) % 3:
        raise MeshError("The STL file doesn't contain any triangles.")
    v, t = weld(np.array(nums, dtype=np.float64))
    solid = re.match(r"solid\s+([^\r\n]*)", text)
    return Mesh([Part((solid.group(1).strip() if solid else "") or name, v, t)])


# --- OBJ --------------------------------------------------------------------------------


def read_obj(data, name="Model"):
    text = data.decode("utf-8", "replace")
    verts = []
    groups = {}  # name -> list of index triples (0-based)
    current = name
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        parts = line.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v" and len(parts) >= 4:
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag in ("o", "g") and len(parts) > 1:
            current = " ".join(parts[1:])
        elif tag == "f" and len(parts) >= 4:
            idx = []
            for p in parts[1:]:
                i = int(p.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            tri = groups.setdefault(current, [])
            for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                tri.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not groups:
        raise MeshError("The OBJ file doesn't contain any faces.")
    v = np.array(verts, dtype=np.float32)
    parts = []
    for gname, tris in groups.items():
        t = np.array(tris, dtype=np.int64)
        used, inverse = np.unique(t, return_inverse=True)
        parts.append(Part(gname, v[used], inverse.reshape(-1, 3)))
    return Mesh(parts)


# --- 3MF --------------------------------------------------------------------------------

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_MAT = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def _hex_color(s):
    s = (s or "").lstrip("#")
    if len(s) not in (6, 8):
        return None
    try:
        vals = [int(s[i:i + 2], 16) / 255 for i in range(0, len(s), 2)]
    except ValueError:
        return None
    return tuple(vals + [1.0] * (4 - len(vals)))


def _3mf_matrix(s):
    if not s:
        return np.eye(4)
    v = [float(x) for x in s.split()]
    if len(v) != 12:
        return np.eye(4)
    m = np.eye(4)
    # 3MF transforms are row vectors: p' = p · M (4x3). Convert to column form.
    m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
    m[:3, 3] = v[9:12]
    return m


def read_3mf(data, name="Model"):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MeshError("This 3MF file isn't a valid zip archive.")
    names = z.namelist()
    main = None
    if "_rels/.rels" in names:
        rels = ET.fromstring(z.read("_rels/.rels"))
        for r in rels:
            if r.get("Type", "").endswith("/3dmodel"):
                main = r.get("Target", "").lstrip("/")
    if not main or main not in names:
        main = next((n for n in names if n.lower().endswith(".model")), None)
    if not main:
        raise MeshError("No 3D model was found inside the 3MF file.")

    models = {}  # path -> (objects, colors, unit)

    def load(path):
        path = path.lstrip("/")
        if path in models:
            return models[path]
        if path not in names:
            raise MeshError(f"The 3MF file refers to {path}, which is missing.")
        root = ET.fromstring(z.read(path))
        unit = UNIT_MM.get(root.get("unit", "millimeter"), 1.0)
        res = root.find(f"{{{NS_CORE}}}resources")
        objects, colors = {}, {}
        if res is not None:
            for bm in res.findall(f"{{{NS_CORE}}}basematerials"):
                colors[bm.get("id")] = [_hex_color(b.get("displaycolor")) for b in bm.findall(f"{{{NS_CORE}}}base")]
            for cg in res.findall(f"{{{NS_MAT}}}colorgroup"):
                colors[cg.get("id")] = [_hex_color(c.get("color")) for c in cg.findall(f"{{{NS_MAT}}}color")]
            for obj in res.findall(f"{{{NS_CORE}}}object"):
                objects[obj.get("id")] = obj
        models[path] = (objects, colors, unit, root)
        return models[path]

    parts = []

    def color_of(colors, pid, pindex):
        group = colors.get(pid)
        if group:
            try:
                return group[int(pindex or 0)]
            except (ValueError, IndexError):
                return None
        return None

    def add_object(path, oid, matrix, depth=0):
        if depth > 16:
            return
        objects, colors, unit, _ = load(path)
        obj = objects.get(oid)
        if obj is None:
            return
        mesh = obj.find(f"{{{NS_CORE}}}mesh")
        if mesh is not None:
            vs = mesh.find(f"{{{NS_CORE}}}vertices")
            ts = mesh.find(f"{{{NS_CORE}}}triangles")
            if vs is None or ts is None:
                return
            v = np.array([(float(e.get("x")), float(e.get("y")), float(e.get("z"))) for e in vs], dtype=np.float64) * unit
            tri_el = list(ts)
            t = np.array([(int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))) for e in tri_el], dtype=np.int64)
            color = color_of(colors, obj.get("pid"), obj.get("pindex"))
            if color is None and tri_el:
                first = tri_el[0]
                color = color_of(colors, first.get("pid") or obj.get("pid"), first.get("p1"))
            label = obj.get("name") or name
            if len(t):
                parts.append(Part(label, v, t, color).transformed(matrix))
            return
        comps = obj.find(f"{{{NS_CORE}}}components")
        if comps is not None:
            for c in comps.findall(f"{{{NS_CORE}}}component"):
                sub_path = c.get(f"{{{NS_PROD}}}path") or path
                add_object(sub_path, c.get("objectid"), matrix @ _3mf_matrix(c.get("transform")), depth + 1)

    objects, colors, unit, root = load(main)
    build = root.find(f"{{{NS_CORE}}}build")
    items = build.findall(f"{{{NS_CORE}}}item") if build is not None else []
    if not items:  # no build section: show every object
        for oid in objects:
            add_object(main, oid, np.eye(4))
    for item in items:
        add_object(item.get(f"{{{NS_PROD}}}path") or main, item.get("objectid"), _3mf_matrix(item.get("transform")))
    return Mesh(parts)


# --- glTF / GLB ---------------------------------------------------------------------------

_COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _quat_matrix(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # column-major
    m = np.eye(4)
    s = node.get("scale", [1, 1, 1])
    m[:3, :3] = _quat_matrix(node.get("rotation", [0, 0, 0, 1])) @ np.diag(s)
    m[:3, 3] = node.get("translation", [0, 0, 0])
    return m


def read_gltf(data, name="Model", z_up=None):
    """GLB (binary) or .gltf (JSON with embedded buffers).

    glTF is Y-up in metres. Files written by OpenCASCADE (the STEP converter, KiCad) are Z-up.
    """
    bin_chunk = b""
    if data[:4] == b"glTF":
        _, version, length = struct.unpack("<III", data[:12])
        if version != 2:
            raise MeshError("Only glTF 2.0 files are supported.")
        pos, doc = 12, None
        while pos + 8 <= len(data):
            clen, ctype = struct.unpack("<II", data[pos:pos + 8])
            chunk = data[pos + 8:pos + 8 + clen]
            if ctype == 0x4E4F534A:
                doc = json.loads(chunk)
            elif ctype == 0x004E4942:
                bin_chunk = chunk
            pos += 8 + clen
        if doc is None:
            raise MeshError("The GLB file has no scene description.")
    else:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MeshError("This doesn't look like a glTF file.")
    if "KHR_draco_mesh_compression" in doc.get("extensionsRequired", []):
        raise MeshError("This glTF uses Draco compression, which isn't supported. Export it without compression.")

    buffers = []
    for b in doc.get("buffers", []):
        uri = b.get("uri")
        if uri is None:
            buffers.append(bin_chunk)
        elif uri.startswith("data:"):
            buffers.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            raise MeshError(f"The glTF refers to a separate file ({uri}). Export as a single .glb file instead.")

    def accessor(i):
        a = doc["accessors"][i]
        dtype = np.dtype(_COMPONENT[a["componentType"]]).newbyteorder("<")
        n, comps = a["count"], _COUNT[a["type"]]
        if "bufferView" not in a:
            return np.zeros((n, comps), dtype=dtype)
        bv = doc["bufferViews"][a["bufferView"]]
        buf = buffers[bv["buffer"]]
        start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = bv.get("byteStride") or dtype.itemsize * comps
        if stride == dtype.itemsize * comps:
            arr = np.frombuffer(buf, dtype=dtype, count=n * comps, offset=start).reshape(n, comps)
        else:
            raw = np.frombuffer(buf, dtype=np.uint8, count=stride * (n - 1) + dtype.itemsize * comps, offset=start)
            arr = np.lib.stride_tricks.as_strided(raw, shape=(n, dtype.itemsize * comps), strides=(stride, 1))
            arr = np.ascontiguousarray(arr).view(dtype).reshape(n, comps)
        return arr

    materials = doc.get("materials", [])
    mesh_cache = {}

    def mesh_parts(mi):
        if mi in mesh_cache:
            return mesh_cache[mi]
        out = []
        m = doc["meshes"][mi]
        for prim in m.get("primitives", []):
            mode = prim.get("mode", 4)
            if mode not in (4, 5, 6) or "POSITION" not in prim.get("attributes", {}):
                continue
            v = accessor(prim["attributes"]["POSITION"]).astype(np.float64)
            idx = accessor(prim["indices"]).reshape(-1).astype(np.int64) if "indices" in prim else np.arange(len(v))
            if mode == 4:
                t = idx[: len(idx) // 3 * 3].reshape(-1, 3)
            elif mode == 5:  # strip
                t = np.array([(idx[i], idx[i + 1], idx[i + 2]) if i % 2 == 0 else (idx[i + 1], idx[i], idx[i + 2])
                              for i in range(len(idx) - 2)], dtype=np.int64).reshape(-1, 3)
            else:  # fan
                t = np.array([(idx[0], idx[i], idx[i + 1]) for i in range(1, len(idx) - 1)], dtype=np.int64).reshape(-1, 3)
            color = None
            if "material" in prim and prim["material"] < len(materials):
                f = materials[prim["material"]].get("pbrMetallicRoughness", {}).get("baseColorFactor")
                if f:
                    color = tuple(f)
            out.append(Part(m.get("name") or name, v, t, color))
        mesh_cache[mi] = out
        return out

    parts = []
    nodes = doc.get("nodes", [])

    def visit(ni, parent, depth=0):
        if depth > 64:
            return
        node = nodes[ni]
        mat = parent @ _node_matrix(node)
        if "mesh" in node:
            for p in mesh_parts(node["mesh"]):
                q = p.transformed(mat)
                if node.get("name") and not node["name"].startswith("=>"):
                    q.name = node["name"][:120]
                parts.append(q)
        for c in node.get("children", []):
            visit(c, mat, depth + 1)

    scenes = doc.get("scenes") or [{"nodes": list(range(len(nodes)))}]
    scene = scenes[doc.get("scene", 0)] if scenes else {"nodes": []}
    for ni in scene.get("nodes", []):
        visit(ni, np.eye(4))
    if not parts and doc.get("meshes"):
        for i in range(len(doc["meshes"])):
            parts.extend(mesh_parts(i))
    mesh = Mesh(parts)
    mesh.scale(1000.0)  # glTF is in metres
    if z_up is None:
        z_up = "open cascade" in doc.get("asset", {}).get("generator", "").lower()
    if not z_up:
        mesh.rotate_y_up_to_z_up()
    return mesh


# --- VRML 2.0 (.wrl) ----------------------------------------------------------------------

_VRML_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]|[^\s{}\[\],"]+')


class _Node:
    __slots__ = ("type", "fields")

    def __init__(self, type_):
        self.type = type_
        self.fields = {}


def _vrml_parse(text):
    text = re.sub(r'("(?:[^"\\]|\\.)*")|#[^\n]*', lambda m: m.group(1) or "", text)
    tokens = _VRML_TOKEN.findall(text)
    pos = 0
    defs = {}

    def peek(k=0):
        return tokens[pos + k] if pos + k < len(tokens) else None

    def node():
        nonlocal pos
        tok = tokens[pos]
        if tok == "USE":
            pos += 2
            return defs.get(tokens[pos - 1])
        name = None
        if tok == "DEF":
            name = tokens[pos + 1]
            pos += 2
        n = _Node(tokens[pos])
        pos += 1
        if peek() != "{":
            return n
        pos += 1
        while pos < len(tokens) and tokens[pos] != "}":
            field = tokens[pos]
            pos += 1
            if field in ("ROUTE", "PROTO", "EXTERNPROTO"):
                continue
            n.fields[field] = value()
        pos += 1
        if name:
            defs[name] = n
        return n

    def is_node_start():
        tok = peek()
        return tok in ("DEF", "USE") or (tok is not None and peek(1) == "{" and tok[:1].isalpha())

    def value():
        nonlocal pos
        if peek() == "[":
            pos += 1
            items = []
            while pos < len(tokens) and tokens[pos] != "]":
                if is_node_start():
                    items.append(node())
                else:
                    items.append(tokens[pos])
                    pos += 1
            pos += 1
            return items
        if is_node_start():
            return node()
        vals = []
        while pos < len(tokens):
            tok = tokens[pos]
            if tok in ("}", "]") or not (re.match(r"^[-+.0-9]", tok) or tok in ("TRUE", "FALSE", "NULL") or tok.startswith('"')):
                break
            vals.append(tok)
            pos += 1
        return vals

    roots = []
    while pos < len(tokens):
        if is_node_start():
            roots.append(node())
        else:
            pos += 1
    return roots


def _floats(v):
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, str):
            try:
                out.append(float(x))
            except ValueError:
                pass
    return out


def _axis_angle(v):
    x, y, z, a = (v + [0, 0, 1, 0])[:4]
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def read_vrml(data, name="Model"):
    text = data.decode("utf-8", "replace")
    if not text.lstrip().startswith("#VRML V2.0"):
        raise MeshError("Only VRML 2.0 (VRML97) files are supported — the kind KiCad exports.")
    roots = _vrml_parse(text)
    parts = []

    def as_nodes(v):
        if isinstance(v, _Node):
            return [v]
        if isinstance(v, list):
            return [x for x in v if isinstance(x, _Node)]
        return []

    def walk(n, mat, depth=0):
        if n is None or depth > 64:
            return
        f = n.fields
        if n.type == "Transform":
            t = _floats(f.get("translation")) or [0, 0, 0]
            r = _floats(f.get("rotation")) or [0, 0, 1, 0]
            s = _floats(f.get("scale")) or [1, 1, 1]
            c = _floats(f.get("center")) or [0, 0, 0]
            m = np.eye(4)
            m[:3, :3] = _axis_angle(r) @ np.diag(s)
            local = np.eye(4)
            local[:3, 3] = np.array(t) + np.array(c)
            back = np.eye(4)
            back[:3, 3] = -np.array(c)
            mat = mat @ local @ m @ back
        if n.type == "Switch":
            choice = int((_floats(f.get("whichChoice")) or [-1])[0])
            kids = as_nodes(f.get("choice"))
            if 0 <= choice < len(kids):
                walk(kids[choice], mat, depth + 1)
            return
        if n.type == "Shape":
            color = None
            app = f.get("appearance")
            if isinstance(app, _Node):
                matn = app.fields.get("material")
                if isinstance(matn, _Node):
                    d = _floats(matn.fields.get("diffuseColor"))
                    if len(d) >= 3:
                        tr = (_floats(matn.fields.get("transparency")) or [0])[0]
                        color = (d[0], d[1], d[2], 1.0 - tr)
            geo = f.get("geometry")
            if isinstance(geo, _Node) and geo.type in ("IndexedFaceSet", "IndexedTriangleSet"):
                coord = geo.fields.get("coord")
                pts = _floats(coord.fields.get("point")) if isinstance(coord, _Node) else []
                if len(pts) >= 9:
                    v = np.array(pts[: len(pts) // 3 * 3], dtype=np.float64).reshape(-1, 3)
                    if geo.type == "IndexedTriangleSet":
                        idx = [int(x) for x in _floats(geo.fields.get("index"))]
                        tris = [idx[i:i + 3] for i in range(0, len(idx) - 2, 3)]
                    else:
                        tris, face = [], []
                        for x in _floats(geo.fields.get("coordIndex")):
                            i = int(x)
                            if i < 0:
                                tris.extend([face[0], face[k], face[k + 1]] for k in range(1, len(face) - 1))
                                face = []
                            else:
                                face.append(i)
                        tris.extend([face[0], face[k], face[k + 1]] for k in range(1, len(face) - 1))
                    ccw = (f.get("ccw") or geo.fields.get("ccw") or ["TRUE"])
                    t = np.array([tr for tr in tris if max(tr) < len(v)], dtype=np.int64).reshape(-1, 3)
                    if ccw and ccw[0] == "FALSE":
                        t = t[:, [0, 2, 1]]
                    if len(t):
                        parts.append(Part(name, v, t, color).transformed(mat))
            return
        for key in ("children", "choice"):
            for child in as_nodes(f.get(key)):
                walk(child, mat, depth + 1)

    for r in roots:
        walk(r, np.eye(4))
    mesh = Mesh(parts)
    if parts:
        lo, hi = mesh.bbox()
        if max(hi[i] - lo[i] for i in range(3)) < 2.0:  # exported in metres
            mesh.scale(1000.0)
            mesh.notes.append("The file looked like it was in metres; it was scaled to millimetres.")
    return mesh


# --- STEP / IGES ----------------------------------------------------------------------------


def step_available():
    try:
        import cascadio  # noqa: F401
    except ImportError:
        return False
    return True


def read_brep(data, name="Model", kind="step"):
    try:
        import cascadio
    except ImportError:
        raise MeshError("STEP and IGES preview needs the OpenCASCADE converter, which isn't installed on this server.")
    ftype = cascadio.FileType.IGES if kind == "iges" else cascadio.FileType.STEP
    # Tolerances in the file's units (mm): fine enough for connectors, coarse enough for big enclosures.
    glb = cascadio.to_glb_bytes(data, ftype, tol_linear=0.02, tol_angular=0.35, tol_relative=False,
                                merge_primitives=True, use_parallel=True)
    if not glb:
        raise MeshError(f"The {kind.upper()} file couldn't be read. Check that it opens in your CAD program.")
    mesh = read_gltf(glb, name, z_up=True)
    # The converter writes metres for STEP but sometimes keeps the file's own units (IGES):
    # anything over 5 m across was already in millimetres.
    if mesh.parts:
        lo, hi = mesh.bbox()
        if max(hi[i] - lo[i] for i in range(3)) > 5000:
            mesh.scale(0.001)
    return mesh


# --- Dispatch ---------------------------------------------------------------------------------

VIEWABLE = {
    "stl": "STL", "obj": "OBJ", "3mf": "3MF", "glb": "glTF (binary)", "gltf": "glTF",
    "wrl": "VRML", "vrml": "VRML", "step": "STEP", "stp": "STEP", "iges": "IGES", "igs": "IGES",
}
# CAD formats that are stored and versioned but can't be drawn without the original program.
NATIVE = {
    "sldprt": "SolidWorks part", "sldasm": "SolidWorks assembly", "slddrw": "SolidWorks drawing",
    "f3d": "Fusion 360 design", "f3z": "Fusion 360 archive", "ipt": "Inventor part", "iam": "Inventor assembly",
    "catpart": "CATIA part", "catproduct": "CATIA product", "prt": "Creo / NX part", "asm": "Creo assembly",
    "x_t": "Parasolid", "x_b": "Parasolid", "3dm": "Rhino", "skp": "SketchUp", "fcstd": "FreeCAD",
    "scad": "OpenSCAD", "dwg": "AutoCAD drawing", "dxf": "DXF drawing", "blend": "Blender",
}


def ext_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def can_view(filename):
    e = ext_of(filename)
    if e in ("step", "stp", "iges", "igs"):
        return step_available()
    return e in VIEWABLE


def convert(data, filename):
    """Read a 3D file into a finished Mesh, or raise MeshError with a helpful message."""
    e = ext_of(filename)
    label = filename.rsplit(".", 1)[0]
    if e in NATIVE:
        raise MeshError(f"{NATIVE[e]} files are stored and versioned, but they can only be viewed in their own program. "
                        "Also upload a STEP or 3MF export to see it here.")
    try:
        if e == "stl":
            mesh = read_stl(data, label)
        elif e == "obj":
            mesh = read_obj(data, label)
        elif e == "3mf":
            mesh = read_3mf(data, label)
        elif e in ("glb", "gltf"):
            mesh = read_gltf(data, label)
        elif e in ("wrl", "vrml"):
            mesh = read_vrml(data, label)
        elif e in ("step", "stp"):
            mesh = read_brep(data, label, "step")
        elif e in ("iges", "igs"):
            mesh = read_brep(data, label, "iges")
        else:
            raise MeshError(f".{e} files can't be shown in 3D.")
    except MeshError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, struct.error, ET.ParseError, zipfile.BadZipFile) as exc:
        raise MeshError(f"The file couldn't be read ({exc.__class__.__name__}). It may be damaged or in an unusual variant.")
    return mesh.finish()
