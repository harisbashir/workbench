from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.firmware.models import Firmware, FirmwareRelease
from apps.inventory.models import BomLine, Part, StockMovement, Supplier
from apps.production.fab import _num, parse_placements
from apps.production.models import BuildOrder, BuildStep, PurchaseOrder
from apps.projects.models import Project, Revision


class Base(TestCase):
    def setUp(self):
        self.buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.eng = make_user("eng")
        self.supplier = Supplier.objects.create(name="LCSC")
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.eng)
        self.rev = Revision.objects.create(project=self.project, name="Rev A")
        self.mcu = Part.objects.create(description="MCU", supplier=self.supplier, unit_cost=Decimal("1.50"), stock=10)
        self.conn = Part.objects.create(description="Header", supplier=self.supplier, unit_cost=Decimal("0.20"), stock=100)
        BomLine.objects.create(revision=self.rev, part=self.mcu, quantity=1, fitted_by="house", references="U1")
        BomLine.objects.create(revision=self.rev, part=self.conn, quantity=2, fitted_by="house", references="J1, J2")
        self.c = signed_in(self.buyer)

    def build(self, qty=5, assembly="house", **kw):
        return BuildOrder.objects.create(revision=self.rev, quantity=qty, assembly=assembly, **kw)

    def stock(self, part):
        part.refresh_from_db()
        return part.stock

    def act(self, b, **data):
        return self.c.post(reverse("production:build_action", args=[b.pk]), data)

    def stage(self, b, **data):
        return self.c.post(reverse("production:build_stage", args=[b.pk]), data)


class StockConsumptionTests(Base):
    def test_consume_is_once_even_from_stale_objects(self):
        b = self.build()
        b1, b2 = BuildOrder.objects.get(pk=b.pk), BuildOrder.objects.get(pk=b.pk)
        self.assertTrue(b1.begin(BuildOrder.Stage.FINISHING))
        self.assertTrue(b1.consume_stock(self.buyer))
        self.assertFalse(b2.begin(BuildOrder.Stage.FINISHING))
        self.assertFalse(b2.consume_stock(self.buyer))
        self.assertEqual(self.stock(self.mcu), 5)

    def test_double_submitted_start_takes_parts_once(self):
        for view, data in (("stage", {"action": "start_house"}), ("act", {"action": "start"})):
            b = self.build(qty=2)
            getattr(self, view)(b, **data)
            getattr(self, view)(b, **data)
        self.assertEqual(self.stock(self.mcu), 10 - 2 - 2)
        self.assertEqual(self.mcu.movements.count(), 2)

    def test_double_submitted_fab_order_takes_parts_once(self):
        b = self.build(qty=3, assembly="fab")
        self.stage(b, action="fab_ordered", fab_order_ref="SO1")
        self.stage(b, action="fab_ordered", fab_order_ref="SO1")
        self.assertEqual(self.stock(self.mcu), 7)

    def test_cancelled_build_cannot_be_restarted(self):
        b = self.build()
        self.act(b, action="cancel")
        for action in ("start_house", "fab_ordered"):
            self.stage(b, action=action)
        self.act(b, action="start")
        self.act(b, action="complete", passed=5, failed=0)
        b.refresh_from_db()
        self.assertEqual(b.status, "cancelled")
        self.assertEqual(self.stock(self.mcu), 10)

    def test_cancel_in_progress_returns_parts(self):
        b = self.build()
        self.stage(b, action="start_house")
        self.assertEqual(self.stock(self.mcu), 5)
        self.act(b, action="cancel", return_parts="1")
        b.refresh_from_db()
        self.assertEqual(b.status, "cancelled")
        self.assertEqual((self.stock(self.mcu), self.stock(self.conn)), (10, 100))
        self.assertEqual(self.mcu.movements.filter(reason=StockMovement.Reason.RETURN).count(), 1)
        self.act(b, action="cancel", return_parts="1")  # repeat: nothing more comes back
        self.assertEqual(self.stock(self.mcu), 10)

    def test_cancel_in_progress_without_returning(self):
        b = self.build()
        self.stage(b, action="start_house")
        self.act(b, action="cancel")
        self.assertEqual(self.stock(self.mcu), 5)

    def test_return_leftover_parts_capped_at_what_was_taken(self):
        b = self.build()
        self.stage(b, action="start_house")
        self.act(b, action="return_parts", **{f"return_{self.conn.pk}": "4", f"return_{self.mcu.pk}": "99", "return_x": "1"})
        self.assertEqual((self.stock(self.conn), self.stock(self.mcu)), (94, 10))
        self.assertEqual(b.consumed_parts(), {self.conn.pk: 6})
        r = self.c.get(reverse("production:build_detail", args=[b.pk]))
        self.assertContains(r, f'name="return_{self.conn.pk}"')

    def test_fewer_boards_received_returns_their_parts(self):
        b = self.build(qty=5, assembly="fab")
        self.stage(b, action="fab_ordered")
        self.assertEqual(self.stock(self.mcu), 5)
        self.stage(b, action="received", received_qty=3)
        self.stage(b, action="received", received_qty=3)  # repeat is ignored
        self.assertEqual((self.stock(self.mcu), self.stock(self.conn)), (7, 94))
        b.refresh_from_db()
        self.assertEqual((b.received_qty, b.stage), (3, "finishing"))


class CompleteTests(Base):
    def test_passed_plus_failed_cannot_exceed_boards(self):
        b = self.build(qty=5)
        self.stage(b, action="start_house")
        self.act(b, action="complete", passed=5, failed=2)
        b.refresh_from_db()
        self.assertEqual(b.status, "in_progress")
        self.act(b, action="complete", passed=4, failed=1)
        b.refresh_from_db()
        self.assertEqual((b.status, b.completed_qty, b.stage), ("completed", 4, "done"))

    def test_complete_refused_with_recalled_firmware_unless_forced(self):
        fw = Firmware.objects.create(project=self.project, name="App")
        rel = FirmwareRelease.objects.create(firmware=fw, version="1.0.0", status="released")
        b = self.build(qty=2)
        b.firmware_releases.set([rel])
        self.stage(b, action="start_house")
        rel.status = "recalled"
        rel.save()
        self.act(b, action="complete", passed=2, failed=0)
        b.refresh_from_db()
        self.assertEqual(b.status, "in_progress")
        self.act(b, action="complete", passed=2, failed=0, force="1")
        b.refresh_from_db()
        self.assertEqual(b.status, "completed")

    def test_set_firmware_only_accepts_released_or_testing(self):
        fw = Firmware.objects.create(project=self.project, name="App")
        ok = FirmwareRelease.objects.create(firmware=fw, version="1.0.0", status="released")
        bad = FirmwareRelease.objects.create(firmware=fw, version="0.9.0", status="recalled")
        draft = FirmwareRelease.objects.create(firmware=fw, version="1.1.0", status="draft")
        b = self.build()
        self.stage(b, action="start_house")
        r = self.act(b, action="set_firmware", firmware=[ok.pk, bad.pk, draft.pk, "abc"])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(list(b.firmware_releases.all()), [ok])


class ShortageOrderTests(Base):
    def test_two_builds_short_of_the_same_part_order_enough_for_both(self):
        a, b = self.build(qty=30), self.build(qty=30)
        self.act(a, action="order_shortages")
        self.act(b, action="order_shortages")
        self.act(b, action="order_shortages")  # pressing again orders nothing more
        line = PurchaseOrder.objects.get().lines.get(part=self.mcu)
        self.assertEqual(line.quantity, 60 - 10)

    def test_foreign_currency_supplier_gets_no_base_price(self):
        self.supplier.currency = "EUR"
        self.supplier.save()
        self.act(self.build(qty=30), action="order_shortages")
        self.assertEqual(PurchaseOrder.objects.get().lines.get(part=self.mcu).unit_cost, 0)


class PurchaseOrderTests(Base):
    def order(self, qty=10, cost="1.40"):
        po = PurchaseOrder.objects.create(supplier=self.supplier, status="ordered")
        return po, po.lines.create(part=self.mcu, quantity=qty, unit_cost=Decimal(cost))

    def test_receive_from_stale_line_objects_counts_once(self):
        po, line = self.order()
        stale = po.lines.get(pk=line.pk)
        self.assertEqual(line.receive(10, self.buyer), 10)
        self.assertEqual(stale.receive(10, self.buyer), 0)
        self.assertEqual(self.stock(self.mcu), 20)

    def test_receive_all_twice(self):
        po, _ = self.order()
        url = reverse("production:po_action", args=[po.pk])
        self.c.post(url, {"action": "receive_all"})
        self.c.post(url, {"action": "receive_all"})
        self.assertEqual(self.stock(self.mcu), 20)

    def test_partial_order_can_be_closed(self):
        po, line = self.order()
        url = reverse("production:po_action", args=[po.pk])
        self.c.post(url, {"action": "receive", f"recv_{line.pk}": "4"})
        self.c.post(url, {"action": "close"})
        po.refresh_from_db()
        self.assertEqual(po.status, "closed")
        self.c.post(url, {"action": "receive_all"})
        self.assertEqual(self.stock(self.mcu), 14)
        r = self.c.get(reverse("production:po_list"))
        self.assertNotIn(po, list(r.context["orders"]))  # no longer "open"

    def test_foreign_currency_does_not_overwrite_part_cost(self):
        self.supplier.currency = "EUR"
        self.supplier.save()
        po, _ = self.order(cost="9.99")
        self.c.post(reverse("production:po_action", args=[po.pk]), {"action": "receive_all"})
        self.mcu.refresh_from_db()
        self.assertEqual((self.mcu.stock, self.mcu.unit_cost), (20, Decimal("1.50")))
        r = self.c.get(reverse("production:po_detail", args=[po.pk]))
        self.assertContains(r, "EUR")

    def test_foreign_currency_line_needs_a_price(self):
        self.supplier.currency = "EUR"
        self.supplier.save()
        po = PurchaseOrder.objects.create(supplier=self.supplier)
        url = reverse("production:po_detail", args=[po.pk])
        self.c.post(url, {"part": self.mcu.pk, "quantity": 5, "unit_cost": ""})
        self.assertFalse(po.lines.exists())
        self.c.post(url, {"part": self.mcu.pk, "quantity": 5, "unit_cost": "1.2"})
        self.assertEqual(po.lines.get().unit_cost, Decimal("1.2"))

    def test_non_numeric_input_is_not_a_500(self):
        po = PurchaseOrder.objects.create(supplier=self.supplier)
        r = self.c.post(reverse("production:po_action", args=[po.pk]), {"remove_line": "abc"})
        self.assertEqual(r.status_code, 302)
        member = signed_in(self.eng)
        r = member.post(reverse("production:bom_fitted", args=[self.rev.pk]), {"line": "abc"})
        self.assertEqual(r.status_code, 404)
        r = member.post(reverse("production:bom_fitted", args=[self.rev.pk]),
                        {"line": self.rev.bom_lines.first().pk, "to": "fab", "next": "https://evil.example/"})
        self.assertEqual(r["Location"], f"/parts/boms/{self.rev.pk}/")


class StepTests(Base):
    def test_step_counts_use_the_database_value(self):
        b = self.build(qty=5)
        self.stage(b, action="start_house")
        step = b.steps.first()
        url = reverse("production:build_step", args=[b.pk, step.pk])
        for _ in range(3):
            self.c.post(url, {"how": "1"})
        self.c.post(url, {"how": "99"})
        step.refresh_from_db()
        self.assertEqual(step.done_qty, 5)

    def test_no_step_changes_on_finished_builds(self):
        b = self.build(qty=5)
        self.stage(b, action="start_house")
        self.act(b, action="cancel")
        step = b.steps.first()
        self.c.post(reverse("production:build_step", args=[b.pk, step.pk]), {"how": "all"})
        self.stage(b, action="add_step", title="Wash")
        self.assertEqual(BuildStep.objects.get(pk=step.pk).done_qty, 0)
        self.assertFalse(b.steps.filter(title="Wash").exists())


class PlacementNumberTests(SimpleTestCase):
    def test_numbers(self):
        self.assertEqual(_num("-.5"), -0.5)
        self.assertAlmostEqual(_num("1.2e-3"), 0.0012)
        self.assertEqual(_num("12,5", decimal_comma=True), 12.5)
        self.assertIsNone(_num("12,5"))
        self.assertIsNone(_num("abc"))
        self.assertAlmostEqual(_num("400mil"), 10.16)

    def test_decimal_comma_file_and_warnings(self):
        rows, warnings = parse_placements(b"Designator;Mid X;Mid Y;Layer;Rotation\nR1;12,5;-3,25;Top;90\nR2;abc;1;Top;0\n")
        self.assertEqual([(r["ref"], r["x"], r["y"]) for r in rows], [("R1", 12.5, -3.25)])
        self.assertEqual(len(warnings), 1)
        self.assertIn("R2", warnings[0])

    def test_kicad_ascii_value_with_spaces(self):
        rows, _ = parse_placements(b"# Ref Val Package PosX PosY Rot Side\nR1 10k 1% R_0603 1.0 -2.0 90 top\n")
        self.assertEqual((rows[0]["val"], rows[0]["package"], rows[0]["x"]), ("10k 1%", "R_0603", 1.0))


class FabPageWarningTests(Base):
    def test_fab_page_shows_placement_warnings(self):
        from apps.design.models import DesignFile
        DesignFile.store(self.rev, SimpleUploadedFile("board-pos.csv", b"Ref,Val,Package,PosX,PosY,Rot,Side\nU1,MCU,QFN,1,1,0,top\nJ1,H,H,x,1,0,top\n"),
                         self.buyer, category="pnp", background=False)
        r = self.c.get(reverse("production:fab_package", args=[self.rev.pk]))
        self.assertContains(r, "couldn")
