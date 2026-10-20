from django.test import TestCase
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.testing import signed_in
from apps.firmware.models import FirmwareSetEntry
from apps.projects.models import Revision

from .helpers import make_fixture


class SetViewTests(TestCase):
    def setUp(self):
        self.f = make_fixture()
        self.url = reverse("firmware:revision_set", args=["PWR", self.f.rev.pk])
        self.lock_url = reverse("firmware:revision_set_lock", args=["PWR", self.f.rev.pk])

    def data(self, boot, app):
        return {f"slot_{self.f.boot_slot.pk}": boot.pk if boot else "", f"slot_{self.f.app_slot.pk}": app.pk if app else ""}

    def test_member_saves_set(self):
        r = signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100))
        self.assertRedirects(r, self.f.rev.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(FirmwareSetEntry.objects.filter(revision=self.f.rev).count(), 2)
        self.assertTrue(AuditLog.objects.filter(action="revision.firmware_set").exists())

    def test_viewer_cannot_save(self):
        r = signed_in(self.f.outsider).post(self.url, self.data(self.f.boot100, self.f.app100))
        self.assertIn(r.status_code, (403, 404))
        self.assertFalse(FirmwareSetEntry.objects.exists())

    def test_revision_from_other_project_404(self):
        other_rev = Revision.objects.create(project=self.f.other, name="Rev X")
        r = signed_in(self.f.eng).post(reverse("firmware:revision_set", args=["PWR", other_rev.pk]), {})
        self.assertEqual(r.status_code, 404)

    def test_engineer_cannot_lock(self):
        self.assertEqual(signed_in(self.f.eng).post(self.lock_url, {"action": "lock"}).status_code, 403)
        signed_in(self.f.lead).post(self.lock_url, {"action": "lock"})
        self.f.rev.refresh_from_db()
        self.assertTrue(self.f.rev.firmware_set_locked)
        self.assertTrue(AuditLog.objects.filter(action="revision.firmware_set_lock").exists())
        signed_in(self.f.lead).post(self.lock_url, {"action": "unlock"})
        self.f.rev.refresh_from_db()
        self.assertFalse(self.f.rev.firmware_set_locked)

    def test_page_shows_rows_and_recall(self):
        signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100))
        self.f.app100.status = "recalled"
        self.f.app100.save()
        page = signed_in(self.f.eng).get(self.f.rev.get_absolute_url()).content.decode()
        self.assertIn("U1 Main MCU", page)
        self.assertIn("Recalled — do not flash", page)
        self.assertIn(self.url, page)

    def test_viewer_sees_set_without_form(self):
        self.f.outsider.role = "viewer"
        self.f.outsider.save()
        self.f.p.members.add(self.f.outsider)
        page = signed_in(self.f.outsider).get(self.f.rev.get_absolute_url()).content.decode()
        self.assertIn("U1 Main MCU", page)
        self.assertNotIn(self.url, page)

    def test_firmware_page_shows_used_on(self):
        signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100))
        page = signed_in(self.f.eng).get(self.f.app.get_absolute_url()).content.decode()
        self.assertIn("U1 Main MCU", page)
        self.assertIn(self.f.rev.get_absolute_url(), page)
        rel_page = signed_in(self.f.eng).get(self.f.app100.get_absolute_url()).content.decode()
        self.assertIn(self.f.rev.get_absolute_url(), rel_page)

    def test_saving_with_untouched_deprecated_pin_is_not_an_error(self):
        signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100))
        self.f.app100.status = "deprecated"
        self.f.app100.save()
        self.f.rev.status = "released"
        self.f.rev.save()
        r = signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100), follow=True)
        self.assertNotContains(r, "be used on")
        self.assertEqual(FirmwareSetEntry.objects.get(slot=self.f.app_slot).release, self.f.app100)

    def test_non_numeric_value_leaves_slot_alone(self):
        signed_in(self.f.eng).post(self.url, self.data(self.f.boot100, self.f.app100))
        signed_in(self.f.eng).post(self.url, {f"slot_{self.f.app_slot.pk}": "abc"})
        self.assertTrue(FirmwareSetEntry.objects.filter(slot=self.f.app_slot).exists())
