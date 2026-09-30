"""Background housekeeping, started by the container next to the web server.

Every minute it emails pending notifications, runs storage moves, 3D conversions and
exports; once a day (at or after the configured hour) it takes a backup and removes old
ones; once a day it empties old trash and removes leftovers.
"""
import logging
import threading
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


def tick(now=None, beat=lambda: None):
    """One pass of the scheduler. Returns what it did (used by tests)."""
    done = []
    site = SiteSettings.load()
    now = now or timezone.now()
    try:
        tz = zoneinfo.ZoneInfo(site.time_zone)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    local = now.astimezone(tz)

    def step(name, fn):
        # One failing job must not stop the others.
        try:
            if fn():
                done.append(name)
        except Exception:
            log.exception("Scheduler job failed: %s", name)
        beat()

    step("emails", email.send_pending_notifications)
    from apps.core import export, storage_move
    step("storage move", storage_move.run_pending)
    from apps.cad import jobs as cad_jobs
    step("3d previews", cad_jobs.tick)
    step("export", export.run_pending)

    # Nightly backup: once a day, at or after the chosen hour (so a missed hour — a restart,
    # daylight-saving change or long job — is caught up later that day). A failed attempt is
    # recorded and not retried until the next day, so it can't fill the disk with retries.
    attempted = site.last_nightly_attempt_at.astimezone(tz).date() if site.last_nightly_attempt_at else None
    from apps.accounts.models import User
    has_data = User.objects.exists()  # nothing to back up before setup is finished
    if site.backup_enabled and has_data and local.hour >= site.backup_hour and attempted != local.date():
        SiteSettings.objects.filter(pk=site.pk).update(last_nightly_attempt_at=now)
        try:
            system.create_backup("nightly")
            system.prune_backups(site.backup_keep)
            SiteSettings.objects.filter(pk=site.pk).update(last_backup_error="")
            done.append("backup")
        except Exception as exc:
            log.exception("Nightly backup failed")
            SiteSettings.objects.filter(pk=site.pk).update(last_backup_error=str(exc)[:300])
            _warn_admins(f"Last night's backup failed: {exc}"[:200])
        beat()

    # Daily housekeeping, independent of backups.
    if site.last_housekeeping_on != local.date():
        SiteSettings.objects.filter(pk=site.pk).update(last_housekeeping_on=local.date())
        step("trash", lambda: system.purge_trash(site.trash_days))
        step("cleanup", lambda: (system.prune_partials(), export.prune_exports()) and False)
    return done


def _warn_admins(text):
    from apps.accounts.models import User
    from apps.core.utils import notify
    for admin in User.objects.filter(is_active=True, role=User.Role.ADMIN):
        notify(admin, text, "/system/backups/")


class Command(BaseCommand):
    help = "Run background jobs (email, nightly backups, trash cleanup, conversions) forever."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")

    def handle(self, *args, **o):
        beat_file = Path(settings.DATA_DIR) / "scheduler.heartbeat"
        state = {"pass_started": time.monotonic()}

        def beat():
            try:
                beat_file.touch()
            except OSError:
                pass

        def heartbeat_thread():
            # Proves the worker is alive while a long job (a big backup, a storage move) runs.
            # A pass stuck for over 2 hours stops the heartbeat so /healthz reports it.
            while True:
                if time.monotonic() - state["pass_started"] < 7200:
                    beat()
                time.sleep(30)

        # Jobs interrupted by a restart continue where they stopped.
        from apps.core import export
        from apps.core.models import StorageMove
        StorageMove.objects.filter(status=StorageMove.Status.RUNNING).update(status=StorageMove.Status.PENDING)
        export.recover()
        from apps.cad.jobs import recover_interrupted
        recover_interrupted()
        beat()
        if not o["once"]:
            threading.Thread(target=heartbeat_thread, daemon=True).start()
        while True:
            close_old_connections()
            state["pass_started"] = time.monotonic()
            try:
                did = tick(beat=beat)
                if did:
                    log.info("Scheduler: %s", ", ".join(did))
            except Exception:
                log.exception("Scheduler pass failed")
            beat()
            if o["once"]:
                return
            time.sleep(60)
