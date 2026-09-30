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
import time
import zipfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from .storage import is_s3, local_root, reset_cache, storage_info, stored_files, write_exact

log = logging.getLogger("workbench")

VERSION = (Path(settings.BASE_DIR) / "VERSION").read_text().strip() if (Path(settings.BASE_DIR) / "VERSION").exists() else "dev"
STORED = {".zip", ".gz", ".7z", ".png", ".jpg", ".jpeg", ".pdf", ".xlsx", ".docx", ".step", ".stp"}


def disk_usage(path=None):
    try:
        total, used, free = shutil.disk_usage(path or settings.DATA_DIR)
    except OSError:
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


def backup_space_needed():
    """A generous estimate of the disk space a new backup needs (bytes)."""
    from .storage import file_totals
    return int((file_totals()["size"] + (database_size() or 0)) * 1.1) + 200 * 1024 * 1024


def create_backup(reason="manual"):
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    final = Path(settings.BACKUP_DIR) / f"workbench-{stamp}.zip"
    tmp = final.with_suffix(".partial")
    Path(settings.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    need, free = backup_space_needed(), shutil.disk_usage(settings.BACKUP_DIR).free
    if free < need:
        raise RuntimeError(f"Not enough free disk space for a backup: about {need // 2**20} MB needed, "
                           f"{free // 2**20} MB free. Delete old backups or add disk space.")
    try:
        return _write_backup(reason, final, tmp)
    finally:
        tmp.unlink(missing_ok=True)  # a failed backup never leaves a half-written file behind


def _write_backup(reason, final, tmp):
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
            missing = []
            files_root = local_root()
            if files_root:
                for root, _dirs, files in os.walk(files_root):
                    for f in files:
                        full = Path(root) / f
                        if f.startswith(".workbench-check-"):
                            continue
                        arc = "files/" + str(full.relative_to(files_root)).replace(os.sep, "/")
                        method = zipfile.ZIP_STORED if full.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                        try:
                            z.write(full, arc, compress_type=method)
                        except FileNotFoundError:  # deleted while the backup was running
                            missing.append(arc[len("files/"):])
                            continue
                        n += 1
            else:
                # Cloud storage: copy every file Workbench references into the zip. The source is
                # opened first, so a file that can't be read never leaves an empty entry behind.
                for name, _size in stored_files():
                    try:
                        src = default_storage.open(name, "rb")
                    except Exception:
                        missing.append(name)
                        continue
                    try:
                        with src, z.open("files/" + name, "w", force_zip64=True) as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                        n += 1
                    except Exception:
                        missing.append(name)
            manifest["missing_files"] = missing
            manifest["files"] = n
            manifest["storage"] = storage_info()["kind"]
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    tmp.replace(final)
    if is_s3():
        # Also keep an off-server copy next to the files in the bucket.
        try:
            with open(final, "rb") as fh:
                write_exact(f"backups/{final.name}", fh)
        except Exception:
            log.exception("Couldn't copy backup %s to S3", final.name)
    from .models import SiteSettings
    SiteSettings.objects.filter(pk=1).update(last_backup_at=timezone.now())
    log.info("Backup created: %s (%s)", final.name, reason)
    return final


def prune_partials():
    """Remove leftovers of backups that were interrupted (e.g. the server restarted)."""
    cutoff = time.time() - 6 * 3600
    for p in Path(settings.BACKUP_DIR).glob("workbench-*.partial"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def prune_backups(keep):
    keep = max(int(keep), 1)
    removed = []
    for item in list_backups()[keep:]:
        (Path(settings.BACKUP_DIR) / item["name"]).unlink(missing_ok=True)
        removed.append(item["name"])
    if is_s3():
        try:
            _dirs, remote = default_storage.listdir("backups")
            for name in sorted((n for n in remote if n.startswith("workbench-")), reverse=True)[keep:]:
                default_storage.delete(f"backups/{name}")
        except Exception:
            log.exception("Couldn't prune backups in S3")
    return removed


def restore_backup(zip_path, *, files_only=False):
    """Restore a backup over the current data. Run with the web app stopped.

    Everything is checked before anything is changed. The data from before the restore is
    kept in data/pre-restore-<time>/ so a mistaken restore can be undone.
    """
    zip_path = Path(zip_path)
    notes = []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        # --- 1. Check everything first ---------------------------------------------
        if "manifest.json" not in names:
            raise ValueError("This isn't a Workbench backup (manifest.json is missing).")
        manifest = json.loads(z.read("manifest.json"))
        for n in names:  # refuse path traversal
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValueError(f"Unsafe path in backup: {n}")
        if "db/workbench.sqlite3" in names and not is_sqlite():
            raise ValueError("This backup is from a SQLite installation; restore it into a SQLite installation.")
        has_db = "db/workbench.sqlite3" in names or "db/data.json" in names
        if not has_db and not files_only:
            raise ValueError("This backup has no database in it.")
        new_secrets = None
        if "secrets.json" in names:
            new_secrets = json.loads(z.read("secrets.json"))
            if not new_secrets.get("field_key"):
                raise ValueError("The keys file in this backup is incomplete.")
        missing = set(manifest.get("missing_files") or [])

        # --- 2. Keep the current data aside ----------------------------------------
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        safety = Path(settings.DATA_DIR) / f"pre-restore-{stamp}"
        safety.mkdir()
        secrets_file = Path(settings.DATA_DIR) / "secrets.json"
        if secrets_file.exists():
            shutil.copy2(secrets_file, safety / "secrets.json")

        # --- 3. Database, then keys --------------------------------------------------
        if not has_db:
            pass  # files-only restore (tests)
        elif "db/workbench.sqlite3" in names:
            db = Path(settings.DATABASES["default"]["NAME"])
            connection.close()
            for p in db.parent.glob(db.name + "*"):
                shutil.move(str(p), safety / p.name)
            with z.open("db/workbench.sqlite3") as src, open(db, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            dump = safety / "restore-data.json"
            dump.write_bytes(z.read("db/data.json"))
            call_command("flush", "--noinput")
            call_command("loaddata", str(dump))
        if new_secrets is not None:
            secrets_file.write_bytes(z.read("secrets.json"))
            os.chmod(secrets_file, 0o600)
            settings.FIELD_KEY = new_secrets.get("field_key", settings.FIELD_KEY)

        # --- 4. Files: wherever the restored settings say they're kept --------------
        reset_cache()
        files_root = local_root()
        file_entries = [n for n in names if n.startswith("files/") and not n.endswith("/") and n[len("files/"):] not in missing]
        if files_root:
            root = Path(files_root)
            default_root = Path(settings.FILES_DIR)
            if not root.exists() and root.resolve() != default_root.resolve():
                # The restored settings point at a disk that isn't mounted here. Don't create the
                # folder inside the container (it would vanish on the next update): use the data
                # folder instead and switch the setting back to it.
                from .models import StorageSettings
                cfg = StorageSettings.load()
                cfg.active, cfg.previous, cfg.locked = {}, {}, False
                cfg.save()
                reset_cache()
                notes.append(f"The backup's files folder {root} doesn't exist on this server, so the files were "
                             f"restored into {default_root} and storage was set back to the data folder.")
                root = default_root
            root.mkdir(parents=True, exist_ok=True)
            # Keep the current files aside (contents are moved, not the folder, which may be a mounted disk).
            (safety / "files").mkdir()
            for child in root.iterdir():
                shutil.move(str(child), safety / "files" / child.name)
            for n in file_entries:
                target = root / n[len("files/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            # Cloud storage: upload each file back under its original key (versions are
            # immutable paths, so existing objects are simply replaced). Files that were missing
            # when the backup was taken are skipped so an empty copy can't replace a good one.
            for n in file_entries:
                with z.open(n) as src:
                    write_exact(n[len("files/"):], src)
        if missing:
            notes.append(f"{len(missing)} file(s) were already missing when this backup was taken and weren't restored.")
    manifest["restore_notes"] = notes
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
    # The background worker touches its heartbeat every 30 seconds. If it has been silent for
    # 10 minutes it has stopped (nightly backups, emails and conversions aren't happening).
    scheduler_ok = last_beat is None or (datetime.now(dt_timezone.utc) - last_beat).total_seconds() < 600
    return {
        "ok": ok_db and disk["free"] > 200 * 1024 * 1024 and scheduler_ok,
        "database": ok_db, "disk_free_mb": disk["free"] // (1024 * 1024),
        "scheduler": "running" if last_beat and scheduler_ok else ("stopped" if last_beat else "not started"),
        "scheduler_seen": last_beat.isoformat() if last_beat else None,
        "version": VERSION,
    }
