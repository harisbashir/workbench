"""Moving every uploaded file to a new storage, then switching to it.

1. Copy each file (skipping ones already there with the same size), checking
   the size after every copy. Uploads keep going to the old storage meanwhile.
2. If every file made it, switch: the new storage becomes active and the old
   one is kept as a read fallback, so nothing is ever missing.
3. Wait for every web worker to notice, then copy anything uploaded to the old
   storage during the switch.

Nothing is deleted from the old storage. A move can be started again safely:
files already copied are skipped.
"""
import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

from . import storage as st

log = logging.getLogger("workbench")


def _copy_all(move, source, target, *, second_pass=False):
    names = list(st.stored_files())
    if not second_pass:
        move.total = len(names)
        move.save(update_fields=["total"])
    for i, (name, size) in enumerate(names, 1):
        try:
            if target.exists(name) and (size is None or target.size(name) == size):
                if not second_pass:
                    move.skipped += 1
            elif second_pass and not source.exists(name):
                pass
            else:
                st.copy_between(name, source, target)
                got = target.size(name)
                if size is not None and got != size:
                    raise RuntimeError(f"size is {got} bytes after copying, expected {size}")
                move.copied += 1
                move.bytes_copied += got
        except Exception as exc:
            move.failed += 1
            if len(move.failures) < 50:
                move.failures.append({"file": name, "error": st.explain_error(exc)})
        if not second_pass:
            move.done_count = i
        if i % 10 == 0 or i == len(names):
            move.save(update_fields=["done_count", "copied", "skipped", "failed", "bytes_copied", "failures"])


def run_move(move, settle_seconds=None):
    from .models import StorageMove, StorageSettings

    move.status, move.started_at = StorageMove.Status.RUNNING, timezone.now()
    move.save(update_fields=["status", "started_at"])
    try:
        source, target = st.build(st.unseal(move.source)), st.build(st.unseal(move.target))
        _copy_all(move, source, target)
        if move.failed:
            move.status = StorageMove.Status.FAILED
            move.message = (f"{move.failed} of {move.total} files couldn't be copied, so Workbench is still using the old storage. "
                            "Fix the problem and start the move again — files already copied are skipped.")
        else:
            cfg = StorageSettings.load()
            cfg.previous = move.source
            cfg.active = move.target
            cfg.draft = {}
            if move.lock_after:
                cfg.locked, cfg.locked_at = True, timezone.now()
            cfg.save()
            # Let every web worker pick up the switch, then catch uploads made meanwhile.
            time.sleep(st._TTL * 2 + 1 if settle_seconds is None else settle_seconds)
            _copy_all(move, source, target, second_pass=True)
            move.status = StorageMove.Status.DONE
            move.message = f"Moved {move.total} files ({move.copied} copied, {move.skipped} were already there)."
    except Exception as exc:
        log.exception("Storage move failed")
        move.status, move.message = StorageMove.Status.FAILED, st.explain_error(exc)[:300]
    move.finished_at = timezone.now()
    move.save()
    if move.requested_by:
        from .utils import notify
        notify(move.requested_by, "Files moved to the new storage" if move.status == "done" else "Moving files to the new storage stopped",
               "/system/storage/")
    return move


def run_pending():
    from .models import StorageMove
    move = StorageMove.objects.filter(status=StorageMove.Status.PENDING).order_by("created_at").first()
    if move:
        run_move(move)
        return True
    return False


def start_in_background(move):
    """Used when no background worker is running (e.g. development)."""
    def work():
        close_old_connections()
        try:
            run_move(move)
        finally:
            close_old_connections()
    threading.Thread(target=work, daemon=True, name="storage-move").start()
