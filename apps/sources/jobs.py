"""Daily Sources housekeeping, run by the scheduler."""
from datetime import timedelta

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
    used = VersionFile.objects.values("blob_id")
    removed = 0
    for blob in Blob.objects.filter(created_at__lt=cutoff).exclude(pk__in=used).exclude(pk__in=in_drafts).iterator():
        blob.file.delete(save=False)
        blob.delete()
        removed += 1
    return removed
