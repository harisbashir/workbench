from datetime import timedelta

from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone

from apps.sources import blobs, jobs
from apps.sources.models import Blob, Draft

from .helpers import commit, make_repo


class CleanupTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        commit(self.f.repo, self.f.eng, **{"kept.c": "kept"})
        self.old = timezone.now() - timedelta(days=2)

    def test_gc_removes_unreferenced_old_blobs_and_drafts(self):
        orphan = blobs.store_bytes(b"orphan")
        staged = blobs.store_bytes(b"staged in an old draft")
        draft = Draft.objects.create(repository=self.f.repo, user=self.f.eng, files={"x.c": staged.pk})
        Blob.objects.filter(pk__in=[orphan.pk, staged.pk]).update(created_at=self.old)
        Draft.objects.filter(pk=draft.pk).update(created_at=self.old)
        name = orphan.file.name
        self.assertEqual(jobs.gc(), 2)
        self.assertFalse(Blob.objects.filter(pk__in=[orphan.pk, staged.pk]).exists())
        self.assertFalse(Draft.objects.exists())
        self.assertFalse(default_storage.exists(name))

    def test_gc_keeps_referenced_and_recent(self):
        Blob.objects.update(created_at=self.old)
        recent = blobs.store_bytes(b"just uploaded")
        fresh = blobs.store_bytes(b"in a fresh draft")
        Blob.objects.filter(pk=fresh.pk).update(created_at=self.old)
        Draft.objects.create(repository=self.f.repo, user=self.f.eng, files={"y.c": fresh.pk})
        self.assertEqual(jobs.gc(), 0)
        self.assertEqual(Blob.objects.count(), 3)
        self.assertTrue(Blob.objects.filter(pk=recent.pk).exists())
