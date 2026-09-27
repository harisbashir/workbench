import io
import math
import zipfile

from django.test import SimpleTestCase

from apps.design import demo_board
from apps.design.board import collect, identify, render_board
from apps.design.gerber import board_shape, evaluate, parse_excellon, parse_gerber

HEAD = "%FSLAX46Y46*%\n%MOMM*%\n"


def g(body, head=HEAD):
    return parse_gerber(head + body + "\nM02*\n", "t.gbr", prefix="t")


class ExpressionTests(SimpleTestCase):
    def test_macro_arithmetic(self):
        v = {1: 0.25, 2: 2.0}
        self.assertAlmostEqual(evaluate("$1+$1", v), 0.5)
        self.assertAlmostEqual(evaluate("$2x3-1", v), 5.0)
        self.assertAlmostEqual(evaluate("($2+2)/4", v), 1.0)
        self.assertAlmostEqual(evaluate("-$1", v), -0.25)
        with self.assertRaises(ValueError):
            evaluate("__import__('os')", v)


class GerberTests(SimpleTestCase):
    def test_units_format_and_flashes(self):
        L = g("%ADD10C,1.0*%\nD10*\nX1000000Y2000000D03*\nX3000000Y2000000D03*\n")
        self.assertEqual(L.stats["flashes"], 2)
        self.assertAlmostEqual(L.bbox.minx, 0.5)
        self.assertAlmostEqual(L.bbox.maxx, 3.5)
        inch = parse_gerber("%FSLAX24Y24*%\n%MOIN*%\n%ADD10C,0.1*%\nD10*\nX10000Y0D03*\nM02*\n", "i.gbr")
        self.assertAlmostEqual(inch.bbox.maxx, 25.4 + 1.27, places=4)

    def test_strokes_track_width_and_regions(self):
        L = g("%ADD10C,0.25*%\n%ADD11C,0.5*%\nD10*\nX0Y0D02*\nX5000000Y0D01*\nD11*\nX0Y1000000D02*\nX5000000Y1000000D01*\n"
              "G36*\nX0Y3000000D02*\nX2000000Y3000000D01*\nX2000000Y5000000D01*\nX0Y3000000D01*\nG37*\n")
        self.assertAlmostEqual(L.stats["min_trace"], 0.25)
        self.assertEqual(L.stats["regions"], 1)
        svg = "".join("".join(e) for _d, e in L.segments)
        self.assertIn('stroke-width="0.25"', svg)
        self.assertIn('stroke-width="0.5"', svg)

    def test_full_circle_arc_bbox(self):
        L = g("%ADD10C,0.1*%\nD10*\nG75*\nX1000000Y0D02*\nG03*\nX1000000Y0I-1000000J0D01*\n")
        self.assertAlmostEqual(L.bbox.minx, -1.05, places=3)
        self.assertAlmostEqual(L.bbox.maxy, 1.05, places=3)

    def test_clear_polarity_makes_a_new_segment(self):
        L = g("%ADD10C,2*%\n%ADD11C,1*%\nD10*\nX0Y0D03*\n%LPC*%\nD11*\nX0Y0D03*\n%LPD*%\nX5000000Y0D03*\n")
        self.assertEqual([dark for dark, _ in L.segments], [True, False, True])

    def test_roundrect_macro_rect_hole_and_thermal(self):
        L = g("%AMRoundRect*\n0 comment*\n4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*\n1,1,$1+$1,$2,$3*\n20,1,$1+$1,$2,$3,$4,$5,0*%\n"
              "%ADD10RoundRect,0.1X-0.4X-0.3X0.4X-0.3X0.4X0.3X-0.4X0.3X0*%\n%ADD11C,1.0X0.4X0.2*%\n"
              "%AMTH*\n7,0,0,2.0,1.4,0.3,0*%\n%ADD12TH*%\nD10*\nX0Y0D03*\nD11*\nX5000000Y0D03*\nD12*\nX9000000Y0D03*\n")
        defs = "".join(L.defs)
        self.assertIn('id="ta10"', defs)
        self.assertIn("<circle", defs)
        self.assertIn('fill-rule="evenodd"', defs)   # rectangular hole in the round pad
        self.assertEqual(L.stats["flashes"], 3)
        self.assertFalse(L.warnings)

    def test_x2_file_function(self):
        L = g("%TF.FileFunction,Copper,L2,Bot*%\n%ADD10C,1*%\nD10*\nX0Y0D03*\n")
        self.assertEqual(identify("anything.gbr", L.function), ("copper", "bottom", 2))


class DrillTests(SimpleTestCase):
    def test_metric_decimal_with_route_slot(self):
        text = ("M48\n; #@! TF.FileFunction,Plated,1,2,PTH\nMETRIC\nT1C0.300\nT2C1.000\n%\nG90\nG05\nT1\nX1.0Y2.0\nX3.5Y2.0\n"
                "T2\nG00X10.0Y10.0\nM15\nG01X12.0Y10.0\nM16\nG05\nX20Y20\nM30\n")
        L = parse_excellon(text, "board-PTH.drl")
        self.assertEqual((L.stats["holes"], L.stats["slots"]), (3, 1))
        self.assertEqual(L.stats["tools"], {0.3: 2, 1.0: 1})
        self.assertTrue(L.stats["plated"])

    def test_inch_trailing_zero_format_and_npth(self):
        text = "M48\nINCH,TZ\nT01C0.0240\n%\nT01\nX16910Y10810\nM30\n"
        L = parse_excellon(text, "board-NPTH.drl")
        self.assertAlmostEqual(L.bbox.minx + 0.3048, 1.691 * 25.4, places=3)
        self.assertFalse(L.stats["plated"])

    def test_leading_zero_format(self):
        L = parse_excellon("M48\nMETRIC,LZ,000.000\nT1C0.8\n%\nT1\nX0105Y02\nM30\n", "x.drl")
        self.assertAlmostEqual(L.bbox.minx + 0.4, 10.5, places=3)
        self.assertAlmostEqual(L.bbox.miny + 0.4, 20.0, places=3)

    def test_g85_slot(self):
        L = parse_excellon("M48\nMETRIC\nT1C0.6\n%\nT1\nX1.0Y1.0G85X3.0Y1.0\nM30\n", "x.drl")
        self.assertEqual(L.stats["slots"], 1)


class LayerNameTests(SimpleTestCase):
    def test_common_names(self):
        cases = {
            "board-F_Cu.gbr": ("copper", "top"), "board-B_Mask.gbs": ("mask", "bottom"), "board-In1_Cu.gbr": ("copper", "inner"),
            "board-F_Silkscreen.gto": ("silk", "top"), "board-Edge_Cuts.gm1": ("outline", None), "x.GTL": ("copper", "top"),
            "x.cmp": ("copper", "top"), "x.sol": ("copper", "bottom"), "x.plc": ("silk", "top"), "x.gko": ("outline", None),
            "Gerber_BottomLayer.GBL": ("copper", "bottom"), "x.G2": ("copper", "inner"), "board-F_Fab.gbr": ("other", None),
        }
        for name, want in cases.items():
            self.assertEqual(identify(name)[:2], want, name)


class OutlineTests(SimpleTestCase):
    def outline(self, body):
        return parse_gerber(HEAD + "%ADD10C,0.1*%\nD10*\n" + body + "M02*\n", "o.gbr")

    def test_rounded_rectangle_is_one_loop(self):
        L = parse_gerber(demo_board.build_files()["pwr-board-Edge_Cuts.gm1"].decode(), "e.gm1")
        d = board_shape(L)
        self.assertEqual(d.count("M"), 1)
        self.assertEqual(d.count("A"), 4)

    def test_gaps_are_bridged_and_duplicates_dropped(self):
        body = ("X0Y0D02*\nX10000000Y0D01*\n"             # bottom
                "X10000000Y100000D02*\nX10000000Y10000000D01*\n"  # right, 0.1 mm gap
                "X10000000Y10000000D02*\nX0Y10000000D01*\nX0Y0D01*\n"
                "X0Y0D02*\nX10000000Y0D01*\nX10000000Y10000000D01*\nX0Y10000000D01*\nX0Y0D01*\n")  # the same outline again
        d = board_shape(self.outline(body))
        self.assertEqual(d.count("M"), 1)

    def test_open_outline_falls_back_to_rectangle(self):
        d = board_shape(self.outline("X0Y0D02*\nX10000000Y0D01*\nX0Y20000000D02*\nX1000000Y20000000D01*\n"))
        self.assertIn("H", d)


class BoardTests(SimpleTestCase):
    def test_demo_board(self):
        r = render_board([("pwr-board-gerbers.zip", demo_board.gerber_zip())])
        i = r["info"]
        self.assertEqual((i["width"], i["height"], i["copper_layers"]), (50.0, 32.0, 2))
        self.assertEqual((i["plated"], i["nonplated"], i["slots"]), (18, 4, 4))
        self.assertEqual(i["thickness"], 1.6)
        self.assertFalse(r["warnings"], r["warnings"])
        labels = {l["label"] for l in r["layers"]}
        self.assertTrue({"Top copper", "Bottom copper", "Board outline", "Top solder mask", "Drill holes (non-plated)"} <= labels)
        for view in ("top", "bottom", "layers"):
            self.assertIn(f'data-view="{view}"', r["svg"])
        self.assertNotIn("<script", r["svg"])
        self.assertNotIn("pwr-board", r["svg"], "file names never end up in the SVG")
        self.assertTrue(r["thumb_top"].startswith("<svg"))

    def test_hostile_names_and_junk_files(self):
        buf = io.BytesIO()
        files = demo_board.build_files()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr('<script>alert(1)</script>-F_Cu.gbr', files["pwr-board-F_Cu.gtl"])
            z.writestr("readme.txt", "hello")
            z.writestr("__MACOSX/._x.gbr", "junk")
        r = render_board([("x.zip", buf.getvalue())])
        self.assertNotIn("<script", r["svg"])
        self.assertEqual(r["unused"], ["readme.txt"])
        self.assertIn("No board outline", " ".join(r["warnings"]))

    def test_nothing_usable(self):
        with self.assertRaises(ValueError):
            render_board([("notes.txt", b"just text")])

    def test_zip_limits(self):
        from apps.design import board
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for k in range(board.MAX_FILES + 1):
                z.writestr(f"f{k}.gbr", "x")
        with self.assertRaises(ValueError):
            collect([("many.zip", buf.getvalue())])

    def test_pos_file_matches_parts(self):
        from apps.production.fab import parse_placements
        rows, warnings = parse_placements(demo_board.pos_csv())
        self.assertEqual(len(rows), len(demo_board.PARTS))
        j2 = next(r for r in rows if r["ref"] == "J2")
        self.assertTrue(math.isclose(j2["x"], 46.0))
