"""Backups, health information and housekeeping.

A backup is a single .zip in <data>/backups containing:
    manifest.json   what's inside and which version made it
    secrets.json    the generated keys (needed to read 2FA secrets!)
    db/…            the database (SQLite file, or JSON for PostgreSQL)
    files/…         every uploaded file

Treat backups as sensitive: they contain everything.
"""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

log = logging.getLogger("workbench")

VERSION = (Path(settings.BASE_DIR) / "VERSION").read_text().strip() if (Path(settings.BASE_DIR) / "VERSION").exists() else "dev"
STORED = {".zip", ".gz", ".7z", ".png", ".jpg", ".jpeg", ".pdf", ".xlsx", ".docx", ".step", ".stp"}


def disk_usage():
    total, used, free = shutil.disk_usage(settings.DATA_DIR)
    return {"total": total, "used": used, "free": free, "percent": round(100 * used / total) if total else 0}


def folder_size(path):
    size = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return size


def is_sqlite():
    return connection.vendor == "sqlite"


def database_size():
    if is_sqlite():
        db = Path(settings.DATABASES["default"]["NAME"])
        return sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.exists())
    return None


# --- Backups ------------------------------------------------------------------

def list_backups():
    items = []
    for p in sorted(Path(settings.BACKUP_DIR).glob("workbench-*.zip"), reverse=True):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size,
                      "created": datetime.fromtimestamp(st.st_mtime, tz=timezone.get_current_timezone())})
    return items


def backup_path(name):
    """Resolve a backup file name safely inside the backup folder."""
    if not name.startswith("workbench-") or not name.endswith(".zip") or "/" in name or "\\" in name:
        return None
    p = Path(settings.BACKUP_DIR) / name
    return p if p.exists() else None


def create_backup(reason="manual"):
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    final = Path(settings.BACKUP_DIR) / f"workbench-{stamp}.zip"
    tmp = final.with_suffix(".partial")
    with tempfile.TemporaryDirectory(dir=settings.DATA_DIR) as work:
        work = Path(work)
        manifest = {"app": "workbench", "version": VERSION, "created": timezone.now().isoformat(),
                    "reason": reason, "database": connection.vendor}
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            if is_sqlite():
                snap = work / "workbench.sqlite3"
                if connection.in_atomic_block:
                    raise RuntimeError("Backups can't run inside a database transaction.")
                connection.ensure_connection()
                dst = sqlite3.connect(str(snap))
                # SQLite's online backup: a consistent copy, safe while people are using the app.
                connection.connection.backup(dst)
                dst.close()
                z.write(snap, "db/workbench.sqlite3")
            else:
                dump = work / "data.json"
                with open(dump, "w") as fh:
                    call_command("dumpdata", "--natural-foreign", "--exclude=contenttypes", "--exclude=auth.permission",
                                 "--exclude=sessions", stdout=fh)
                z.write(dump, "db/data.json")
            secrets_file = Path(settings.DATA_DIR) / "secrets.json"
            if secrets_file.exists():
                z.write(secrets_file, "secrets.json")
            n = 0
            for root, _dirs, files in os.walk(settings.FILES_DIR):
                for f in files:
                    full = Path(root) / f
                    arc = "files/" + str(full.relative_to(settings.FILES_DIR)).replace(os.sep, "/")
                    method = zipfile.ZIP_STORED if full.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                    z.write(full, arc, compress_type=method)
                    n += 1
            manifest["files"] = n
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    tmp.replace(final)
    from .models import SiteSettings
    SiteSettings.objects.filter(pk=1).update(last_backup_at=timezone.now())
    log.info("Backup created: %s (%s)", final.name, reason)
    return final


def prune_backups(keep):
    keep = max(int(keep), 1)
    removed = []
    for item in list_backups()[keep:]:
        (Path(settings.BACKUP_DIR) / item["name"]).unlink(missing_ok=True)
        removed.append(item["name"])
    return removed


def restore_backup(zip_path):
    """Restore a backup over the current data. Run with the web app stopped."""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "manifest.json" not in names:
            raise ValueError("This isn't a Workbench backup (manifest.json is missing).")
        manifest = json.loads(z.read("manifest.json"))
        for n in names:  # refuse path traversal
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValueError(f"Unsafe path in backup: {n}")
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        safety = Path(settings.DATA_DIR) / f"pre-restore-{stamp}"
        safety.mkdir()
        # Keep the current data aside so a mistaken restore can be undone.
        if Path(settings.FILES_DIR).exists():
            shutil.move(str(settings.FILES_DIR), safety / "files")
        Path(settings.FILES_DIR).mkdir(parents=True, exist_ok=True)
        secrets_file = Path(settings.DATA_DIR) / "secrets.json"
        if secrets_file.exists():
            shutil.copy2(secrets_file, safety / "secrets.json")
        for n in names:
            if n.startswith("files/") and not n.endswith("/"):
                target = Path(settings.FILES_DIR) / n[len("files/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if "secrets.json" in names:
            secrets_file.write_bytes(z.read("secrets.json"))
            os.chmod(secrets_file, 0o600)
        if "db/workbench.sqlite3" in names:
            if not is_sqlite():
                raise ValueError("This backup is from a SQLite installation; restore it into a SQLite installation.")
            db = Path(settings.DATABASES["default"]["NAME"])
            connection.close()
            for p in db.parent.glob(db.name + "*"):
                shutil.move(str(p), safety / p.name)
            with z.open("db/workbench.sqlite3") as src, open(db, "wb") as dst:
                shutil.copyfileobj(src, dst)
        elif "db/data.json" in names:
            dump = safety / "restore-data.json"
            dump.write_bytes(z.read("db/data.json"))
            call_command("flush", "--noinput")
            call_command("loaddata", str(dump))
    return manifest, safety


# --- Housekeeping (run by the scheduler) -------------------------------------------

def purge_trash(days):
    from apps.files.models import Document
    cutoff = timezone.now() - timezone.timedelta(days=days)
    n = 0
    for doc in Document.objects.filter(deleted_at__lt=cutoff):
        doc.purge()
        n += 1
    return n


def health():
    from django.db import connection as c
    ok_db = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:  # pragma: no cover
        ok_db = False
    beat = Path(settings.DATA_DIR) / "scheduler.heartbeat"
    last_beat = datetime.fromtimestamp(beat.stat().st_mtime, tz=dt_timezone.utc) if beat.exists() else None
    disk = disk_usage()
    return {
        "ok": ok_db and disk["free"] > 200 * 1024 * 1024,
        "database": ok_db, "disk_free_mb": disk["free"] // (1024 * 1024),
        "scheduler_seen": last_beat.isoformat() if last_beat else None,
        "version": VERSION,
    }
