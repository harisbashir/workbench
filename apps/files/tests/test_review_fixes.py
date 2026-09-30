"""Regression tests for the pre-launch review of the file library."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.files.models import Document
from apps.projects.models import Project


class FileFixTests(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.admin = make_user("admin", role=User.Role.ADMIN)
        self.eng = make_user("eng")
        self.viewer = make_user("view", role=User.Role.VIEWER)
        self.project = Project.objects.create(key="PWR", name="Power", lead=self.lead)
        self.project.members.add(self.eng, self.viewer)
        self.c = signed_in(self.eng)

    def upload(self, name="a.txt", data=b"x"):
        self.c.post(reverse("files:upload_root", args=["PWR"]), {"files": SimpleUploadedFile(name, data)})

    def test_trashed_file_cannot_be_downloaded(self):
        self.upload()
        doc = Document.objects.get()
        version = doc.latest
        self.assertEqual(self.c.get(reverse("files:download", args=[version.pk])).status_code, 200)
        self.c.post(reverse("files:document_delete", args=[doc.pk]))
        self.assertEqual(self.c.get(reverse("files:download", args=[version.pk])).status_code, 404)
        self.assertEqual(self.c.get(reverse("files:download", args=[version.pk]) + "?inline=1").status_code, 404)

    def test_restore_renames_on_name_clash(self):
        self.upload(data=b"old")
        old = Document.objects.get()
        self.c.post(reverse("files:document_delete", args=[old.pk]))
        self.upload(data=b"new")
        self.c.post(reverse("files:trash_action", args=[old.pk]), {"action": "restore"})
        old.refresh_from_db()
        self.assertIsNone(old.deleted_at)
        self.assertEqual(old.name, "a (restored).txt")
        self.assertEqual(Document.objects.alive().filter(name="a.txt").count(), 1)

    def test_restore_keeps_name_without_clash(self):
        self.upload()
        doc = Document.objects.get()
        doc.trash(self.eng)
        doc.restore()
        self.assertEqual(doc.name, "a.txt")

    def test_viewer_does_not_see_restore_buttons(self):
        self.upload()
        Document.objects.get().trash(self.eng)
        self.assertNotContains(signed_in(self.viewer).get(reverse("files:trash")), 'value="restore"')
        self.assertContains(self.c.get(reverse("files:trash")), 'value="restore"')

    def test_versions_are_numbered_in_order(self):
        self.upload(data=b"1")
        self.upload(data=b"2")
        doc = Document.objects.get()
        self.assertEqual(list(doc.versions.order_by("number").values_list("number", flat=True)), [1, 2])
        self.assertEqual(doc.version_count, 2)

    def test_prune_with_bad_keep_is_not_500(self):
        r = signed_in(self.admin).post(reverse("files:prune_versions"), {"keep": "abc"})
        self.assertEqual(r.status_code, 302)
