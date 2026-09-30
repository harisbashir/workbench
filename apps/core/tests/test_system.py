import json
import zipfile
from datetime import datetime, timezone as dt_tz
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.mail import get_connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core import email, system
from apps.core.management.commands.run_scheduler import tick
from apps.core.middleware import FirstRunSetupMiddleware
from apps.core.models import Notification, SiteSettings
from apps.core.testing import make_user, signed_in
from apps.files.models import Document
from apps.projects.models import Project


class SetupWizardTests(TestCase):
    def setUp(self):
        FirstRunSetupMiddleware._done = False

    def tearDown(self):
        FirstRunSetupMiddleware._done = False

    def test_everything_redirects_to_setup_until_done(self):
        r = self.client.get("/projects/")
        self.assertRedirects(r, reverse("core:setup"), fetch_redirect_response=False)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def data(self, **kw):
        d = {"setup_code": settings.SETUP_TOKEN, "company_name": "Acme Embedded", "site_url": "https://wb.example.com",
             "first_name": "Haris", "username": "haris", "email": "h@example.com", "time_zone": "America/Toronto",
             "password1": "A-very-good-passphrase-7", "password2": "A-very-good-passphrase-7"}
        d.update(kw)
        return d

    def test_wrong_code_rejected(self):
        r = self.client.post(reverse("core:setup"), self.data(setup_code="WRONG"))
        self.assertContains(r, "setup code isn")
        self.assertFalse(User.objects.exists())

    def test_setup_creates_admin_and_requires_2fa(self):
        r = self.client.post(reverse("core:setup"), self.data())
        self.assertRedirects(r, reverse("accounts:mfa_setup"), fetch_redirect_response=False)
        u = User.objects.get()
        self.assertTrue(u.is_admin_role and u.is_superuser)
        site = SiteSettings.load()
        self.assertEqual((site.company_name, site.site_url), ("Acme Embedded", "https://wb.example.com"))
        self.assertRedirects(self.client.get("/"), reverse("accounts:mfa_setup"), fetch_redirect_response=False)
        # the setup page can't be used again
        self.assertRedirects(Client().get(reverse("core:setup")), "/", fetch_redirect_response=False)


class SystemPageTests(TestCase):
    def test_admin_only(self):
        self.assertEqual(signed_in(make_user("eng")).get(reverse("core:system")).status_code, 403)
        c = signed_in(make_user("boss", role=User.Role.ADMIN))
        for tab in ("", "general/", "backups/", "email/", "github/"):
            self.assertEqual(c.get(f"/system/{tab}").status_code, 200, tab)

    def test_github_secret_generated_and_rotatable(self):
        site = SiteSettings.load()
        old = site.github_secret
        self.assertEqual(len(old), 64)
        self.assertNotIn(old, site._github_secret)  # encrypted at rest
        signed_in(make_user("boss", role=User.Role.ADMIN)).post(reverse("core:system_action"), {"action": "rotate_github_secret"})
        self.assertNotEqual(SiteSettings.load().github_secret, old)

    def test_invite_links_use_configured_address(self):
        site = SiteSettings.load()
        site.site_url = "https://wb.example.com"
        site.save()
        c = signed_in(make_user("boss", role=User.Role.ADMIN))
        eng = make_user("eng")
        r = c.get(reverse("accounts:user_invite", args=[eng.pk]), HTTP_HOST="evil.example.net")
        self.assertContains(r, "https://wb.example.com/accounts/set-password/")
        self.assertNotContains(r, "evil.example.net")


class BackupTests(TransactionTestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMIN)
        p = Project.objects.create(key="PWR", name="Power")
        p.members.add(self.admin)
        signed_in(self.admin).post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile("notes.txt", b"hello backup")]})

    def test_backup_contains_everything(self):
        path = system.create_backup("test")
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            manifest = json.loads(z.read("manifest.json"))
        self.assertIn("secrets.json", names)
        self.assertTrue(any(n.startswith("db/") for n in names))
        self.assertTrue(any(n.endswith("notes.txt") for n in names))
        self.assertGreaterEqual(manifest["files"], 1)
        self.assertIsNotNone(SiteSettings.load().last_backup_at)

    def test_ui_backup_download_and_prune(self):
        c = signed_in(self.admin)
        c.post(reverse("core:system_action"), {"action": "backup_now"})
        name = system.list_backups()[0]["name"]
        r = c.post(reverse("core:system_action"), {"action": "download_backup", "name": name})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post(reverse("core:system_action"), {"action": "download_backup", "name": "../secrets.json"}).status_code, 404)
        self.assertEqual(signed_in(make_user("eng")).post(reverse("core:system_action"), {"action": "backup_now"}).status_code, 403)

    def test_scheduler_runs_nightly_backup_once(self):
        site = SiteSettings.load()
        site.backup_hour, site.time_zone = 2, "UTC"
        site.save()
        # Today's date: the backup records the real time it ran.
        at_two = datetime.now(dt_tz.utc).replace(hour=2, minute=5)
        self.assertIn("backup", tick(at_two))
        self.assertNotIn("backup", tick(at_two))  # only once per day
        self.assertNotIn("backup", tick(at_two.replace(hour=14)))

    def test_trash_purged_after_retention(self):
        doc = Document.objects.get()
        doc.trash(self.admin)
        Document.objects.filter(pk=doc.pk).update(deleted_at=datetime(2020, 1, 1, tzinfo=dt_tz.utc))
        self.assertEqual(system.purge_trash(30), 1)
        self.assertFalse(Document.objects.exists())


class RestoreTests(TransactionTestCase):
    """Round trip: back up, change data, restore, and the old data is back."""

    def test_restore_round_trip(self):
        files_dir = Path(settings.FILES_DIR)
        (files_dir / "shared").mkdir(parents=True, exist_ok=True)
        (files_dir / "shared" / "keep.txt").write_text("original")
        full = system.create_backup("test")
        # The test database lives in memory, so restore a copy without the database part.
        files_only = Path(settings.BACKUP_DIR) / "workbench-files-only.zip"
        with zipfile.ZipFile(full) as src, zipfile.ZipFile(files_only, "w") as dst:
            for item in src.infolist():
                if not item.filename.startswith("db/"):
                    dst.writestr(item, src.read(item.filename))
        (files_dir / "shared" / "keep.txt").write_text("changed")
        (files_dir / "shared" / "new.txt").write_text("added after backup")
        manifest, safety = system.restore_backup(files_only, files_only=True)
        self.assertEqual(manifest["reason"], "test")
        self.assertEqual((files_dir / "shared" / "keep.txt").read_text(), "original")
        self.assertFalse((files_dir / "shared" / "new.txt").exists())
        self.assertEqual((safety / "files" / "shared" / "new.txt").read_text(), "added after backup")

    def test_rejects_path_traversal(self):
        bad = Path(settings.BACKUP_DIR) / "workbench-evil.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("manifest.json", "{}")
            z.writestr("files/../../etc/passwd", "x")
        with self.assertRaises(ValueError):
            system.restore_backup(bad)

    def test_rejects_non_backups(self):
        bad = Path(settings.BACKUP_DIR) / "workbench-bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(ValueError):
            system.restore_backup(bad)


class HealthTests(TestCase):
    def test_healthz(self):
        make_user("x")
        (Path(settings.DATA_DIR) / "scheduler.heartbeat").touch()
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["database"])


class EmailTests(TestCase):
    def setUp(self):
        site = SiteSettings.load()
        site.email_enabled, site.smtp_host, site.email_from = True, "smtp.example.com", "wb@example.com"
        site.smtp_password = "app-password"
        site.save()

    def test_password_encrypted(self):
        site = SiteSettings.load()
        self.assertEqual(site.smtp_password, "app-password")
        self.assertNotIn("app-password", site._smtp_password)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_notifications_emailed_once_per_person(self):
        u = make_user("eng", email="eng@example.com")
        quiet = make_user("quiet", email="q@example.com", email_notifications=False)
        for text in ("PWR-1 assigned", "PWR-2 assigned"):
            Notification.objects.create(user=u, text=text, url="/projects/PWR/tasks/1/")
        Notification.objects.create(user=quiet, text="hush")
        with mock.patch.object(email, "connection_for", lambda site: get_connection("django.core.mail.backends.locmem.EmailBackend")):
            self.assertEqual(email.send_pending_notifications(), 1)
            self.assertEqual(email.send_pending_notifications(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 new notifications", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["eng@example.com"])
