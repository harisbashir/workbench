from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.firmware import sets
from apps.firmware.tests.helpers import make_fixture, release
from apps.production.models import BuildFirmware, BuildOrder


class BuildFirmwareTests(TestCase):
    def setUp(self):
        self.f = make_fixture()
        self.buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.f.p.members.add(self.buyer)
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: self.f.boot100.pk, self.f.app_slot.pk: self.f.app100.pk}, self.f.eng)
        self.client = signed_in(self.buyer)

    def create_build(self):
        self.client.post(reverse("production:build_create"), {
            "revision": self.f.rev.pk, "quantity": 5, "assembly": "house", "notes": ""})
        return BuildOrder.objects.get()

    def act(self, b, **data):
        return self.client.post(reverse("production:build_action", args=[b.pk]), data)

    def test_new_build_copies_revision_set(self):
        b = self.create_build()
        rows = list(b.firmware_rows())
        self.assertEqual([(r.controller_label, r.release) for r in rows],
                         [("U1 Main MCU", self.f.boot100), ("U1 Main MCU", self.f.app100)])

    def test_build_snapshot_independent_of_revision(self):
        b = self.create_build()
        newer = release(self.f.app, "1.0.1", revisions=[self.f.rev])
        sets.save_set(self.f.rev, {self.f.app_slot.pk: newer.pk}, self.f.eng)
        self.assertIn(self.f.app100, [r.release for r in b.firmware_rows()])
        self.assertNotIn(newer, [r.release for r in b.firmware_rows()])

    def test_reset_from_revision(self):
        b = self.create_build()
        newer = release(self.f.app, "1.0.1", revisions=[self.f.rev])
        sets.save_set(self.f.rev, {self.f.app_slot.pk: newer.pk}, self.f.eng)
        self.act(b, action="reset_firmware")
        self.assertIn(newer, [r.release for r in b.firmware_rows()])

    def test_build_snapshot_survives_controller_delete(self):
        b = self.create_build()
        self.f.controller.delete()
        rows = list(b.firmware_rows())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.slot is None and r.controller_label == "U1 Main MCU" for r in rows))

    def test_start_blocked_by_recalled_entry(self):
        b = self.create_build()
        self.f.app100.status = "recalled"
        self.f.app100.save()
        self.assertTrue(b.has_recalled_firmware())
        self.act(b, action="start")
        b.refresh_from_db()
        self.assertEqual(b.status, "planned")

    def test_change_row_version(self):
        b = self.create_build()
        row = b.firmware_rows().get(release=self.f.app100)
        self.act(b, action="set_firmware", **{f"row_{row.pk}": self.f.app110.pk})
        row.refresh_from_db()
        self.assertEqual(row.release, self.f.app110)

    def test_set_build_firmware_version_must_match_firmware(self):
        b = self.create_build()
        row = b.firmware_rows().get(release=self.f.app100)
        self.act(b, action="set_firmware", **{f"row_{row.pk}": self.f.boot100.pk})
        row.refresh_from_db()
        self.assertEqual(row.release, self.f.app100)

    def test_set_build_firmware_refuses_other_project(self):
        b = self.create_build()
        self.act(b, action="set_firmware", add=self.f.other_rel.pk)
        self.assertEqual(b.firmware_rows().count(), 2)

    def test_add_and_remove_rows(self):
        b = self.create_build()
        extra_fw = self.f.p.firmwares.create(name="Radio")
        radio = release(extra_fw, "0.9.1")
        self.act(b, action="set_firmware", add=radio.pk)
        added = b.firmware_rows().get(release=radio)
        self.assertEqual(added.controller_label, "")
        self.act(b, action="set_firmware", **{f"remove_{added.pk}": "1"})
        self.assertFalse(BuildFirmware.objects.filter(pk=added.pk).exists())

    def test_build_page_lists_firmware(self):
        b = self.create_build()
        page = self.client.get(b.get_absolute_url()).content.decode()
        self.assertIn("U1 Main MCU", page)
        self.assertIn("Bootloader", page)
