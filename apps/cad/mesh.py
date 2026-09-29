"""A simple triangle mesh made of coloured parts, and the compact file format the 3D viewer loads.

Everything is converted to millimetres with Z pointing up.

File format ("WBM1", stored gzip-compressed):
    4 bytes   magic b"WBM1"
    4 bytes   little-endian length of the JSON header
    n bytes   JSON header, padded with spaces to a multiple of 4:
              {"parts": [{"name", "color": [r, g, b, a], "vertices", "triangles",
                          "vertex_offset", "index_offset"}], "bbox": [[x, y, z], [x, y, z]], "triangles": n}
    …         all vertex positions as float32 x, y, z (part after part)
    …         all triangle indices as uint32 (part after part, each part's indices start at 0)
"""
import gzip
import json
import struct

import numpy as np

MAX_TRIANGLES = 4_000_000

# Colours for parts that don't carry one (enclosures, plain STL…)
PALETTE = [
    (0.72, 0.75, 0.80, 1.0), (0.36, 0.55, 0.78, 1.0), (0.85, 0.62, 0.32, 1.0), (0.47, 0.70, 0.49, 1.0),
    (0.78, 0.45, 0.45, 1.0), (0.62, 0.52, 0.78, 1.0), (0.80, 0.78, 0.45, 1.0), (0.45, 0.72, 0.75, 1.0),
]


class MeshError(ValueError):
    """The file couldn't be read as a 3D model. The message is shown to the user."""


class Part:
    __slots__ = ("name", "color", "vertices", "triangles")

    def __init__(self, name, vertices, triangles, color=None):
        self.name = (name or "Part")[:120]
        self.vertices = np.ascontiguousarray(vertices, dtype=np.float32).reshape(-1, 3)
        self.triangles = np.ascontiguousarray(triangles, dtype=np.uint32).reshape(-1, 3)
        self.color = tuple(float(c) for c in color) if color is not None else None

    def transformed(self, matrix):
        """A copy with the 4x4 matrix applied to the vertices."""
        m = np.asarray(matrix, dtype=np.float64)
        v = self.vertices.astype(np.float64) @ m[:3, :3].T + m[:3, 3]
        tris = self.triangles
        if np.linalg.det(m[:3, :3]) < 0:  # mirrored: keep faces pointing outwards
            tris = tris[:, [0, 2, 1]]
        return Part(self.name, v, tris, self.color)


class Mesh:
    def __init__(self, parts=None):
        self.parts = [p for p in (parts or []) if len(p.triangles)]
        self.notes = []

    @property
    def triangle_count(self):
        return int(sum(len(p.triangles) for p in self.parts))

    def bbox(self):
        if not self.parts:
            return [[0, 0, 0], [0, 0, 0]]
        lo = np.min([p.vertices.min(axis=0) for p in self.parts], axis=0)
        hi = np.max([p.vertices.max(axis=0) for p in self.parts], axis=0)
        return [[round(float(x), 4) for x in lo], [round(float(x), 4) for x in hi]]

    def scale(self, factor):
        for p in self.parts:
            p.vertices = (p.vertices * factor).astype(np.float32)

    def rotate_y_up_to_z_up(self):
        """glTF and many exporters use Y up; the viewer uses Z up like CAD tools."""
        m = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
        self.parts = [p.transformed(m) for p in self.parts]

    def merge_by_color(self, max_parts=400):
        """Assemblies with thousands of parts (KiCad boards) are merged by colour so they draw quickly."""
        if len(self.parts) <= max_parts:
            return
        groups = {}
        for p in self.parts:
            groups.setdefault(p.color, []).append(p)
        merged = []
        for color, parts in groups.items():
            offset, verts, tris = 0, [], []
            for p in parts:
                verts.append(p.vertices)
                tris.append(p.triangles + offset)
                offset += len(p.vertices)
            name = parts[0].name if len(parts) == 1 else f"{len(parts)} parts"
            merged.append(Part(name, np.concatenate(verts), np.concatenate(tris), color))
        self.parts = merged
        self.notes.append("Parts with the same colour were merged so the model draws quickly.")

    def finish(self):
        """Check limits and give uncoloured parts a colour."""
        if not self.parts:
            raise MeshError("The file doesn't contain any surfaces to show.")
        n = self.triangle_count
        if n > MAX_TRIANGLES:
            raise MeshError(f"The model has {n:,} triangles, more than the {MAX_TRIANGLES:,} the viewer can show. "
                            "Export it with a coarser resolution.")
        for i, p in enumerate(self.parts):
            if p.color is None:
                p.color = PALETTE[i % len(PALETTE)] if len(self.parts) > 1 else PALETTE[0]
            bad = p.triangles.max(initial=0) >= len(p.vertices) if len(p.triangles) else False
            if bad:
                raise MeshError(f"Part “{p.name}” refers to vertices that don't exist; the file may be damaged.")
        self.merge_by_color()
        return self

    def to_bytes(self):
        parts, voff, ioff = [], 0, 0
        for p in self.parts:
            parts.append({"name": p.name, "color": [round(c, 4) for c in p.color], "vertices": len(p.vertices),
                          "triangles": len(p.triangles), "vertex_offset": voff, "index_offset": ioff})
            voff += len(p.vertices)
            ioff += len(p.triangles)
        header = json.dumps({"parts": parts, "bbox": self.bbox(), "triangles": ioff, "notes": self.notes}).encode()
        header += b" " * (-len(header) % 4)
        body = b"".join(p.vertices.astype("<f4").tobytes() for p in self.parts)
        body += b"".join(p.triangles.astype("<u4").tobytes() for p in self.parts)
        return gzip.compress(b"WBM1" + struct.pack("<I", len(header)) + header + body, compresslevel=6)

    def info(self):
        lo, hi = self.bbox()
        return {"triangles": self.triangle_count, "parts": len(self.parts),
                "size": [round(hi[i] - lo[i], 2) for i in range(3)], "notes": self.notes}


def read_header(data):
    """Parse a stored mesh file's header (used by tests and the export)."""
    raw = gzip.decompress(data)
    if raw[:4] != b"WBM1":
        raise MeshError("Not a Workbench mesh")
    n = struct.unpack("<I", raw[4:8])[0]
    return json.loads(raw[8:8 + n])


def weld(vertices, tolerance=1e-5):
    """Turn a triangle soup (3 vertices per triangle) into indexed vertices."""
    v = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
    if len(v) == 0:
        return v, np.zeros((0, 3), dtype=np.uint32)
    key = np.round(v / tolerance).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return v[first], inverse.reshape(-1, 3).astype(np.uint32)
