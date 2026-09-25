from django.core.management.base import BaseCommand

from apps.core import system
from apps.core.models import SiteSettings


class Command(BaseCommand):
    help = "Create a backup now in <data>/backups."

    def handle(self, *args, **o):
        path = system.create_backup("command line")
        system.prune_backups(SiteSettings.load().backup_keep)
        self.stdout.write(self.style.SUCCESS(f"Backup written to {path}"))
