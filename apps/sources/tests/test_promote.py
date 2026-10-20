import io
import zipfile

from django.test import TestCase
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.testing import signed_in
from apps.firmware.models import Firmware, FirmwareRelease
from apps.projects.models import Revision, default_board

from .helpers import commit, make_repo


class PromoteTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        self.fw = Firmware.objects.create(project=self.f.p, name="Main application")
        self.f.repo.firmware = self.fw
        self.f.repo.save()
        commit(self.f.repo, self.f.eng, message="ADC fix", **{"src/main.c": "int main;\n"})
        self.url = reverse("sources:promote", args=["PWR", self.f.repo.slug, 1])

    def test_promote_firmware_creates_draft_release_with_source_zip(self):
        r = signed_in(self.f.eng).post(self.url, {"version": "1.2.0"})
        rel = FirmwareRelease.objects.get(firmware=self.fw)
        self.assertRedirects(r, rel.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual((rel.version, rel.status, rel.git_ref, rel.notes), ("1.2.0", "draft", "sources v1", "ADC fix"))
        art = rel.artifacts.get()
        self.assertEqual(art.kind, "other")
        with art.file.open("rb") as fh:
            self.assertIn("src/main.c", zipfile.ZipFile(io.BytesIO(fh.read())).namelist())
        self.assertTrue(AuditLog.objects.filter(action="sources.promote").exists())

    def test_promote_firmware_version_checks(self):
        c = signed_in(self.f.eng)
        c.post(self.url, {"version": "1.2"})
        self.assertFalse(FirmwareRelease.objects.exists())
        c.post(self.url, {"version": ""})
        self.assertEqual(FirmwareRelease.objects.get().version, "0.1.0")
        c.post(self.url, {"version": "0.1.0"})
        self.assertEqual(FirmwareRelease.objects.count(), 1)

    def test_promote_kicad_links_revision(self):
        repo = self.f.repo
        repo.kind, repo.firmware = "kicad", None
        repo.save()
        rev = Revision.objects.create(project=self.f.p, board=self.f.board, name="Rev C")
        r = signed_in(self.f.eng).post(self.url, {"revision": rev.pk})
        self.assertRedirects(r, rev.get_absolute_url(), fetch_redirect_response=False)
        rev.refresh_from_db()
        self.assertEqual(rev.source_version.number, 1)
        page = signed_in(self.f.eng).get(rev.get_absolute_url()).content.decode()
        self.assertIn("Built from", page)
        self.assertIn(rev.source_version.get_absolute_url().replace("&", "&amp;"), page)

    def test_promote_revision_other_board_refused(self):
        self.f.repo.kind, self.f.repo.firmware = "kicad", None
        self.f.repo.save()
        other_rev = Revision.objects.create(project=self.f.other, board=default_board(self.f.other), name="Rev X")
        r = signed_in(self.f.eng).post(self.url, {"revision": other_rev.pk})
        self.assertEqual(r.status_code, 404)
        other_rev.refresh_from_db()
        self.assertIsNone(other_rev.source_version)

    def test_promote_needs_writer(self):
        self.assertEqual(signed_in(self.f.viewer).post(self.url, {"version": "1.0.0"}).status_code, 403)
        self.assertFalse(FirmwareRelease.objects.exists())

    def test_repo_page_offers_promotion(self):
        page = signed_in(self.f.eng).get(self.f.repo.get_absolute_url()).content.decode()
        self.assertIn(self.url, page)
        self.assertNotIn(self.url, signed_in(self.f.viewer).get(self.f.repo.get_absolute_url()).content.decode())
