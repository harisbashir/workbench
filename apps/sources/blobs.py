"""Storing file contents once, by SHA-256."""
import hashlib
import io
import tempfile

from django.core.files import File
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Blob

CHUNK = 1024 * 1024


def looks_like_text(head):
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start > len(head) - 4:  # a character cut in half at the end of the sample
            return True
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127)
    return printable / max(len(head), 1) > 0.95


def _discard(loser, winner):
    """Remove the file a losing writer saved. On S3 both writers saved the same key, so keep it then."""
    if loser.file.name and loser.file.name != winner.file.name:
        loser.file.delete(save=False)


def _reuse(blob, spool):
    """Use an existing Blob: mark it as just used (so cleanup leaves it alone) and put its file back if it's gone."""
    Blob.objects.filter(pk=blob.pk).update(created_at=timezone.now())
    if not blob.file.storage.exists(blob.file.name):
        spool.seek(0)
        blob.file.save(blob.sha256, File(spool), save=False)
        Blob.objects.filter(pk=blob.pk).update(file=blob.file.name)
    return blob


def store(fileobj, size_limit, name="file"):
    """Store the contents of an open binary file and return its Blob. Raises ValueError past `size_limit` bytes."""
    h, size, head = hashlib.sha256(), 0, b""
    spool = tempfile.SpooledTemporaryFile(max_size=5 * CHUNK)
    try:
        for chunk in iter(lambda: fileobj.read(CHUNK), b""):
            size += len(chunk)
            if size > size_limit:
                raise ValueError(f"{name} is larger than the {size_limit // CHUNK or 1} MB limit.")
            if len(head) < 8192:
                head += chunk[:8192 - len(head)]
            h.update(chunk)
            spool.write(chunk)
        sha = h.hexdigest()
        existing = Blob.objects.filter(sha256=sha).first()
        if existing:
            return _reuse(existing, spool)
        spool.seek(0)
        blob = Blob(sha256=sha, size=size, is_text=looks_like_text(head))
        blob.file.save(sha, File(spool), save=False)
        try:
            with transaction.atomic():
                blob.save()
        except IntegrityError:  # someone stored the same contents at the same moment
            winner = Blob.objects.get(sha256=sha)
            _discard(blob, winner)
            return _reuse(winner, spool)
        return blob
    finally:
        spool.close()


def store_bytes(data, name="file"):
    return store(io.BytesIO(data), max(len(data), 1), name=name)
