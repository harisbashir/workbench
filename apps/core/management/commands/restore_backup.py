"""Restore a backup. Stop the web app first:

    docker compose stop workbench
    docker compose run --rm workbench restore workbench-20260925-020000.zip
    docker compose start workbench
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core import system


class Command(BaseCommand):
    help = "Restore database, files and keys from a Workbench backup .zip."

    def add_arguments(self, parser):
        parser.add_argument("backup", help="File name in <data>/backups, or a full path.")
        parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")

    def handle(self, *args, **o):
        path = Path(o["backup"])
        if not path.exists():
            path = Path(settings.BACKUP_DIR) / o["backup"]
        if not path.exists():
            raise CommandError(f"Backup not found: {o['backup']}")
        if not o["yes"]:
            answer = input(f"Replace ALL current data with {path.name}? Type 'restore' to continue: ")
            if answer.strip() != "restore":
                raise CommandError("Cancelled.")
        try:
            manifest, safety = system.restore_backup(path)
        except ValueError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"Restored backup from {manifest.get('created')} (Workbench {manifest.get('version')})."))
        for note in manifest.get("restore_notes", []):
            self.stdout.write(self.style.WARNING(note))
        self.stdout.write(f"Your previous data was kept in {safety} — delete it once you've checked everything.")
        self.stdout.write("Now start Workbench again. Database migrations run automatically on start.")
