"""Background housekeeping, started by the container next to the web server.

Every minute it: emails pending notifications; once a day at the configured
hour it takes a backup, removes old backups and empties old trash.
"""
import logging
import time
import zoneinfo
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.core import email, system
from apps.core.models import SiteSettings

log = logging.getLogger("workbench")


def tick(now=None):
    """One pass of the scheduler. Returns what it did (used by tests)."""
    done = []
    site = SiteSettings.load()
    now = now or timezone.now()
    try:
        tz = zoneinfo.ZoneInfo(site.time_zone)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    local = now.astimezone(tz)
    if email.send_pending_notifications():
        done.append("emails")
    from apps.core import export
    if export.run_pending():
        export.prune_exports()
        done.append("export")
    last = site.last_backup_at.astimezone(tz).date() if site.last_backup_at else None
    if site.backup_enabled and local.hour == site.backup_hour and last != local.date():
        system.create_backup("nightly")
        system.prune_backups(site.backup_keep)
        done.append("backup")
        if system.purge_trash(site.trash_days):
            done.append("trash")
    return done


class Command(BaseCommand):
    help = "Run background jobs (email, nightly backups, trash cleanup) forever."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")

    def handle(self, *args, **o):
        beat = Path(settings.DATA_DIR) / "scheduler.heartbeat"
        while True:
            close_old_connections()
            try:
                did = tick()
                if did:
                    log.info("Scheduler: %s", ", ".join(did))
            except Exception:
                log.exception("Scheduler pass failed")
            beat.touch()
            if o["once"]:
                return
            time.sleep(60)
