"""Tests for the problems found in the 1.7.0 review."""
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock
from xml.dom import minidom

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.design.gerber import Budget, GerberLimitError
from apps.sources import checkin
from apps.sources.kicad import pcb, render, schematic, sexpr

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"
SCH = (FIX / "v8_hier.kicad_sch").read_text()
PCB = (FIX / "v8_board.kicad_pcb").read_text()


class SpeedTests(SimpleTestCase):
    def test_many_strings_parse_quickly(self):
        text = "(a " + '"x" ' * 300_000 + ")"
        started = time.monotonic()
        sexpr.parse(text)
        self.assertLess(time.monotonic() - started, 3)

    def test_whitespace_does_not_count_as_nodes(self):
        with mock.patch.object(sexpr, "MAX_NODES", 10):
            sexpr.parse("(a" + " " * 1000 + "b)")

    def test_parse_has_a_deadline(self):
        with self.assertRaisesMessage(sexpr.KicadError, "too long"):
            sexpr.parse("(a " + "(b) " * 200_000 + ")", budget=Budget(seconds=0))

    def test_outline_joining_is_fast(self):
        lines = "".join(f'(gr_line (start {i * 3} 0) (end {i * 3 + 1} 0) (layer "Edge.Cuts") (width 0.1))\n' for i in range(4000))
        board = pcb.load_pcb(f"(kicad_pcb (version 20240108) {lines})")
        started = time.monotonic()
        pcb._outline(board)
        self.assertLess(time.monotonic() - started, 3)


class RenderSafetyTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": SCH, "pwr.kicad_pcb": PCB})
        for path in ("pwr.kicad_sch", "pwr.kicad_pcb"):
            checkin.lock(self.f.repo, path, self.f.eng)
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": SCH.replace('"Value" "10k"', '"Value" "12k"'),
                                           "pwr.kicad_pcb": PCB.replace("(at 10 15 90)", "(at 12 15 90)")})
        self.c = signed_in(self.f.eng)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def svg_url(self, v, path):
        return reverse("sources:kicad_svg", args=["PWR", self.f.repo.slug, v, path])

    def test_compare_panes_use_different_ids(self):
        a = self.c.get(self.svg_url(1, "pwr.kicad_pcb") + "?compare=2").content
        b = self.c.get(self.svg_url(2, "pwr.kicad_pcb") + "?compare=1").content
        ids = lambda s: {x.split(b'"')[0] for x in s.split(b' id="')[1:]}  # noqa: E731
        self.assertFalse(ids(a) & ids(b))

    def test_odd_numbers_no_error(self):
        self.assertEqual(self.c.get(self.svg_url(2, "pwr.kicad_sch") + "?compare=%C2%B2").status_code, 200)
        self.assertEqual(sexpr.file_version(sexpr.parse("(x (version ²))")), 0)
        huge = SCH.replace('"Reference" "R7"', '"Reference" "R' + "9" * 5000 + '"')
        sch = schematic.load_schematic(huge)
        self.assertTrue(sch.parts())

    def test_compare_view_survives_unexpected_errors(self):
        url = reverse("sources:kicad_compare", args=["PWR", self.f.repo.slug, 1, 2, "pwr.kicad_sch"])
        with mock.patch("apps.sources.kicad.compare.compare_schematics", side_effect=ValueError("boom")):
            r = self.c.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "can't be compared")

    def test_compare_summary_is_cached(self):
        url = reverse("sources:kicad_compare", args=["PWR", self.f.repo.slug, 1, 2, "pwr.kicad_sch"])
        self.c.get(url)
        with mock.patch.object(render, "parse_blob", side_effect=AssertionError("parsed again")):
            self.assertContains(self.c.get(url), "10k → 12k")
            self.assertEqual(self.c.get(self.svg_url(2, "pwr.kicad_sch") + "?compare=1").status_code, 200)

    def test_timeouts_are_not_cached(self):
        vf = self.f.repo.latest().files.get(path="pwr.kicad_sch")
        with mock.patch.object(render, "_draw", side_effect=GerberLimitError("These files took too long to draw")):
            self.assertFalse(render.render_blob(vf.blob, "pwr.kicad_sch")["ok"])
        self.assertTrue(render.render_blob(vf.blob, "pwr.kicad_sch")["ok"])

    def test_prune_caps_total_size(self):
        d = render.cache_dir()
        for i in range(5):
            p = d / f"f{i}-sch-1.json"
            p.write_text("x" * 1000)
            os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
        (d / "stale.partial").write_text("x")
        os.utime(d / "stale.partial", (time.time() - 7200, time.time() - 7200))
        with mock.patch.object(render, "MAX_CACHE_BYTES", 2500):
            render.prune()
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["f3-sch-1.json", "f4-sch-1.json"])

    def test_trail_is_encoded(self):
        commit(self.f.repo, self.f.eng, **{"a&b.kicad_sch": SCH, "buck.kicad_sch": (FIX / "v8_buck.kicad_sch").read_text()})
        page = self.c.get(reverse("sources:kicad", args=["PWR", self.f.repo.slug, 3, "a&b.kicad_sch"])).content.decode()
        self.assertIn("trail=a%26b.kicad_sch", page)


class DrawingTests(SimpleTestCase):
    def test_rotated_and_mirrored_like_kicad(self):
        x, y = schematic.place((0, 3.81), 100, 50, 90, "x")
        self.assertAlmostEqual(x, 103.81)
        self.assertAlmostEqual(y, 50)

    def test_fields_of_rotated_symbol_stay_readable(self):
        svg = schematic.draw_schematic(schematic.load_schematic(SCH))
        r7 = svg[svg.index('data-ref="R7"'):]
        r7 = r7[:r7.index("</g>")]
        ref = r7[r7.index(">R7<") - 300:r7.index(">R7<")]
        self.assertNotIn("rotate(", ref.split("<text")[-1])

    def test_control_characters_keep_svg_valid(self):
        sch = schematic.load_schematic(SCH.replace('"Value" "10k"', '"Value" "10\x01k"'))
        minidom.parseString(schematic.draw_schematic(sch))
        r = pcb.render_pcb(PCB.replace('"PWR <B> & co"', '"PWR \x02"'))
        minidom.parseString(r["svg"])

    def test_multi_unit_symbol_listed_once(self):
        unit2 = SCH.replace('(uuid "bbbb0000-0000-0000-0000-000000000001")', '(uuid "bbbb0000-0000-0000-0000-0000000000u2")') \
                   .replace("(at 100 50 0) (unit 1)", "(at 200 50 0) (unit 2)", 1)
        both = SCH[:SCH.rindex("(sheet_instances")] + unit2[unit2.index('(symbol (lib_id "Device:R") (at 200'):unit2.index("(symbol (lib_id \"Device:R\") (at 120")] \
            + SCH[SCH.rindex("(sheet_instances"):]
        refs = [p["ref"] for p in schematic.load_schematic(both).parts()]
        self.assertEqual(refs.count("R1"), 1)
