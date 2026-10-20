from pathlib import Path

from django.test import SimpleTestCase

from apps.sources.kicad import schematic
from apps.sources.kicad.sexpr import KicadError

FIX = Path(__file__).parent / "fixtures" / "kicad"


def load(name):
    return schematic.load_schematic((FIX / name).read_text())


class LoadTests(SimpleTestCase):
    def test_loads_v6_v8_v9(self):
        v8 = load("v8_hier.kicad_sch")
        parts = {s.ref: (s.value, s.footprint) for s in v8.symbols}
        self.assertEqual(parts["R1"], ("10k", "Resistor_SMD:R_0603_1608Metric"))
        self.assertEqual(parts["C1"], ("22u", "Capacitor_SMD:C_0805_2012Metric"))
        self.assertEqual(v8.title, "PWR power board")
        self.assertEqual([(s.name, s.file) for s in v8.sheets], [("Buck", "buck.kicad_sch")])
        self.assertEqual(v8.sheets[0].pins[0][0], "EN")
        self.assertIn("VBUS", v8.nets())
        self.assertIn("GND", v8.nets())
        self.assertTrue(next(s for s in v8.symbols if s.ref == "#PWR01").power)
        v6 = load("v6_simple.kicad_sch")
        self.assertEqual([(s.ref, s.value) for s in v6.symbols], [("R2", "1k")])
        self.assertEqual([(s.name, s.file) for s in v6.sheets], [("Sensors", "sensors.kicad_sch")])
        v9 = load("v9_simple.kicad_sch")
        self.assertEqual([(s.ref, s.dnp) for s in v9.symbols], [("R9", True)])
        self.assertEqual(v9.paper, (297.0, 210.0))

    def test_parts_list_excludes_power(self):
        refs = [p["ref"] for p in load("v8_hier.kicad_sch").parts()]
        self.assertEqual(refs, ["C1", "R1", "R7"])

    def test_legacy_refused(self):
        with self.assertRaisesMessage(KicadError, "isn't supported for previews"):
            schematic.load_schematic("EESchema Schematic File Version 4\nEELAYER 30 0\n")
        with self.assertRaisesMessage(KicadError, "isn't supported for previews"):
            schematic.load_schematic("(kicad_sch (version 20200310))")

    def test_not_a_schematic(self):
        with self.assertRaises(KicadError):
            schematic.load_schematic("(kicad_pcb (version 20240108))")


class TransformTests(SimpleTestCase):
    def test_symbol_transform(self):
        pin1 = (0, 3.81)  # top end of a resistor in library coordinates (Y up)
        cases = {(0, None): (100, 46.19), (90, None): (96.19, 50), (180, None): (100, 53.81), (270, None): (103.81, 50),
                 (0, "x"): (100, 53.81), (0, "y"): (100, 46.19)}
        for (angle, mirror), want in cases.items():
            x, y = schematic.place(pin1, 100, 50, angle, mirror)
            self.assertAlmostEqual(x, want[0], places=3, msg=(angle, mirror))
            self.assertAlmostEqual(y, want[1], places=3, msg=(angle, mirror))
        x, y = schematic.place((2, 0), 100, 50, 0, "y")
        self.assertAlmostEqual(x, 98)


class DrawTests(SimpleTestCase):
    def test_draw_contains_parts_and_wires(self):
        svg = schematic.draw_schematic(load("v8_hier.kicad_sch"))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('data-ref="R1"', svg)
        self.assertIn('data-value="10k"', svg)
        self.assertIn('viewBox="0 0 297 210"', svg)
        self.assertIn("<circle", svg)          # junction
        self.assertIn('data-sheet="buck.kicad_sch"', svg)
        self.assertIn("VBUS", svg)
        self.assertIn("PWR power board", svg)

    def test_text_escaped(self):
        sch = load("v8_hier.kicad_sch")
        sch.symbols[0].value = '<b>&"x'
        svg = schematic.draw_schematic(sch)
        self.assertNotIn("<b>", svg)
        self.assertIn("&lt;b&gt;&amp;", svg)
        self.assertIn("&lt;check&gt; R1 &amp; C1", svg)

    def test_highlight(self):
        sch = load("v8_hier.kicad_sch")
        uid = next(s.uuid for s in sch.symbols if s.ref == "R7")
        svg = schematic.draw_schematic(sch, highlight={uid: "changed"})
        self.assertIn('class="sym hl-changed"', svg)

    def test_all_versions_draw(self):
        for name in ("v6_simple.kicad_sch", "v8_buck.kicad_sch", "v9_simple.kicad_sch"):
            self.assertIn("<svg", schematic.draw_schematic(load(name)))
