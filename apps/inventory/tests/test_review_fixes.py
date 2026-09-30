from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.inventory import bom_import
from apps.inventory.models import BomLine, Part
from apps.inventory.views import _apply_import
from apps.projects.models import Project, Revision


class ImportDnpTests(TestCase):
    """The same part in a fitted row and a DNP row must not turn the fitted parts into DNP (or vice versa)."""

    def setUp(self):
        self.p = Project.objects.create(key="PWR", name="Power")
        self.rev = Revision.objects.create(project=self.p, name="Rev A")

    def _import(self, csv):
        rows, _ = bom_import.parse(csv)
        return _apply_import(self.rev, rows, True, None)

    def test_dnp_row_first(self):
        _c, _l, notes = self._import(b"Reference,Value,Footprint,Qty,DNP\nR5,10k,R_0603,1,DNP\nR1 R2 R3,10k,R_0603,3,\n")
        line = self.rev.bom_lines.get()
        self.assertEqual((line.dnp, line.quantity, line.references), (False, 3, "R1, R2, R3"))
        self.assertIn("R5", line.notes)
        self.assertTrue(notes)

    def test_fitted_row_first(self):
        self._import(b"Reference,Value,Footprint,Qty,DNP\nR1 R2 R3,10k,R_0603,3,\nR5,10k,R_0603,1,DNP\n")
        line = self.rev.bom_lines.get()
        self.assertEqual((line.dnp, line.quantity, line.references), (False, 3, "R1, R2, R3"))
        self.assertIn("R5", line.notes)

    def test_same_dnp_state_still_merges(self):
        self._import(b"Reference,Value,Footprint,Qty,DNP\nR1,10k,R_0603,1,\nR2,10k,R_0603,1,\n")
        line = self.rev.bom_lines.get()
        self.assertEqual((line.quantity, line.references, line.dnp), (2, "R1, R2", False))

    def test_preview_explains_the_merge(self):
        eng = make_user("eng")
        self.p.members.add(eng)
        c = signed_in(eng)
        f = SimpleUploadedFile("bom.csv", b"Reference,Value,Footprint,Qty,DNP\nR5,10k,R_0603,1,DNP\nR1,10k,R_0603,1,\n")
        r = c.post(reverse("inventory:bom_import", args=[self.rev.pk]), {"file": f, "replace": "on"})
        self.assertContains(r, "Same part both fitted and DNP")


class ParserRobustnessTests(TestCase):
    def test_cp1252_fallback(self):
        rows, warnings = bom_import.parse("Reference,Value,Footprint,Qty\nC1,4.7µF,C_0603,1\n".encode("cp1252"))
        self.assertEqual(rows[0]["value"], "4.7µF")
        self.assertTrue(any("Windows-1252" in w for w in warnings))

    def test_skipped_rows_are_reported(self):
        rows, warnings = bom_import.parse(b"Reference;Value;Footprint;Qty\n;10k;R_0603;\nC1;100n;C_0402;2\nC9;1u;C_0402;0\n")
        self.assertEqual([r["references"] for r in rows], ["C1"])
        self.assertEqual(len(warnings), 2)
        self.assertIn("no references and no quantity", warnings[0])

    def test_absurd_quantities(self):
        rows, warnings = bom_import.parse(b"Reference,Value,Qty\nR1,10k,1e999\nR2,10k,99999999999\n")
        self.assertEqual([r["quantity"] for r in rows], [1])
        self.assertTrue(any("more than" in w for w in warnings))


class StockAdjustTests(TestCase):
    def test_set_exact_count_uses_current_stock(self):
        buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        part = Part.objects.create(description="MCU", stock=10)
        Part.objects.filter(pk=part.pk).update(stock=4)  # changed since the page was loaded
        signed_in(buyer).post(reverse("inventory:part_adjust", args=[part.pk]), {"mode": "set", "quantity": 7})
        part.refresh_from_db()
        self.assertEqual(part.stock, 7)
        self.assertEqual(part.movements.get().delta, 3)


class BomPageTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.eng)
        self.a = Revision.objects.create(project=self.p, name="Rev A")
        self.b = Revision.objects.create(project=self.p, name="Rev B")
        r1 = Part.objects.create(description="10k", unit_cost=Decimal("0.01"))
        r2 = Part.objects.create(description="22k")
        r3 = Part.objects.create(description="MCU")
        BomLine.objects.create(revision=self.a, part=r1, quantity=2, references="R1, R2")
        BomLine.objects.create(revision=self.a, part=r2, quantity=1, references="R3")
        BomLine.objects.create(revision=self.b, part=r1, quantity=3, references="R1, R2, R4")
        BomLine.objects.create(revision=self.b, part=r3, quantity=1, references="U1")

    def test_compare_with_added_and_removed_parts(self):
        c = signed_in(self.eng)
        r = c.get(reverse("inventory:bom_compare", args=[self.b.pk, self.a.pk]) + "?all=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["summary"], {"added": 1, "removed": 1, "changed": 1, "same": 0})
        self.assertContains(r, "R3")  # removed line shows the old references
        self.assertContains(r, "U1")

    def test_engineer_sees_no_edit_links_on_a_released_revision(self):
        self.b.status = Revision.Status.RELEASED
        self.b.save()
        line = self.b.bom_lines.first()
        c = signed_in(self.eng)
        r = c.get(reverse("inventory:bom", args=[self.b.pk]))
        for name, args in (("bom_line_edit", [self.b.pk, line.pk]), ("bom_line_create", [self.b.pk]), ("bom_import", [self.b.pk])):
            url = reverse(f"inventory:{name}", args=args)
            self.assertNotContains(r, f'href="{url}"')
            self.assertEqual(c.get(url).status_code, 403)
        self.assertNotContains(r, reverse("production:bom_fitted", args=[self.b.pk]))

    def test_links_shown_to_those_who_can_use_them(self):
        line = self.a.bom_lines.first()
        r = signed_in(self.eng).get(reverse("inventory:bom", args=[self.a.pk]))
        self.assertContains(r, reverse("inventory:bom_line_edit", args=[self.a.pk, line.pk]))
        self.b.status = Revision.Status.RELEASED
        self.b.save()
        r = signed_in(self.lead).get(reverse("inventory:bom", args=[self.b.pk]))
        self.assertContains(r, reverse("inventory:bom_import", args=[self.b.pk]))
