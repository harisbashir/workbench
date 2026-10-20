"""Storing file contents once, by SHA-256."""
import hashlib
import io
import tempfile

from django.core.files import File
from django.db import IntegrityError, transaction

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


def store(fileobj, size_limit, name="file"):
    """Store the contents of an open binary file and return its Blob. Raises ValueError past `size_limit` bytes."""
    h, size, head = hashlib.sha256(), 0, b""
    spool = tempfile.SpooledTemporaryFile(max_size=5 * CHUNK)
    for chunk in iter(lambda: fileobj.read(CHUNK), b""):
        size += len(chunk)
        if size > size_limit:
            spool.close()
            raise ValueError(f"{name} is larger than the {size_limit // CHUNK or 1} MB limit.")
        if len(head) < 8192:
            head += chunk[:8192 - len(head)]
        h.update(chunk)
        spool.write(chunk)
    sha = h.hexdigest()
    try:
        existing = Blob.objects.filter(sha256=sha).first()
        if existing:
            return existing
        spool.seek(0)
        blob = Blob(sha256=sha, size=size, is_text=looks_like_text(head))
        try:
            with transaction.atomic():
                blob.file.save(sha, File(spool), save=False)
                blob.save()
        except IntegrityError:  # stored by someone else at the same moment
            blob.file.delete(save=False)
            return Blob.objects.get(sha256=sha)
        return blob
    finally:
        spool.close()


def store_bytes(data, name="file"):
    return store(io.BytesIO(data), max(len(data), 1), name=name)
