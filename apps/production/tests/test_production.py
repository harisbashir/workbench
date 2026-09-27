from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.inventory.models import BomLine, Part, Supplier
from apps.production.models import BuildOrder, PurchaseOrder
from apps.projects.models import Project, Revision


class ProductionTests(TestCase):
    def setUp(self):
        self.buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.eng = make_user("eng")
        self.supplier = Supplier.objects.create(name="LCSC")
        self.project = Project.objects.create(key="PWR", name="Power")
        self.rev = Revision.objects.create(project=self.project, name="Rev A")
        self.r = Part.objects.create(description="10k", supplier=self.supplier, unit_cost=Decimal("0.001"), stock=100)
        self.u = Part.objects.create(description="MCU", supplier=self.supplier, unit_cost=Decimal("1.50"), stock=3)
        BomLine.objects.create(revision=self.rev, part=self.r, quantity=4)
        BomLine.objects.create(revision=self.rev, part=self.u, quantity=1)

    def test_receiving_updates_stock_and_status(self):
        po = PurchaseOrder.objects.create(supplier=self.supplier)
        line = po.lines.create(part=self.u, quantity=10, unit_cost=Decimal("1.40"))
        c = signed_in(self.buyer)
        c.post(reverse("production:po_action", args=[po.pk]), {"action": "order"})
        c.post(reverse("production:po_action", args=[po.pk]), {"action": "receive", f"recv_{line.pk}": "4"})
        po.refresh_from_db()
        self.u.refresh_from_db()
        self.assertEqual((po.status, self.u.stock), ("partial", 7))
        self.assertEqual(self.u.unit_cost, Decimal("1.40"))
        c.post(reverse("production:po_action", args=[po.pk]), {"action": "receive_all"})
        po.refresh_from_db()
        self.u.refresh_from_db()
        self.assertEqual((po.status, self.u.stock), ("received", 13))
        self.assertEqual(self.u.movements.count(), 2)

    def test_cannot_over_receive(self):
        po = PurchaseOrder.objects.create(supplier=self.supplier, status="ordered")
        line = po.lines.create(part=self.u, quantity=2, unit_cost=1)
        signed_in(self.buyer).post(reverse("production:po_action", args=[po.pk]), {"action": "receive", f"recv_{line.pk}": "50"})
        self.u.refresh_from_db()
        self.assertEqual(self.u.stock, 5)

    def test_build_shortages_and_ordering(self):
        b = BuildOrder.objects.create(revision=self.rev, quantity=10, assembly="house")
        self.assertEqual(b.shortage_count, 1)  # needs 10 MCUs, has 3
        signed_in(self.buyer).post(reverse("production:build_action", args=[b.pk]), {"action": "order_shortages"})
        po = PurchaseOrder.objects.get(status="draft")
        self.assertEqual(po.lines.get().quantity, 7)

    def test_start_blocked_when_short_unless_forced(self):
        b = BuildOrder.objects.create(revision=self.rev, quantity=10, assembly="house")
        c = signed_in(self.buyer)
        c.post(reverse("production:build_action", args=[b.pk]), {"action": "start"})
        b.refresh_from_db()
        self.assertEqual(b.status, "planned")
        c.post(reverse("production:build_action", args=[b.pk]), {"action": "start", "force": "1"})
        b.refresh_from_db()
        self.r.refresh_from_db()
        self.assertEqual(b.status, "in_progress")
        self.assertEqual(self.r.stock, 60)

    def test_complete_records_yield_and_consumes_once(self):
        b = BuildOrder.objects.create(revision=self.rev, quantity=2, assembly="house")
        c = signed_in(self.buyer)
        c.post(reverse("production:build_action", args=[b.pk]), {"action": "start"})
        c.post(reverse("production:build_action", args=[b.pk]), {"action": "complete", "passed": 2, "failed": 0})
        b.refresh_from_db()
        self.u.refresh_from_db()
        self.assertEqual((b.status, b.yield_percent, self.u.stock), ("completed", 100, 1))

    def test_engineers_cannot_place_orders(self):
        self.assertEqual(signed_in(self.eng).get(reverse("production:po_create")).status_code, 403)
