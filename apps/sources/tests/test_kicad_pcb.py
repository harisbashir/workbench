from pathlib import Path

from django.test import SimpleTestCase

from apps.sources.kicad import pcb
from apps.sources.kicad.sexpr import KicadError

FIX = Path(__file__).parent / "fixtures" / "kicad"


def text(name):
    return (FIX / name).read_text()


class LoadTests(SimpleTestCase):
    def test_loads_versions(self):
        b8 = pcb.load_pcb(text("v8_board.kicad_pcb"))
        self.assertEqual({f.ref: f.value for f in b8.footprints}, {"R1": "10k", "R7": "4.7k", "R9": "0R", "H1": "MountingHole"})
        self.assertEqual(b8.thickness, 1.6)
        self.assertEqual(b8.nets[1], "GND")
        self.assertEqual(len(b8.tracks), 2)
        self.assertEqual(len(b8.arcs), 1)
        self.assertEqual(len(b8.vias), 1)
        self.assertEqual(next(f for f in b8.footprints if f.ref == "R9").side, "bottom")
        b6 = pcb.load_pcb(text("v6_board.kicad_pcb"))
        self.assertEqual([(f.ref, f.value) for f in b6.footprints], [("R2", "1k")])
        b9 = pcb.load_pcb(text("v9_board.kicad_pcb"))
        self.assertEqual([(f.ref, f.value, f.angle) for f in b9.footprints], [("C5", "100n", 270.0)])

    def test_footprint_transform(self):
        b8 = pcb.load_pcb(text("v8_board.kicad_pcb"))
        r7 = next(f for f in b8.footprints if f.ref == "R7")
        x, y = r7.to_board(-0.825, 0)
        self.assertAlmostEqual(x, 10)
        self.assertAlmostEqual(y, 15.825)
        r1 = next(f for f in b8.footprints if f.ref == "R1")
        self.assertEqual(r1.to_board(0.825, 0), (10.825, 5))

    def test_legacy_refused(self):
        with self.assertRaisesMessage(KicadError, "isn't supported for previews"):
            pcb.load_pcb("(kicad_pcb (version 20171130) (host pcbnew 5.1.6))")

    def test_parts(self):
        parts = pcb.load_pcb(text("v8_board.kicad_pcb")).parts()
        self.assertEqual([p["ref"] for p in parts], ["H1", "R1", "R7", "R9"])
        self.assertEqual(next(p for p in parts if p["ref"] == "R9")["side"], "bottom")


class RenderTests(SimpleTestCase):
    def test_render_has_views_and_layers(self):
        r = pcb.render_pcb(text("v8_board.kicad_pcb"))
        self.assertIn('data-view="top"', r["svg"])
        self.assertIn('data-view="bottom"', r["svg"])
        self.assertIn('data-view="layers"', r["svg"])
        labels = [layer["label"] for layer in r["layers"]]
        for want in ("Top copper", "Bottom copper", "Board outline", "Top silkscreen", "Drill holes"):
            self.assertIn(want, labels)
        self.assertEqual(r["info"]["copper_layers"], 2)
        self.assertEqual(r["info"]["holes"], 3)        # mounting hole, its slot and the via
        self.assertEqual(r["info"]["slots"], 1)
        self.assertEqual(r["info"]["min_trace"], 0.25)
        self.assertNotIn("<B>", r["svg"])
        self.assertIn("&lt;B&gt; &amp;", r["svg"])

    def test_board_size_from_edge_cuts(self):
        r = pcb.render_pcb(text("v8_board.kicad_pcb"))
        self.assertEqual((r["info"]["width"], r["info"]["height"]), (40, 30))
        self.assertTrue(r["info"]["has_outline"])
        r6 = pcb.render_pcb(text("v6_board.kicad_pcb"))
        self.assertEqual((r6["info"]["width"], r6["info"]["height"]), (20, 10))
        r9 = pcb.render_pcb(text("v9_board.kicad_pcb"))
        self.assertEqual((r9["info"]["width"], r9["info"]["height"]), (15, 8))

    def test_no_outline_still_draws(self):
        src = text("v6_board.kicad_pcb").replace('"Edge.Cuts"', '"Dwgs.User"')
        r = pcb.render_pcb(src)
        self.assertIn("<svg", r["svg"])
        self.assertTrue(any("outline" in w for w in r["warnings"]))

    def test_highlight_boxes(self):
        b = pcb.load_pcb(text("v8_board.kicad_pcb"))
        uid = next(f.uuid for f in b.footprints if f.ref == "R7")
        r = pcb.render_pcb(text("v8_board.kicad_pcb"), highlight={uid: "changed"})
        self.assertIn('class="hl-changed"', r["svg"])
