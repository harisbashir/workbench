"""Run on every container start: prepares the data folder and prints how to finish setup."""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.accounts.models import User
from apps.core.models import SiteSettings


class Command(BaseCommand):
    help = "Prepare Workbench's data folder and print first-run instructions."

    def handle(self, *args, **o):
        site = SiteSettings.load()
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
