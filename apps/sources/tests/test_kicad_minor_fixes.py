"""Tests for the smaller problems left over from the 1.7.0 review."""
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import unquote
from xml.dom import minidom

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources.kicad import pcb, render, schematic

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"
SCH = (FIX / "v8_hier.kicad_sch").read_text()
BUCK = (FIX / "v8_buck.kicad_sch").read_text()
PCB = (FIX / "v8_board.kicad_pcb").read_text()
ROOT = "/11111111-0000-0000-0000-000000000001"
SHEET1, SHEET2 = "cccc0000-0000-0000-0000-000000000001", "cccc0000-0000-0000-0000-000000000002"

# The buck sheet used twice: the second copy's regulator is U13.
SHEET_NODE = SCH[SCH.index("  (sheet (at 160 30)"):SCH.index("  (sheet_instances")]
ROOT_TWICE = SCH.replace("  (sheet_instances", SHEET_NODE.replace(SHEET1, SHEET2).replace("(at 160 30)", "(at 160 80)")
                         .replace('"Sheetname" "Buck"', '"Sheetname" "Buck 2"') + "  (sheet_instances")
BUCK_TWICE = BUCK.replace(f'(path "{ROOT}/{SHEET1}" (reference "U3") (unit 1))',
                          f'(path "{ROOT}/{SHEET1}" (reference "U3") (unit 1)) (path "{ROOT}/{SHEET2}" (reference "U13") (unit 1))')


class TempCache:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)


class CacheSplitTests(TempCache, TestCase):
    def setUp(self):
        super().setUp()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": SCH})
        self.vf = self.f.repo.latest().files.get(path="pwr.kicad_sch")

    def test_page_facts_are_kept_apart_from_the_drawing(self):
        meta = render.render_blob(self.vf.blob, "pwr.kicad_sch", svg=False)
        self.assertTrue(meta["ok"])
        self.assertNotIn("svg", meta)
        self.assertIn("R7", [p["ref"] for p in meta["parts"]])
        files = {p.suffix for p in render.cache_dir().iterdir()}
        self.assertEqual(files, {".json", ".svg"})
        for p in render.cache_dir().glob("*.json"):
            self.assertNotIn("<svg", p.read_text())
        self.assertIn("<svg", render.render_blob(self.vf.blob, "pwr.kicad_sch")["svg"])

    def test_missing_drawing_is_drawn_again(self):
        render.render_blob(self.vf.blob, "pwr.kicad_sch")
        for p in render.cache_dir().glob("*.svg"):
            p.unlink()
        self.assertIn("<svg", render.render_blob(self.vf.blob, "pwr.kicad_sch")["svg"])

    def test_page_and_drawing_still_work(self):
        c = signed_in(self.f.eng)
        self.assertContains(c.get(reverse("sources:kicad", args=["PWR", self.f.repo.slug, 1, "pwr.kicad_sch"])), "R7")
        r = c.get(reverse("sources:kicad_svg", args=["PWR", self.f.repo.slug, 1, "pwr.kicad_sch"]))
        self.assertIn(b"<svg", r.content)

    def test_prune_removes_drawings_too(self):
        render.render_blob(self.vf.blob, "pwr.kicad_sch")
        with mock.patch.object(render, "MAX_CACHE_BYTES", 0):
            render.prune()
        self.assertEqual(list(render.cache_dir().iterdir()), [])


class PrerenderBookmarkTests(TempCache, TestCase):
    def setUp(self):
        super().setUp()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": SCH, "notes.txt": "x"})

    def test_files_already_looked_at_are_not_scanned_again(self):
        self.assertEqual(render.prerender(), 1)
        with mock.patch.object(render, "kind_of", wraps=render.kind_of) as looked:
            self.assertEqual(render.prerender(), 0)
        looked.assert_not_called()
        commit(self.f.repo, self.f.eng, **{"buck.kicad_sch": BUCK})
        self.assertEqual(render.prerender(), 1)

    def test_stops_and_continues_where_it_left_off(self):
        commit(self.f.repo, self.f.eng, **{"buck.kicad_sch": BUCK})
        self.assertEqual(render.prerender(limit=1), 1)
        self.assertEqual(render.prerender(limit=1), 1)
        self.assertEqual(render.prerender(limit=1), 0)


class PcbDrawingTests(SimpleTestCase):
    def test_highlight_boxes_are_not_clipped_by_the_board_edge(self):
        board = pcb.load_pcb(PCB)
        uid = board.footprints[0].uuid
        svg = pcb.render_pcb("", board=board, highlight={uid: "changed"})["svg"]
        doc = minidom.parseString(svg)
        top = next(g for g in doc.getElementsByTagName("g") if g.getAttribute("data-view") == "top")
        rects = [r for r in top.getElementsByTagName("rect") if r.getAttribute("class") == "hl-changed"]
        self.assertTrue(rects)
        node = rects[0].parentNode
        while node is not top.parentNode:
            self.assertFalse(node.getAttribute("clip-path") or node.getAttribute("mask"))
            node = node.parentNode

    def test_negative_pad_sizes_draw_valid_svg(self):
        text = PCB.replace("(size 0.8 0.95)", "(size -0.8 -0.95)")
        text = text.replace("smd roundrect (at -0.825 0)", "thru_hole circle (at -0.825 0) (drill -0.4)", 1)
        self.assertIn("(size -0.8", text)
        svg = pcb.render_pcb(text)["svg"]
        self.assertFalse(re.search(r'\b(width|height|r|stroke-width)="-', svg))
        minidom.parseString(svg)


class InstanceTests(SimpleTestCase):
    def test_each_copy_of_a_sheet_gets_its_own_references(self):
        sch = schematic.load_schematic(BUCK_TWICE)
        self.assertTrue(sch.use_instance(f"{ROOT}/{SHEET2}"))
        self.assertEqual([p["ref"] for p in sch.parts()], ["U13"])
        sch = schematic.load_schematic(BUCK_TWICE)
        sch.use_instance(f"{ROOT}/{SHEET1}")
        self.assertEqual([p["ref"] for p in sch.parts()], ["U3"])

    def test_kicad6_references_come_from_the_root(self):
        child = BUCK.replace(f'(instances (project "pwr" (path "{ROOT}/{SHEET1}" (reference "U3") (unit 1))))', "")
        extra = f'  (symbol_instances (path "/{SHEET2}/eeee0000-0000-0000-0000-000000000001" (reference "U23") (unit 1)))\n'
        root = schematic.load_schematic(SCH.replace("  (sheet_instances", extra + "  (sheet_instances"))
        sch = schematic.load_schematic(child)
        self.assertTrue(sch.use_instance(f"{ROOT}/{SHEET2}", root.symbol_instances))
        self.assertEqual([p["ref"] for p in sch.parts()], ["U23"])


class InstanceViewTests(TempCache, TestCase):
    def setUp(self):
        super().setUp()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": ROOT_TWICE, "buck.kicad_sch": BUCK_TWICE})
        self.c = signed_in(self.f.eng)

    def page(self, path, query=""):
        return self.c.get(reverse("sources:kicad", args=["PWR", self.f.repo.slug, 1, path]) + query).content.decode()

    def test_sheet_links_name_the_copy_and_show_its_references(self):
        root = self.page("pwr.kicad_sch")
        links = re.findall(r'href="([^"]*buck\.kicad_sch[^"]*)"', root)
        second = next(unquote(u) for u in links if SHEET2 in unquote(u))
        child = self.c.get(second.replace("&amp;", "&")).content.decode()
        self.assertIn("U13", child)
        self.assertNotIn(">U3<", child)
        svg_url = unquote(re.search(r'data-svg-url="([^"]+)"|src="([^"]*svg[^"]*)"', child).group(0).split('"')[1]).replace("&amp;", "&")
        self.assertIn(b"U13", self.c.get(svg_url).content)

    def test_crumbs_keep_the_copy(self):
        child = self.page("buck.kicad_sch", f"?trail=pwr.kicad_sch&inst={ROOT}/{SHEET2}")
        self.assertIn("U13", child)
        self.assertIn('href="/projects/PWR/sources/main-firmware/kicad/1/pwr.kicad_sch"', child)

    def test_bad_instance_is_ignored(self):
        self.assertIn("U3", self.page("buck.kicad_sch", "?inst=../../x"))


class UprightTextTests(SimpleTestCase):
    """Footprint text is kept readable, as KiCad does: text turned past 90° is drawn the other way up."""
    def rotations(self, svg, ref):
        return [m for m in re.findall(r'<text transform="[^"]*rotate\(([-\d.]+)\)[^>]*>([^<]*)</text>', svg) if m[1] == ref]

    def test_upside_down_reference_is_turned_upright(self):
        text = PCB.replace('(at 10 15 90)', '(at 10 15 180)').replace('(property "Reference" "R7" (at 0 -1.43 90)', '(property "Reference" "R7" (at 0 -1.43 180)')
        angles = [float(a) % 360 for a, _ in self.rotations(pcb.render_pcb(text)["svg"], "R7")]
        self.assertTrue(angles)
        self.assertTrue(all(a <= 90 or a > 270 for a in angles), angles)

    def test_sideways_reference_keeps_its_angle(self):
        angles = [float(a) % 360 for a, _ in self.rotations(pcb.render_pcb(PCB)["svg"], "R7")]
        self.assertEqual(angles, [270.0])


class PcbFactsTests(TempCache, TestCase):
    def test_pcb_facts_do_not_carry_a_second_drawing(self):
        f = make_repo(kind="kicad")
        commit(f.repo, f.eng, **{"pwr.kicad_pcb": PCB})
        blob = f.repo.latest().files.get(path="pwr.kicad_pcb").blob
        meta = render.render_blob(blob, "pwr.kicad_pcb", svg=False)
        self.assertTrue(meta["ok"])
        self.assertNotIn("thumb_top", meta)
        for p in render.cache_dir().glob("*.json"):
            self.assertNotIn("<svg", p.read_text())
