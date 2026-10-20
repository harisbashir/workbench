"""Pre-releases (1.2.0-rc.1, 2.0.0-beta…) are for testing only and can't be released for production."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.core.testing import signed_in
from apps.firmware.models import FirmwareArtifact

from .helpers import make_fixture, release


class PreReleaseTests(TestCase):
    def setUp(self):
        self.f = make_fixture()
        self.rc = release(self.f.app, "1.2.0-rc.1", status="testing", revisions=[self.f.rev])
        FirmwareArtifact.store(self.rc, SimpleUploadedFile("app.bin", b"app"), self.f.eng)
        self.url = reverse("firmware:release_status", args=["PWR", self.f.app.slug, self.rc.version])
        self.page = reverse("firmware:release", args=["PWR", self.f.app.slug, self.rc.version])

    def test_pre_release_cannot_be_released(self):
        r = signed_in(self.f.lead).post(self.url, {"action": "released"}, follow=True)
        self.rc.refresh_from_db()
        self.assertEqual(self.rc.status, "testing")
        self.assertContains(r, "1.2.0")  # names the final version to make instead

    def test_release_button_not_offered_for_pre_release(self):
        page = signed_in(self.f.lead).get(self.page).content.decode()
        self.assertNotIn('value="released"', page)
        self.assertIn("pre-release", page)

    def test_pre_release_can_still_be_tested_and_recalled(self):
        c = signed_in(self.f.lead)
        c.post(self.url, {"action": "recalled", "note": "bad"})
        self.rc.refresh_from_db()
        self.assertEqual(self.rc.status, "recalled")

    def test_final_version_still_releases(self):
        final = release(self.f.app, "1.2.0", status="testing", revisions=[self.f.rev])
        FirmwareArtifact.store(final, SimpleUploadedFile("app.bin", b"app2"), self.f.eng)
        signed_in(self.f.lead).post(reverse("firmware:release_status", args=["PWR", self.f.app.slug, "1.2.0"]), {"action": "released"})
        final.refresh_from_db()
        self.assertEqual(final.status, "released")
