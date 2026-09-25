from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.core.testing import make_user, signed_in
from apps.inventory import bom_import
from apps.inventory.models import BomLine, Part
from apps.projects.models import Project, Revision

SAMPLE = Path(settings.BASE_DIR) / "docs" / "examples" / "sample_bom_rev_b.csv"

KIBOT_STYLE = b"""Project info:
Title,Power board
Row,Description,Part,References,Value,Footprint,Quantity Per PCB,manf#
1,Unpolarized capacitor,C,C1 C2,100nF,Capacitor_SMD:C_0603_1608Metric,2,CL10B104KB8NNNC
2,Resistor,R,R1,10k,Resistor_SMD:R_0603_1608Metric,1,
"""


class ParserTests(TestCase):
    def test_parses_kicad_export(self):
        rows, warnings = bom_import.parse(SAMPLE.read_bytes())
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 17)
        caps = rows[0]
        self.assertEqual((caps["value"], caps["quantity"], caps["category"]), ("100nF", 5, "capacitor"))
        self.assertTrue(next(r for r in rows if r["references"] == "R7")["dnp"])

    def test_parses_kibot_export_with_title_block(self):
        rows, _ = bom_import.parse(KIBOT_STYLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["references"], "C1, C2")
        self.assertEqual(rows[0]["quantity"], 2)

    def test_rejects_unrelated_csv(self):
        rows, warnings = bom_import.parse(b"name,email\nali,a@b.c\n")
        self.assertEqual(rows, [])
        self.assertTrue(warnings)


class ImportFlowTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.eng)
        self.rev = Revision.objects.create(project=self.project, name="Rev B")
        Part.objects.create(description="existing 10k", value="10k", mpn="RC0603FR-0710KL", stock=100)

    def test_preview_then_confirm(self):
        c = signed_in(self.eng)
        url = reverse("inventory:bom_import", args=[self.rev.pk])
        r = c.post(url, {"file": SimpleUploadedFile("bom.csv", SAMPLE.read_bytes()), "replace": "on"})
        self.assertContains(r, "Save BOM")
        self.assertEqual(BomLine.objects.count(), 0, "nothing saved before confirming")
        c.post(url, {"confirm": "1"})
        self.assertEqual(self.rev.bom_lines.count(), 17)
        self.assertEqual(Part.objects.filter(mpn="RC0603FR-0710KL").count(), 1, "matched by MPN, not duplicated")
        r = c.get(reverse("inventory:bom", args=[self.rev.pk]))
        self.assertContains(r, "Boards buildable from stock")

    def test_released_revision_locked_for_engineers(self):
        self.rev.status = Revision.Status.RELEASED
        self.rev.save()
        self.assertEqual(signed_in(self.eng).get(reverse("inventory:bom_import", args=[self.rev.pk])).status_code, 403)

    def test_compare(self):
        other = Revision.objects.create(project=self.project, name="Rev A")
        p = Part.objects.first()
        BomLine.objects.create(revision=other, part=p, quantity=1)
        BomLine.objects.create(revision=self.rev, part=p, quantity=2)
        r = signed_in(self.eng).get(reverse("inventory:bom_compare", args=[self.rev.pk, other.pk]))
        self.assertContains(r, "Changed")

    def test_part_numbers_generated(self):
        a = Part.objects.create(description="x", category="capacitor")
        b = Part.objects.create(description="y", category="capacitor")
        self.assertEqual((a.ipn, b.ipn), ("CAP-0001", "CAP-0002"))
