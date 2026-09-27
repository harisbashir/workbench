"""Check or unlock file storage from the server.

    manage storage              where files are kept, and whether it's locked
    manage storage --test       write, read back and delete a test file
    manage storage --unlock     allow changing storage in System → Storage again
    manage storage --lock       lock it (same as the checkbox when moving)
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core import storage as st
from apps.core.models import StorageMove, StorageSettings


class Command(BaseCommand):
    help = "Show, test, lock or unlock where uploaded files are stored."

    def add_arguments(self, parser):
        parser.add_argument("--test", action="store_true")
        parser.add_argument("--unlock", action="store_true")
        parser.add_argument("--lock", action="store_true")

    def handle(self, *args, **o):
        cfg = StorageSettings.load()
        if o["unlock"] or o["lock"]:
            if st.env_config():
                raise CommandError("Storage is set in .env (WORKBENCH_STORAGE), so there is nothing to lock or unlock.")
            cfg.locked = bool(o["lock"])
            cfg.locked_at = timezone.now() if o["lock"] else None
            cfg.save()
            from apps.core.models import AuditLog
            AuditLog.objects.create(action="storage.locked" if o["lock"] else "storage.unlocked", object_repr="from the server command line")
            self.stdout.write(self.style.SUCCESS("Storage locked." if o["lock"] else "Storage unlocked — it can be changed in System → Storage."))
            return
        info = st.storage_info()
        self.stdout.write(f"Files are kept in: {info['where']}" + ("  (set in .env)" if info["pinned"] else ""))
        self.stdout.write(f"Locked: {'yes' if cfg.locked and not info['pinned'] else 'no'}")
        if cfg.previous and not info["pinned"]:
            self.stdout.write(f"Old storage still used as a fallback: {st.describe(st.unseal(cfg.previous))['where']}")
        move = StorageMove.objects.first()
        if move:
            self.stdout.write(f"Last move: {move.get_status_display()} — {move.message or f'{move.done_count}/{move.total} files'}")
        if o["test"]:
            ok, msg = st.test_storage(st.active_config())
            (self.stdout.write if ok else self.stderr.write)(msg)
            if not ok:
                raise CommandError("Storage test failed.")
