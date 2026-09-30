"""Hostile and damaged 3D files are refused quickly, without using much memory."""
import base64
import io
import json
import struct
import time
import zipfile

import numpy as np
from django.test import SimpleTestCase

from apps.cad import formats, samples
from apps.cad.mesh import Mesh, MeshError, Part

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def _3mf(model_xml, compress=zipfile.ZIP_DEFLATED):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", compress) as z:
        z.writestr("3D/3dmodel.model", model_xml)
    return b.getvalue()


def _exploding_3mf(depth, fan):
    objs = ['<object id="0" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
            '<vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>']
    for i in range(1, depth + 1):
        objs.append(f'<object id="{i}" type="model"><components>' + f'<component objectid="{i - 1}"/>' * fan + "</components></object>")
    return _3mf(f'<model xmlns="{NS}" unit="millimeter"><resources>{"".join(objs)}</resources>'
                f'<build><item objectid="{depth}"/></build></model>')


def _gltf(nodes):
    pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    doc = {"asset": {"version": "2.0"}, "buffers": [{"uri": "data:;base64," + base64.b64encode(pos).decode(), "byteLength": 36}],
           "bufferViews": [{"buffer": 0, "byteLength": 36}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}], "nodes": nodes, "scenes": [{"nodes": [0]}]}
    return json.dumps(doc).encode()


class HostileFileTests(SimpleTestCase):
    def assertRefusedQuickly(self, data, name, pattern=None, seconds=5):
        t = time.monotonic()
        with self.assertRaises(MeshError) as cm:
            formats.convert(data, name)
        self.assertLess(time.monotonic() - t, seconds, name)
        if pattern:
            self.assertRegex(str(cm.exception), pattern)

    def test_3mf_components_expanding_exponentially(self):
        self.assertRefusedQuickly(_exploding_3mf(16, 10), "bomb.3mf", "places parts more than")

    def test_3mf_moderate_components_still_work(self):
        m = formats.convert(_exploding_3mf(3, 4), "ok.3mf")
        self.assertEqual(m.triangle_count, 64)

    def test_3mf_zip_bomb(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            with z.open("3D/3dmodel.model", "w") as f:
                f.write(f'<model xmlns="{NS}"><resources>'.encode())
                for _ in range(40):
                    f.write(b" " * (1 << 20))
                f.write(b"</resources></model>")
        self.assertLess(len(b.getvalue()), 200_000)
        self.assertRefusedQuickly(b.getvalue(), "bomb.3mf", "unusually large")

    def test_3mf_entity_expansion_and_dtd_are_refused(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
               f'<model xmlns="{NS}"><resources><object id="1" name="&b;"/></resources></model>')
        self.assertRefusedQuickly(_3mf(xml), "dtd.3mf", "DTD")

    def test_3mf_triangle_budget(self):
        tris = '<triangle v1="0" v2="1" v3="2"/>' * 50
        xml = (f'<model xmlns="{NS}"><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
               f'<vertex x="0" y="1" z="0"/></vertices><triangles>{tris}</triangles></mesh></object></resources>'
               '<build><item objectid="1"/></build></model>')
        budget = formats.Budget(max_triangles=10)
        with self.assertRaisesRegex(MeshError, "more than 10 triangles"):
            formats.read_3mf(_3mf(xml), budget=budget)

    def test_gltf_node_loop(self):
        self.assertRefusedQuickly(_gltf([{"mesh": 0, "children": [0, 0]}]), "loop.gltf", "loop")

    def test_gltf_exponential_tree(self):
        nodes = [{"mesh": 0, "children": [i + 1] * 4} for i in range(30)] + [{"mesh": 0}]
        self.assertRefusedQuickly(_gltf(nodes), "tree.gltf", "places parts more than")

    def test_gltf_huge_accessor_count(self):
        doc = json.loads(_gltf([{"mesh": 0}]))
        doc["accessors"] = [{"componentType": 5126, "count": 10 ** 10, "type": "VEC3"}]
        self.assertRefusedQuickly(json.dumps(doc).encode(), "huge.gltf", "accessor")
        doc["accessors"] = [{"bufferView": 0, "componentType": 5126, "count": 1000, "type": "VEC3"}]
        self.assertRefusedQuickly(json.dumps(doc).encode(), "short.gltf", "outside its buffers")

    def test_gltf_strip_and_fan(self):
        doc = json.loads(_gltf([{"mesh": 0}]))
        for mode in (5, 6):
            doc["meshes"][0]["primitives"][0]["mode"] = mode
            self.assertEqual(formats.convert(json.dumps(doc).encode(), "s.gltf").triangle_count, 1)

    def test_vrml_use_doubling(self):
        s = ("#VRML V2.0 utf8\nDEF L0 Shape { geometry IndexedFaceSet { coord Coordinate { point [0 0 0, 1 0 0, 0 1 0] } "
             "coordIndex [0 1 2 -1] } }\n")
        for i in range(1, 41):
            s += f"DEF L{i} Group {{ children [ USE L{i - 1} USE L{i - 1} ] }}\n"
        self.assertRefusedQuickly(s.encode(), "bomb.wrl", "places parts more than")

    def test_vrml_deep_nesting(self):
        self.assertRefusedQuickly(("#VRML V2.0 utf8\n" + "Group { children [ " * 5000).encode(), "deep.wrl")

    def test_stl_with_nan_or_huge_vertices(self):
        v = samples.CUBE_V * 10
        for bad in (np.nan, np.inf, 1e30):
            vv = v.copy()
            vv[0, 0] = bad
            self.assertRefusedQuickly(samples.stl_binary(vv, samples.CUBE_T), "nan.stl", "invalid coordinates")

    def test_nan_from_a_transform_is_refused(self):
        m = Mesh([Part("p", [[0, 0, 0], [1, 0, 0], [np.nan, 1, 0]], [[0, 1, 2]])])
        with self.assertRaisesRegex(MeshError, "invalid coordinates"):
            m.finish()

    def test_binary_stl_whose_header_says_solid(self):
        data = bytearray(samples.stl_binary())
        data[:80] = b"solid exported by a CAD program".ljust(80, b" ")
        data += b"\0" * 7  # some exporters pad the end, so the size doesn't match exactly
        m = formats.convert(bytes(data), "cube.stl")
        self.assertEqual(m.triangle_count, 12)

    def test_stl_claiming_too_many_triangles(self):
        data = samples.stl_binary()
        data = data[:80] + struct.pack("<I", 5_000_000) + data[84:]
        self.assertRefusedQuickly(data, "big.stl")
