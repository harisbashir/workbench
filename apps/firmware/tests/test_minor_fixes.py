"""Tests for the smaller problems left over from the 1.5.0 review."""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.firmware import sets
from apps.firmware.models import ControllerSlot, Firmware
from apps.production.models import BuildOrder
from apps.projects.models import Controller

from .helpers import make_fixture, release


def count_queries(fn):
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


class SetQueryTests(TestCase):
    def setUp(self):
        self.f = make_fixture()
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: self.f.boot100.pk, self.f.app_slot.pk: self.f.app100.pk}, self.f.eng)

    def add_controllers(self, n):
        for i in range(n):
            c = Controller.objects.create(board=self.f.board, designator=f"U{10 + i}", name=f"Radio {i}")
            fw = Firmware.objects.create(project=self.f.p, name=f"Radio fw {i}")
            rel = release(fw, "1.0.0", revisions=[self.f.rev])
            release(fw, "1.1.0", status="testing", revisions=[self.f.rev])
            slot = ControllerSlot.objects.create(controller=c, firmware=fw, flash_order=1)
            sets.save_set(self.f.rev, {slot.pk: rel.pk}, self.f.eng)

    def test_set_rows_queries_do_not_grow_with_slots(self):
        few = count_queries(lambda: sets.set_rows(self.f.rev))
        self.add_controllers(4)
        rows = []
        many = count_queries(lambda: rows.extend(sets.set_rows(self.f.rev)))
        self.assertEqual(len(rows), 6)
        self.assertEqual(many, few)

    def test_set_rows_results_unchanged(self):
        newer = release(self.f.app, "1.2.0", revisions=[self.f.rev])
        rows = {r["slot"].pk: r for r in sets.set_rows(self.f.rev)}
        app = rows[self.f.app_slot.pk]
        self.assertEqual(app["recommended"], newer)
        self.assertEqual([r.version for r in app["choices"]], ["1.2.0", "1.1.0", "1.0.0"])
        self.assertIn(("warn", "Newer release 1.2.0 is available"), app["issues"])
        self.assertEqual(rows[self.f.boot_slot.pk]["issues"], [])
        incompatible = release(self.f.boot, "1.0.1")
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: incompatible.pk}, self.f.eng)
        boot = {r["slot"].pk: r for r in sets.set_rows(self.f.rev)}[self.f.boot_slot.pk]
        self.assertIn(("warn", "Not marked compatible with Rev A"), boot["issues"])

    def test_build_page_queries_do_not_grow_with_rows(self):
        buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.f.p.members.add(buyer)
        c = signed_in(buyer)
        b = BuildOrder.objects.create(revision=self.f.rev, quantity=2)
        b.copy_firmware_from_revision()
        url = reverse("production:build_detail", args=[b.pk])
        c.get(url)  # the first request also does one-off session work
        few = count_queries(lambda: c.get(url))
        self.add_controllers(4)
        b.copy_firmware_from_revision()
        many = count_queries(lambda: c.get(url))
        self.assertEqual(many, few)


class BuildFirmwareStatusTests(TestCase):
    """The build's status is checked again once it's locked, so a build finished meanwhile isn't changed."""
    def setUp(self):
        self.f = make_fixture()
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: self.f.boot100.pk, self.f.app_slot.pk: self.f.app100.pk}, self.f.eng)
        self.b = BuildOrder.objects.create(revision=self.f.rev, quantity=2)
        self.b.copy_firmware_from_revision()

    def finish_elsewhere(self):
        BuildOrder.objects.filter(pk=self.b.pk).update(status=BuildOrder.Status.COMPLETED)

    def test_reset_skipped_when_build_finished(self):
        self.finish_elsewhere()
        sets.save_set(self.f.rev, {self.f.app_slot.pk: None}, self.f.eng)
        self.assertIsNone(self.b.copy_firmware_from_revision())
        self.assertEqual(self.b.build_firmware.count(), 2)

    def test_set_firmware_skipped_when_build_finished(self):
        from django.test import RequestFactory

        from apps.production.views import _set_build_firmware
        row = self.b.build_firmware.get(release=self.f.app100)
        request = RequestFactory().post("/", {f"remove_{row.pk}": "1"})
        request.user = self.f.lead
        self.finish_elsewhere()
        self.assertIsNone(_set_build_firmware(request, self.b))
        self.assertEqual(self.b.build_firmware.count(), 2)
