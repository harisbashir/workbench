import re

import pyotp
from django.contrib.auth.tokens import default_token_generator
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import RecoveryCode, User
from apps.core.models import AuditLog
from apps.core.testing import make_user, signed_in

PASSWORD = "Correct-horse-battery-9"


class LoginAndTwoFactorTests(TestCase):
    def test_pages_require_sign_in(self):
        make_user("someone")
        r = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(r, reverse("accounts:login") + "?next=/", fetch_redirect_response=False)

    def test_new_user_is_forced_to_set_up_2fa(self):
        User.objects.create_user("new", password=PASSWORD)
        r = self.client.post(reverse("accounts:login"), {"username": "new", "password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        r = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(r, reverse("accounts:mfa_setup"), fetch_redirect_response=False)

    def test_full_2fa_enrolment(self):
        User.objects.create_user("new", password=PASSWORD)
        self.client.post(reverse("accounts:login"), {"username": "new", "password": PASSWORD})
        page = self.client.get(reverse("accounts:mfa_setup")).content.decode()
        secret = re.search(r"<code>([A-Z2-7]{32})</code>", page).group(1)
        r = self.client.post(reverse("accounts:mfa_setup"), {"code": pyotp.TOTP(secret).now()})
        self.assertRedirects(r, reverse("accounts:recovery_codes"), fetch_redirect_response=False)
        user = User.objects.get(username="new")
        self.assertTrue(user.mfa_enabled)
        self.assertEqual(user.mfa_secret, secret)
        self.assertNotIn(secret, user._mfa_secret, "secret must be encrypted at rest")
        self.assertEqual(user.recovery_codes.count(), 8)
        self.assertEqual(self.client.get(reverse("core:dashboard")).status_code, 200)

    def test_enrolled_user_must_enter_code(self):
        user = make_user("eng")
        self.client.post(reverse("accounts:login"), {"username": "eng", "password": PASSWORD})
        r = self.client.get(reverse("core:dashboard"))
        self.assertTrue(r["Location"].startswith(reverse("accounts:mfa_verify")))
        r = self.client.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("accounts:mfa_verify"), {"code": pyotp.TOTP(user.mfa_secret).now()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse("core:dashboard")).status_code, 200)

    def test_recovery_code_works_once(self):
        user = make_user("eng")
        codes = RecoveryCode.generate_for(user)
        for expected in (302, 200):
            c = Client()
            c.post(reverse("accounts:login"), {"username": "eng", "password": PASSWORD})
            r = c.post(reverse("accounts:mfa_verify"), {"code": codes[0]})
            self.assertEqual(r.status_code, expected)

    @override_settings(LOGIN_MAX_ATTEMPTS=3)
    def test_account_locks_after_failed_attempts(self):
        make_user("eng")
        for _ in range(3):
            self.client.post(reverse("accounts:login"), {"username": "eng", "password": "wrong"})
        self.assertTrue(User.objects.get(username="eng").is_locked)
        r = self.client.post(reverse("accounts:login"), {"username": "eng", "password": PASSWORD})
        # Locked accounts get the same message as a wrong password (no username discovery).
        self.assertContains(r, "Wrong username or password")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(AuditLog.objects.filter(action="login.locked").exists())

    def test_set_password_link_is_single_use(self):
        user = User.objects.create_user("invitee")
        user.set_unusable_password()
        user.save()
        url = reverse("accounts:set_password", args=[urlsafe_base64_encode(force_bytes(user.pk)), default_token_generator.make_token(user)])
        r = self.client.post(url, {"password1": "A-much-better-password-1", "password2": "A-much-better-password-1"})
        self.assertRedirects(r, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_weak_password_rejected(self):
        user = User.objects.create_user("invitee")
        url = reverse("accounts:set_password", args=[urlsafe_base64_encode(force_bytes(user.pk)), default_token_generator.make_token(user)])
        r = self.client.post(url, {"password1": "12345678", "password2": "12345678"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.get(pk=user.pk).has_usable_password())

    def test_logout_requires_post(self):
        c = signed_in(make_user("eng"))
        self.assertEqual(c.get(reverse("accounts:logout")).status_code, 405)


class AdminOnlyTests(TestCase):
    def test_engineer_cannot_manage_people(self):
        c = signed_in(make_user("eng"))
        for name in ("accounts:user_list", "accounts:user_create", "core:audit_log"):
            self.assertEqual(c.get(reverse(name)).status_code, 403, name)

    def test_admin_creates_user_and_gets_link(self):
        c = signed_in(make_user("boss", role=User.Role.ADMIN))
        r = c.post(reverse("accounts:user_create"), {"username": "ali", "first_name": "Ali", "role": "engineer",
                                                     "time_zone": "Asia/Karachi", "is_active": "on"})
        ali = User.objects.get(username="ali")
        self.assertRedirects(r, reverse("accounts:user_invite", args=[ali.pk]), fetch_redirect_response=False)
        self.assertContains(c.get(r["Location"]), "/accounts/set-password/")
        self.assertFalse(ali.has_usable_password())

    def test_admin_can_reset_2fa(self):
        admin = make_user("boss", role=User.Role.ADMIN)
        eng = make_user("eng")
        signed_in(admin).post(reverse("accounts:user_reset_mfa", args=[eng.pk]))
        eng.refresh_from_db()
        self.assertFalse(eng.mfa_enabled)


class TotpReplayTests(TestCase):
    def test_code_cannot_be_reused(self):
        user = make_user("eng")
        code = pyotp.TOTP(user.mfa_secret).now()
        c1 = Client()
        c1.post(reverse("accounts:login"), {"username": "eng", "password": PASSWORD})
        self.assertEqual(c1.post(reverse("accounts:mfa_verify"), {"code": code}).status_code, 302)
        c2 = Client()
        c2.post(reverse("accounts:login"), {"username": "eng", "password": PASSWORD})
        self.assertEqual(c2.post(reverse("accounts:mfa_verify"), {"code": code}).status_code, 200)


class ResetAccountCommandTests(TestCase):
    def test_unlock_reset_2fa_and_password_link(self):
        import io
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils import timezone

        from apps.core.models import AuditLog
        u = make_user("boss", role=User.Role.ADMIN)
        User.objects.filter(pk=u.pk).update(locked_until=timezone.now() + timedelta(minutes=10), failed_logins=3,
                                            mfa_enabled=True, _mfa_secret="X")
        out = io.StringIO()
        call_command("reset_account", "BOSS", "--2fa", "--password", stdout=out)
        u.refresh_from_db()
        self.assertEqual((u.locked_until, u.failed_logins, u.mfa_enabled), (None, 0, False))
        self.assertIn("/set-password/", out.getvalue())
        self.assertTrue(AuditLog.objects.filter(action="user.reset_from_server").exists())
        out = io.StringIO()
        call_command("reset_account", "--list", stdout=out)
        self.assertIn("boss", out.getvalue())
