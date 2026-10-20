import io

from django.core.files.storage import default_storage
from django.test import TestCase

from apps.projects.models import Project
from apps.sources import blobs
from apps.sources.models import Blob, Repository


class BlobTests(TestCase):
    def test_same_content_stored_once(self):
        a = blobs.store(io.BytesIO(b"int main(void) { return 0; }\n"), 1024)
        b = blobs.store(io.BytesIO(b"int main(void) { return 0; }\n"), 1024)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Blob.objects.count(), 1)
        self.assertTrue(default_storage.exists(a.file.name))
        self.assertEqual(a.read(), b"int main(void) { return 0; }\n")

    def test_text_detection(self):
        self.assertTrue(blobs.store_bytes(b"int main(){}\n").is_text)
        self.assertFalse(blobs.store_bytes(b"\x00\x01\x02\xff").is_text)

    def test_size_limit(self):
        with self.assertRaisesMessage(ValueError, "limit"):
            blobs.store(io.BytesIO(b"x" * 2048), 1024)
        self.assertFalse(Blob.objects.exists())

    def test_repository_slug_unique_per_project(self):
        p = Project.objects.create(key="PWR", name="Power")
        a = Repository.objects.create(project=p, name="Main FW")
        b = Repository.objects.create(project=p, name="Main FW 2")
        c = Repository.objects.create(project=p, name="main fw")
        self.assertEqual((a.slug, b.slug, c.slug), ("main-fw", "main-fw-2", "main-fw-3"))
        self.assertIsNone(a.latest())
