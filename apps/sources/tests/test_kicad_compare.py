import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources import checkin
from apps.sources.kicad import compare, pcb, schematic

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"
SCH = (FIX / "v8_hier.kicad_sch").read_text()
PCB = (FIX / "v8_board.kicad_pcb").read_text()


class SchematicCompareTests(SimpleTestCase):
    def test_value_change_is_change(self):
        a = schematic.load_schematic(SCH)
        b = schematic.load_schematic(SCH.replace('"Value" "4.7k"', '"Value" "12k"'))
        d = compare.compare_schematics(a, b)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        self.assertEqual(d["changed"], [{"ref": "R7", "what": ["value 4.7k → 12k"]}])
        uid = next(s.uuid for s in b.symbols if s.ref == "R7")
        self.assertEqual(d["highlight_b"][uid], "changed")

    def test_renamed_reference_same_uuid_is_change(self):
        a = schematic.load_schematic(SCH)
        b = schematic.load_schematic(SCH.replace('"Reference" "R7"', '"Reference" "R8"'))
        d = compare.compare_schematics(a, b)
        self.assertEqual(d["changed"], [{"ref": "R8", "what": ["renamed from R7"]}])

    def test_added_removed_parts(self):
        a = schematic.load_schematic(SCH)
        b = schematic.load_schematic(SCH.replace("bbbb0000-0000-0000-0000-000000000003", "bbbb0000-0000-0000-0000-00000000ffff")
                                     .replace('"Reference" "C1"', '"Reference" "C9"'))
        d = compare.compare_schematics(a, b)
        self.assertEqual(d["added"], ["C9"])
        self.assertEqual(d["removed"], ["C1"])

    def test_nets_diff(self):
        a = schematic.load_schematic(SCH)
        b = schematic.load_schematic(SCH.replace('(label "VSW"', '(label "VOUT"'))
        d = compare.compare_schematics(a, b)
        self.assertEqual((d["nets_added"], d["nets_removed"]), (["VOUT"], ["VSW"]))


class PcbCompareTests(SimpleTestCase):
    def test_pcb_moved_part(self):
        a = pcb.load_pcb(PCB)
        b = pcb.load_pcb(PCB.replace("(at 10 15 90)", "(at 12 15 180)"))
        d = compare.compare_pcbs(a, b)
        self.assertEqual(d["changed"], [{"ref": "R7", "what": ["moved 2.00 mm", "rotated 90° → 180°"]}])
        self.assertEqual(d["tracks"], (3, 3))
        self.assertEqual(d["vias"], (1, 1))

    def test_pcb_side_and_value(self):
        a = pcb.load_pcb(PCB)
        b = pcb.load_pcb(PCB.replace('"Value" "10k"', '"Value" "22k"'))
        self.assertEqual(compare.compare_pcbs(a, b)["changed"], [{"ref": "R1", "what": ["value 10k → 22k"]}])


class CompareViewTests(TestCase):
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

    def url(self, path):
        return reverse("sources:kicad_compare", args=["PWR", self.f.repo.slug, 1, 2, path])

    def test_compare_page(self):
        r = self.c.get(self.url("pwr.kicad_sch"))
        self.assertContains(r, "10k → 12k")
        self.assertContains(r, "?compare=1")
        r = self.c.get(self.url("pwr.kicad_pcb"))
        self.assertContains(r, "moved 2.00 mm")

    def test_highlighted_svg(self):
        svg = reverse("sources:kicad_svg", args=["PWR", self.f.repo.slug, 2, "pwr.kicad_sch"])
        r = self.c.get(svg + "?compare=1")
        self.assertIn(b"hl-changed", r.getvalue())
        r = self.c.get(reverse("sources:kicad_svg", args=["PWR", self.f.repo.slug, 2, "pwr.kicad_pcb"]) + "?compare=1")
        self.assertIn(b"hl-changed", r.getvalue())

    def test_compare_links(self):
        page = self.c.get(reverse("sources:kicad", args=["PWR", self.f.repo.slug, 2, "pwr.kicad_sch"])).content.decode()
        self.assertIn(self.url("pwr.kicad_sch"), page)
        page = self.c.get(reverse("sources:compare", args=["PWR", self.f.repo.slug])).content.decode()
        self.assertIn(self.url("pwr.kicad_sch"), page)

    def test_permissions(self):
        self.assertIn(signed_in(self.f.outsider).get(self.url("pwr.kicad_sch")).status_code, (403, 404))
