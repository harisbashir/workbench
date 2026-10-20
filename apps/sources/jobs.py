"""Daily Sources housekeeping, run by the scheduler."""
from datetime import timedelta

from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, ProtectedError
from django.utils import timezone

from .models import Blob, Draft, VersionFile

KEEP = timedelta(days=1)


def gc(now=None):
    """Remove check-in drafts nobody confirmed, then stored file contents no version or draft uses.
    Returns how many contents were removed."""
    cutoff = (now or timezone.now()) - KEEP
    Draft.objects.filter(created_at__lt=cutoff).delete()
    in_drafts = set()
    for files in Draft.objects.values_list("files", flat=True):
        in_drafts.update(files.values())
    unused = ~Exists(VersionFile.objects.filter(blob_id=OuterRef("pk")))
    removed = 0
    for pk, name in list(Blob.objects.filter(unused, created_at__lt=cutoff).exclude(pk__in=in_drafts).values_list("pk", "file")):
        # Delete the row first, re-checking in the same statement that nothing uses it and it wasn't just reused;
        # only then remove the file, so a check-in happening at the same moment never loses its contents.
        try:
            with transaction.atomic():
                gone = Blob.objects.filter(unused, pk=pk, created_at__lt=cutoff).delete()[0]
        except (IntegrityError, ProtectedError):
            continue
        if gone:
            if not Blob.objects.filter(file=name).exists():
                default_storage.delete(name)
            removed += 1
    return removed
