"""Security and reliability fixes from the pre-launch review (accounts, core, scheduler)."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path
from unittest import mock

import pyotp
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.forms import UserAdminForm
from apps.accounts.models import User
from apps.core import csvsafe, export, system
from apps.core.forms import StorageLocationForm
from apps.core.management.commands.run_scheduler import tick
from apps.core.models import ExportJob, SiteSettings
from apps.core.templatetags.workbench import money
from apps.core.testing import make_user, signed_in

PASSWORD = "Correct-horse-battery-9"


class TwoFactorHardeningTests(TestCase):
    def test_empty_secret_never_accepts_a_code(self):
        u = make_user("eve")
        User.objects.filter(pk=u.pk).update(_mfa_secret="not-a-valid-token")  # key changed / corrupted
        u.refresh_from_db()
        self.assertEqual(u.mfa_secret, "")
        self.assertFalse(u.verify_totp(pyotp.TOTP("").now()))
        self.assertFalse(u.verify_totp("000000"))

    def test_code_can_only_be_used_once_even_concurrently(self):
        u = make_user("amy")
        code = pyotp.TOTP(u.mfa_secret).now()
        other = User.objects.get(pk=u.pk)  # a second request holding its own copy
        self.assertTrue(u.verify_totp(code))
        self.assertFalse(other.verify_totp(code))

    def _password_step(self, username):
        c = self.client_class()
        c.post(reverse("accounts:login"), {"username": username, "password": PASSWORD})
        return c

    def test_wrong_codes_lock_the_account(self):
        u = make_user("bob")
        c = self._password_step("bob")
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            r = c.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertRedirects(r, reverse("accounts:login"), fetch_redirect_response=False)
        u.refresh_from_db()
        self.assertTrue(u.is_locked)
        # A fresh password sign-in is refused while locked, and the message doesn't reveal it.
        r = self.client.post(reverse("accounts:login"), {"username": "bob", "password": PASSWORD})
        self.assertContains(r, "Wrong username or password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_password_does_not_reset_2fa_failures(self):
        u = make_user("cat")
        for _ in range(3):
            self._password_step("cat").post(reverse("accounts:mfa_verify"), {"code": "000000"})
        u.refresh_from_db()
        self.assertEqual(u.mfa_failures, 3)

    def test_regenerating_recovery_codes_needs_password(self):
        u = make_user("dan")
        c = signed_in(u)
        r = c.post(reverse("accounts:regenerate_recovery_codes"), {"password": "wrong"})
        self.assertRedirects(r, reverse("accounts:security"), fetch_redirect_response=False)
        self.assertFalse(u.recovery_codes.exists())
        c.post(reverse("accounts:regenerate_recovery_codes"), {"password": PASSWORD})
        self.assertTrue(u.recovery_codes.exists())

    def test_admin_2fa_reset_signs_the_person_out(self):
        admin = make_user("boss", role=User.Role.ADMIN)
        victim = make_user("lost")
        signed_in(victim)
        self.assertEqual(Session.objects.count(), 1)
        signed_in(admin).post(reverse("accounts:user_reset_mfa", args=[victim.pk]))
        remaining = [s.get_decoded().get("_auth_user_id") for s in Session.objects.all()]
        self.assertNotIn(str(victim.pk), remaining)


class LoginLockoutTests(TestCase):
    def test_locked_and_wrong_password_look_the_same(self):
        make_user("real")
        wrong = self.client.post(reverse("accounts:login"), {"username": "nobody", "password": "x"})
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            self.client.post(reverse("accounts:login"), {"username": "real", "password": "x"})
        locked = self.client.post(reverse("accounts:login"), {"username": "real", "password": PASSWORD})
        self.assertEqual(wrong.context["form"].non_field_errors(), locked.context["form"].non_field_errors())
        self.assertTrue(User.objects.get(username="real").is_locked)

    def test_old_failures_expire(self):
        u = make_user("slow")
        User.objects.filter(pk=u.pk).update(failed_logins=settings.LOGIN_MAX_ATTEMPTS - 1,
                                             last_failed_login=timezone.now() - timedelta(days=30))
        self.client.post(reverse("accounts:login"), {"username": "slow", "password": "typo"})
        u.refresh_from_db()
        self.assertFalse(u.is_locked)
        self.assertEqual(u.failed_logins, 1)

    def test_many_failures_from_one_address_are_throttled(self):
        from django.core.cache import cache
        cache.clear()
        make_user("target")
        for i in range(31):
            r = self.client.post(reverse("accounts:login"), {"username": f"guess{i}", "password": "x"})
        self.assertContains(r, "Too many failed sign-ins from this network")
        cache.clear()


class UserAdminRulesTests(TestCase):
    def test_usernames_are_unique_ignoring_case(self):
        make_user("Ali")
        f = UserAdminForm(data={"username": "ali", "role": "engineer", "time_zone": "UTC", "is_active": True})
        self.assertFalse(f.is_valid())
        self.assertIn("username", f.errors)

    def test_last_admin_cannot_be_demoted_or_deactivated(self):
        admin = make_user("only", role=User.Role.ADMIN)
        f = UserAdminForm(instance=admin, editor=admin, data={
            "username": "only", "role": "engineer", "time_zone": "UTC", "is_active": True})
        self.assertFalse(f.is_valid())
        f = UserAdminForm(instance=admin, editor=admin, data={
            "username": "only", "role": "admin", "time_zone": "UTC", "is_active": False})
        self.assertFalse(f.is_valid())

    def test_admins_cannot_demote_themselves(self):
        me = make_user("me", role=User.Role.ADMIN)
        make_user("other", role=User.Role.ADMIN)
        f = UserAdminForm(instance=me, editor=me, data={"username": "me", "role": "lead", "time_zone": "UTC", "is_active": True})
        self.assertFalse(f.is_valid())

    def test_new_people_get_the_company_time_zone(self):
        site = SiteSettings.load()
        site.time_zone = "America/Toronto"
        site.save()
        page = signed_in(make_user("boss", role=User.Role.ADMIN)).get(reverse("accounts:user_create"))
        self.assertContains(page, '<option value="America/Toronto" selected>')

    def test_invite_email_reports_when_mail_is_not_set_up(self):
        admin = make_user("boss", role=User.Role.ADMIN)
        person = make_user("new", email="new@example.com")
        r = signed_in(admin).post(reverse("accounts:user_invite", args=[person.pk]), {"action": "email"}, follow=True)
        self.assertContains(r, "nothing was sent")

    def test_reset_account_can_reactivate_and_make_admin(self):
        u = make_user("gone")
        User.objects.filter(pk=u.pk).update(is_active=False)
        with self.assertRaises(CommandError):
            call_command("reset_account", "gone", stdout=io.StringIO())
        call_command("reset_account", "gone", "--reactivate", "--make-admin", stdout=io.StringIO())
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertEqual(u.role, User.Role.ADMIN)


class EncryptionKeyCheckTests(TestCase):
    def test_bootstrap_refuses_to_start_with_the_wrong_key(self):
        make_user("kim")
        call_command("bootstrap", stdout=io.StringIO())  # right key: fine
        from cryptography.fernet import Fernet
        with override_settings(FIELD_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(CommandError) as cm:
                call_command("bootstrap", stdout=io.StringIO())
        self.assertIn("secrets.json", str(cm.exception))


class StoragePathTests(TestCase):
    def form(self, path):
        return StorageLocationForm(data={"kind": "local", "path": path, "provider": "aws", "prefix": "workbench"}, saved={})

    def test_dangerous_folders_rejected(self):
        data = str(settings.DATA_DIR)
        for bad in (data, data + "/files/..", str(Path(data).parent), "/", "/etc", data + "/backups/x", "relative/path"):
            self.assertFalse(self.form(bad).is_valid(), bad)

    def test_normal_folders_allowed(self):
        self.assertTrue(self.form(str(settings.FILES_DIR)).is_valid())
        self.assertTrue(self.form("/mnt/storage/workbench").is_valid())


class SmallFixesTests(TestCase):
    def test_email_subject_with_line_breaks(self):
        from apps.core import email
        site = SiteSettings.load()
        with mock.patch.object(SiteSettings, "email_ready", True), \
                mock.patch("apps.core.email.connection_for", return_value=None), \
                mock.patch("apps.core.email.EmailMessage") as msg:
            email.send("a@example.com", "Recalled:\nwatchdog resets", "body", site)
        subject = msg.call_args.kwargs["subject"]
        self.assertNotIn("\n", subject)
        self.assertIn("Recalled: watchdog resets", subject)

    def test_csv_cells_cannot_become_formulas(self):
        buf = io.StringIO()
        csvsafe.writer(buf).writerow(["=HYPERLINK(1)", "+1", "-5", "-2.5", "@SUM", "plain", 3])
        self.assertEqual(buf.getvalue().strip(), "'=HYPERLINK(1),+1,-5,-2.5,'@SUM,plain,3")

    def test_money_uses_the_configured_currency(self):
        self.assertEqual(money(1234.5), "$1,234.50")
        with self.settings(WORKBENCH_CURRENCY="EUR"):
            self.assertEqual(money(3), "€3.00")
        self.assertEqual(money(3, "CHF"), "3.00 CHF")
        self.assertEqual(money(None), "—")

    def test_search_with_huge_task_number(self):
        r = signed_in(make_user("s")).get(reverse("core:search") + "?q=PWR-99999999999999999999")
        self.assertEqual(r.status_code, 200)

    def test_overview_counts_every_kind_of_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.firmware.models import Firmware, FirmwareArtifact, FirmwareRelease
        from apps.projects.models import Project
        admin = make_user("boss", role=User.Role.ADMIN)
        p = Project.objects.create(key="PWR", name="Power")
        rel = FirmwareRelease.objects.create(firmware=Firmware.objects.create(project=p, name="App"), version="1.0.0")
        FirmwareArtifact.store(rel, SimpleUploadedFile("app.bin", b"x" * 5000), admin)
        page = signed_in(admin).get(reverse("core:system"))
        self.assertContains(page, "4.9\xa0KB")
        self.assertContains(page, "1 file")


class SchedulerTests(TransactionTestCase):
    def setUp(self):
        make_user("someone")  # an installation that has been set up
        site = SiteSettings.load()
        site.backup_hour, site.time_zone = 2, "UTC"
        site.save()

    def test_missed_backup_hour_is_caught_up_later_that_day(self):
        now = datetime.now(dt_tz.utc).replace(hour=15, minute=0)
        self.assertIn("backup", tick(now))
        self.assertNotIn("backup", tick(now + timedelta(minutes=5)))

    def test_no_backup_before_setup(self):
        User.objects.all().delete()
        self.assertNotIn("backup", tick(datetime.now(dt_tz.utc).replace(hour=15)))

    def test_failed_backup_is_not_retried_every_minute_and_admins_hear_about_it(self):
        admin = make_user("boss", role=User.Role.ADMIN)
        now = datetime.now(dt_tz.utc).replace(hour=3)
        with mock.patch("apps.core.system.create_backup", side_effect=RuntimeError("disk full")) as cb:
            tick(now)
            tick(now + timedelta(minutes=1))
        self.assertEqual(cb.call_count, 1)
        self.assertIn("disk full", SiteSettings.load().last_backup_error)
        self.assertTrue(admin.notifications.filter(text__contains="backup failed").exists())

    def test_trash_is_emptied_even_with_backups_off(self):
        site = SiteSettings.load()
        site.backup_enabled = False
        site.save()
        with mock.patch("apps.core.system.purge_trash", return_value=2) as purge:
            did = tick(datetime.now(dt_tz.utc).replace(hour=1))
            tick(datetime.now(dt_tz.utc).replace(hour=5))
        self.assertIn("trash", did)
        self.assertEqual(purge.call_count, 1)

    def test_one_failing_job_does_not_stop_the_others(self):
        now = datetime.now(dt_tz.utc).replace(hour=3)
        with mock.patch("apps.core.email.send_pending_notifications", side_effect=RuntimeError("smtp down")):
            self.assertIn("backup", tick(now))

    def test_backup_failure_leaves_no_partial_file(self):
        with mock.patch("apps.core.system._write_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                system.create_backup("test")
        self.assertEqual(list(Path(settings.BACKUP_DIR).glob("*.partial")), [])

    def test_backup_refused_without_enough_disk(self):
        from collections import namedtuple
        du = namedtuple("du", "total used free")(10**9, 10**9, 1000)
        with mock.patch("apps.core.system.shutil.disk_usage", return_value=du):
            with self.assertRaisesRegex(RuntimeError, "Not enough free disk space"):
                system.create_backup("test")

    def test_stuck_exports_are_recovered(self):
        old = ExportJob.objects.create(status="running")
        ExportJob.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=5))
        recent = ExportJob.objects.create(status="running")
        export.recover()
        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual((old.status, recent.status), ("failed", "pending"))

    def test_healthz_reports_a_stopped_worker(self):
        beat = Path(settings.DATA_DIR) / "scheduler.heartbeat"
        beat.touch()
        self.assertEqual(json.loads(self.client.get("/healthz").content)["scheduler"], "running")
        old = (timezone.now() - timedelta(minutes=30)).timestamp()
        import os
        os.utime(beat, (old, old))
        r = self.client.get("/healthz")
        self.assertEqual(json.loads(r.content)["scheduler"], "stopped")
        self.assertFalse(json.loads(r.content)["ok"])
        beat.unlink()


class RestoreSafetyTests(TransactionTestCase):
    def _backup_zip(self, tmp, **entries):
        path = Path(tmp) / "workbench-20260101-000000.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, data in entries.items():
                z.writestr(name.replace("__", "/"), data)
        return path

    def test_incompatible_backup_changes_nothing(self):
        import tempfile
        secrets_file = Path(settings.DATA_DIR) / "secrets.json"
        before = secrets_file.read_bytes() if secrets_file.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            path = self._backup_zip(tmp, **{"manifest.json": "{}", "secrets.json": json.dumps({"field_key": "x"})})
            with self.assertRaisesRegex(ValueError, "no database"):
                system.restore_backup(path)
        after = secrets_file.read_bytes() if secrets_file.exists() else None
        self.assertEqual(before, after)
