import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.core.models import AuditLog, Notification
from apps.core.testing import signed_in
from apps.sources import checkin
from apps.sources.models import Draft, Lock

from .helpers import commit, make_repo


def up(name, data):
    return SimpleUploadedFile(name, data.encode() if isinstance(data, str) else data)


class CheckinViewTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        commit(self.f.repo, self.f.eng, message="Start", **{"src/main.c": "int a;\n", "board.kicad_sch": "(kicad_sch)\n"})
        self.c = signed_in(self.f.eng)
        self.url = reverse("sources:checkin", args=["PWR", self.f.repo.slug])

    def stage(self, client=None, files=(), paths=None, **extra):
        data = {"files": list(files), "mode": "update", "base": 1, "into": "", **extra}
        if paths is not None:
            data["paths"] = paths
        r = (client or self.c).post(self.url, data)
        return r, Draft.objects.order_by("-pk").first()

    def confirm(self, draft, client=None, **extra):
        url = reverse("sources:checkin_preview", args=["PWR", self.f.repo.slug, draft.pk])
        return (client or self.c).post(url, {"message": "Fix the ADC", "release_locks": "1", **extra})

    def test_checkin_flow_creates_version(self):
        self.assertEqual(self.c.get(self.url).status_code, 200)
        r, draft = self.stage(files=[up("main.c", "int b;\n"), up("adc.c", "void adc(void);\n")], paths=["src/main.c", "src/adc.c"])
        preview = reverse("sources:checkin_preview", args=["PWR", self.f.repo.slug, draft.pk])
        self.assertRedirects(r, preview, fetch_redirect_response=False)
        page = self.c.get(preview).content.decode()
        self.assertIn("src/adc.c", page)
        self.assertIn("src/main.c", page)
        r = self.confirm(draft)
        self.assertRedirects(r, self.f.repo.get_absolute_url(), fetch_redirect_response=False)
        self.f.repo.refresh_from_db()
        self.assertEqual(self.f.repo.latest_number, 2)
        self.assertFalse(Draft.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="sources.checkin").exists())

    def test_fetch_upload_returns_redirect_json(self):
        r = self.c.post(self.url, {"files": [up("x.c", "1")], "paths": ["lib/x.c"], "mode": "update", "base": 1},
                        headers={"x-requested-with": "fetch"})
        self.assertIn("redirect", r.json())
        self.assertIn("lib/x.c", Draft.objects.get().files)

    def test_zip_checkin(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("src/main.c", "int zip;\n")
        _, draft = self.stage(files=[up("code.zip", buf.getvalue())])
        self.assertEqual(list(draft.files), ["src/main.c"])

    def test_message_required(self):
        _, draft = self.stage(files=[up("main.c", "int b;\n")], paths=["src/main.c"])
        r = self.confirm(draft, message="")
        self.assertEqual(r.status_code, 200)
        self.f.repo.refresh_from_db()
        self.assertEqual(self.f.repo.latest_number, 1)

    def test_draft_belongs_to_author(self):
        _, draft = self.stage(files=[up("main.c", "int b;\n")], paths=["src/main.c"])
        other = signed_in(self.f.eng2)
        url = reverse("sources:checkin_preview", args=["PWR", self.f.repo.slug, draft.pk])
        self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(self.confirm(draft, client=other).status_code, 404)

    def test_confirm_replans_against_latest(self):
        _, draft = self.stage(files=[up("main.c", "int mine;\n")], paths=["src/main.c"])
        commit(self.f.repo, self.f.eng2, base=1, **{"src/main.c": "int theirs;\n"})
        r = self.confirm(draft)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "the version you started from")
        self.f.repo.refresh_from_db()
        self.assertEqual(self.f.repo.latest_number, 2)

    def test_partial_checkin(self):
        _, draft = self.stage(files=[up("main.c", "int mine;\n"), up("new.c", "x\n")], paths=["src/main.c", "src/new.c"])
        commit(self.f.repo, self.f.eng2, base=1, **{"src/main.c": "int theirs;\n"})
        self.confirm(draft, allow_partial="1")
        self.f.repo.refresh_from_db()
        self.assertEqual(self.f.repo.latest_number, 3)
        self.assertIn("src/new.c", checkin.tree(self.f.repo.latest()))

    def test_lock_and_unlock_views(self):
        url = reverse("sources:lock", args=["PWR", self.f.repo.slug])
        self.c.post(url, {"path": "board.kicad_sch", "action": "lock"})
        self.assertTrue(Lock.objects.filter(user=self.f.eng).exists())
        r = signed_in(self.f.eng2).post(url, {"path": "board.kicad_sch", "action": "lock"}, follow=True)
        self.assertContains(r, "Already checked out")
        signed_in(self.f.eng2).post(url, {"path": "board.kicad_sch", "action": "unlock"})
        self.assertTrue(Lock.objects.exists())
        self.c.post(url, {"path": "board.kicad_sch", "action": "unlock"})
        self.assertFalse(Lock.objects.exists())

    def test_break_lock_needs_manager(self):
        url = reverse("sources:lock", args=["PWR", self.f.repo.slug])
        self.c.post(url, {"path": "board.kicad_sch", "action": "lock"})
        self.assertEqual(signed_in(self.f.eng2).post(url, {"path": "board.kicad_sch", "action": "break"}).status_code, 403)
        signed_in(self.f.lead).post(url, {"path": "board.kicad_sch", "action": "break"})
        self.assertFalse(Lock.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="sources.lock_broken").exists())
        self.assertTrue(Notification.objects.filter(user=self.f.eng).exists())

    def test_repo_page_shows_checkin_and_lock_buttons(self):
        page = self.c.get(self.f.repo.get_absolute_url()).content.decode()
        self.assertIn(self.url, page)
        self.assertIn(reverse("sources:lock", args=["PWR", self.f.repo.slug]), page)
        page = signed_in(self.f.viewer).get(self.f.repo.get_absolute_url()).content.decode()
        self.assertNotIn(self.url, page)

    def test_viewer_cannot_checkin(self):
        viewer = signed_in(self.f.viewer)
        self.assertEqual(viewer.get(self.url).status_code, 403)
        r, _ = self.stage(client=viewer, files=[up("x.c", "1")])
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Draft.objects.exists())
        url = reverse("sources:lock", args=["PWR", self.f.repo.slug])
        self.assertEqual(viewer.post(url, {"path": "board.kicad_sch", "action": "lock"}).status_code, 403)
