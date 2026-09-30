"""Run on every container start: prepares the data folder and prints how to finish setup."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.core.models import SiteSettings


class Command(BaseCommand):
    help = "Prepare Workbench's data folder and print first-run instructions."

    def check_key(self, site):
        """Refuse to start if the encryption key doesn't match the stored secrets.

        That happens when secrets.json was lost or replaced, or WORKBENCH_FIELD_KEY points at
        another key. Running anyway would silently turn 2FA secrets and passwords into blanks.
        """
        from apps.core.crypto import can_decrypt
        samples = [v for v in (site._github_secret, site._smtp_password) if v]
        samples += list(User.objects.exclude(_mfa_secret="").values_list("_mfa_secret", flat=True)[:5])
        bad = [v for v in samples if not can_decrypt(v)]
        if bad:
            raise CommandError(
                "The encryption key in data/secrets.json (or WORKBENCH_FIELD_KEY) doesn't match this database, "
                f"so {len(bad)} stored secret(s) can't be read. Workbench won't start, to keep two-factor sign-in safe. "
                "Put back the secrets.json that belongs to this database (it's in every backup zip), "
                "or restore a complete backup.")

    def handle(self, *args, **o):
        site = SiteSettings.load()
        self.check_key(site)
        line = "=" * 64
        self.stdout.write(line)
        self.stdout.write(f"  Workbench is starting. Data folder: {settings.DATA_DIR}")
        if not User.objects.exists():
            self.stdout.write("")
            self.stdout.write("  FIRST-TIME SETUP")
            url = site.site_url or (f"https://{settings.DOMAIN}" if settings.DOMAIN else "http://<this-server>:8000")
            self.stdout.write(f"  1. Open {url} in your browser")
            self.stdout.write(f"  2. Enter this setup code:  {settings.SETUP_TOKEN}")
            self.stdout.write("  3. Create your administrator account")
        elif not settings.HTTPS:
            self.stdout.write("  Note: running without HTTPS. Use the https profile (see README) for internet access.")
        self.stdout.write(line)
