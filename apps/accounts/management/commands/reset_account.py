"""Get someone back into their account from the server (e.g. the only administrator).

    manage reset_account --list                       list accounts
    manage reset_account haris                        unlock after too many failed sign-ins
    manage reset_account haris --password             also print a one-time link to choose a new password
    manage reset_account haris --2fa                  also turn off 2FA (set up again at next sign-in)
"""
from django.contrib.auth.tokens import default_token_generator
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import User
from apps.core.models import SiteSettings
from apps.core.utils import audit


class Command(BaseCommand):
    help = "Unlock an account, and optionally reset its password or two-factor authentication."

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?")
        parser.add_argument("--password", action="store_true", help="Print a one-time link to set a new password.")
        parser.add_argument("--2fa", dest="mfa", action="store_true", help="Turn off two-factor authentication.")
        parser.add_argument("--list", action="store_true", help="List accounts.")
        parser.add_argument("--reactivate", action="store_true", help="Reactivate a deactivated account.")
        parser.add_argument("--make-admin", action="store_true", help="Give the account the Administrator role.")

    def handle(self, *args, **o):
        if o["list"] or not o["username"]:
            for u in User.objects.order_by("username"):
                flags = [u.get_role_display()]
                if not u.is_active:
                    flags.append("deactivated")
                if u.is_locked:
                    flags.append("LOCKED")
                if not u.mfa_enabled:
                    flags.append("no 2FA yet")
                self.stdout.write(f"{u.username:20} {u.display_name:28} {', '.join(flags)}")
            return
        try:
            user = User.objects.get(username__iexact=o["username"])
        except User.DoesNotExist:
            raise CommandError(f"No account called {o['username']}. Use --list to see them.")

        done = []
        if not user.is_active:
            if not o["reactivate"]:
                raise CommandError(f"{user.username} is deactivated. Add --reactivate to turn the account back on.")
            user.is_active = True
            user.save(update_fields=["is_active"])
            done.append("reactivated")
        if o["make_admin"]:
            user.role = User.Role.ADMIN
            user.save(update_fields=["role"])
            done.append("made administrator")
        user.failed_logins, user.locked_until, user.mfa_failures = 0, None, 0
        user.save(update_fields=["failed_logins", "locked_until", "mfa_failures"])
        done.append("unlocked")
        if o["mfa"]:
            user.mfa_enabled, user.mfa_secret = False, ""
            user.save(update_fields=["mfa_enabled", "_mfa_secret"])
            user.recovery_codes.all().delete()
            done.append("2FA turned off (they set it up again at next sign-in)")
        audit(None, "user.reset_from_server", user, what=", ".join(done), password_link=o["password"])
        self.stdout.write(self.style.SUCCESS(f"{user.username}: " + "; ".join(done) + "."))
        if o["password"]:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            url = SiteSettings.load().absolute_url(reverse("accounts:set_password", args=[uid, default_token_generator.make_token(user)]))
            if not url.startswith("http"):
                url = "http://localhost:8000" + url
            self.stdout.write("Open this link to choose a new password (works once, valid for 3 days):")
            self.stdout.write(url)
