from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.cad import formats, samples
from apps.cad.mesh import MeshError, read_header

FIXTURES = Path(__file__).parent / "fixtures"


def size(mesh):
    return mesh.info()["size"]


class FormatTests(SimpleTestCase):
    def test_stl_binary_is_welded(self):
        m = formats.convert(samples.stl_binary(), "cube.stl")
        self.assertEqual(m.triangle_count, 12)
        self.assertEqual(len(m.parts[0].vertices), 8, "shared corners are merged")
        self.assertEqual(size(m), [10, 10, 10])

    def test_stl_ascii(self):
        m = formats.convert(samples.stl_ascii(), "bracket.stl")
        self.assertEqual((m.triangle_count, m.parts[0].name, size(m)), (12, "bracket", [5, 5, 5]))

    def test_obj_quads_are_triangulated(self):
        m = formats.convert(samples.obj(), "lid.obj")
        self.assertEqual(m.triangle_count, 12)
        self.assertEqual(m.parts[0].name, "lid")

    def test_3mf_colours_and_transforms(self):
        m = formats.convert(samples.three_mf(), "enclosure.3mf")
        self.assertEqual([p.name for p in m.parts], ["Base", "Lid"])
        self.assertEqual(size(m), [80, 50, 33], "the lid sits 30 mm up")
        self.assertAlmostEqual(m.parts[0].color[0], 0x2F / 255, places=3)

    def test_glb_y_up_metres(self):
        v, t = samples.box(0, 0, 0, 40, 20, 10)
        m = formats.convert(samples.glb([("Case", v, t, (1, 0, 0, 1))]), "case.glb")
        self.assertEqual(size(m), [40, 20, 10], "metres → mm and Y-up → Z-up")
        self.assertEqual(m.parts[0].color, (1.0, 0.0, 0.0, 1.0))

    def test_glb_from_opencascade_is_z_up(self):
        v, t = samples.box(0, 0, 0, 40, 20, 10)
        m = formats.convert(samples.glb([("Case", v, t, (1, 1, 1, 1))], generator="Open CASCADE Technology", y_up=False), "b.glb")
        self.assertEqual(size(m), [40, 20, 10])

    def test_vrml_like_kicad(self):
        m = formats.convert(samples.vrml_board(), "board.wrl")
        self.assertEqual(len(m.parts), 4, "board, two components and a USE'd material")
        self.assertEqual(size(m)[:2], [50, 32])
        self.assertAlmostEqual(size(m)[2], 7.6, places=3)  # 1 mm below the board to 5 mm above it
        self.assertAlmostEqual(m.parts[0].color[1], 0.3, places=3)
        self.assertEqual(m.parts[3].color, m.parts[0].color, "USE PCB_GREEN reuses the material")

    @skipUnless(formats.step_available(), "cascadio not installed")
    def test_step_assembly_keeps_colours(self):
        m = formats.convert((FIXTURES / "board_assembly.step").read_bytes(), "board.step")
        self.assertEqual(size(m)[:2], [50, 32])
        colours = {tuple(round(c, 2) for c in p.color[:3]) for p in m.parts}
        self.assertIn((0.07, 0.35, 0.15), colours)
        self.assertIn((0.1, 0.1, 0.1), colours)
        self.assertEqual(len(m.parts), 4, "board, two ICs and a capacitor")

    @skipUnless(formats.step_available(), "cascadio not installed")
    def test_iges(self):
        m = formats.convert((FIXTURES / "block.igs").read_bytes(), "block.igs")
        self.assertEqual(size(m), [20, 10, 5])

    def test_native_formats_explain(self):
        with self.assertRaisesRegex(MeshError, "SolidWorks part"):
            formats.convert(b"xx", "case.SLDPRT")
        with self.assertRaisesRegex(MeshError, "Fusion 360"):
            formats.convert(b"xx", "case.f3d")

    def test_damaged_files(self):
        for name, data in (("a.stl", b"x" * 90), ("a.3mf", b"not a zip"), ("a.glb", b"glTF" + b"\0" * 20),
                           ("a.wrl", b"#VRML V1.0 ascii"), ("a.obj", b"# nothing")):
            with self.assertRaises(MeshError, msg=name):
                formats.convert(data, name)

    def test_binary_roundtrip(self):
        m = formats.convert(samples.three_mf(), "enclosure.3mf")
        h = read_header(m.to_bytes())
        self.assertEqual(h["triangles"], 24)
        self.assertEqual([p["name"] for p in h["parts"]], ["Base", "Lid"])
