from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.files.models import Document, Folder
from apps.projects.models import Project, Task


def upload(name, content=b"%PDF-1.4 test"):
    return SimpleUploadedFile(name, content)


class FileLibraryTests(TestCase):
    def setUp(self):
        self.member = make_user("member")
        self.outsider = make_user("outsider")
        self.viewer = make_user("viewer", role=User.Role.VIEWER)
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.member, self.viewer)

    def up(self, client, name, content=b"data", space="PWR", folder=None, **extra):
        url = reverse("files:upload", args=[space, folder.pk]) if folder else reverse("files:upload_root", args=[space])
        return client.post(url, {"files": [upload(name, content)]}, **extra)

    def test_upload_versions_and_download(self):
        c = signed_in(self.member)
        self.up(c, "report.pdf", b"%PDF-1.4 one")
        self.up(c, "report.pdf", b"%PDF-1.4 two!")
        doc = Document.objects.get()
        self.assertEqual((doc.version_count, doc.size), (2, 13))
        latest = doc.latest
        self.assertTrue(latest.file.name.startswith(f"PWR/{doc.pk}/v2-report"))
        r = c.get(reverse("files:download", args=[latest.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 two!")
        first = doc.versions.get(number=1)
        self.assertEqual(b"".join(c.get(reverse("files:download", args=[first.pk])).streaming_content), b"%PDF-1.4 one")

    def test_inline_preview_is_sandboxed(self):
        c = signed_in(self.member)
        self.up(c, "scope.png", b"\x89PNG....")
        v = Document.objects.get().latest
        r = c.get(reverse("files:download", args=[v.pk]) + "?inline=1")
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
        self.assertNotIn("attachment", r["Content-Disposition"])

    def test_access_control(self):
        self.up(signed_in(self.member), "secret.pdf")
        doc = Document.objects.get()
        self.assertEqual(signed_in(self.outsider).get(doc.get_absolute_url()).status_code, 404)
        self.assertEqual(signed_in(self.outsider).get(reverse("files:download", args=[doc.latest.pk])).status_code, 404)
        self.assertEqual(signed_in(self.outsider).get(reverse("files:space", args=["PWR"])).status_code, 403)
        self.assertEqual(self.up(signed_in(self.viewer), "x.txt").status_code, 403)
        self.assertNotContains(signed_in(self.outsider).get("/search/?q=secret"), "secret.pdf")

    def test_shared_space_visible_to_everyone(self):
        self.up(signed_in(self.member), "policy.pdf", space="shared")
        doc = Document.objects.get(project__isnull=True)
        self.assertEqual(signed_in(self.outsider).get(doc.get_absolute_url()).status_code, 200)

    def test_blocked_extensions(self):
        c = signed_in(self.member)
        for name in ("evil.html", "run.exe", "x.js", "a.svg.html"):
            self.up(c, name, b"<script>")
        self.assertFalse(Document.objects.exists())

    def test_folders_trash_restore_purge(self):
        c = signed_in(self.member)
        c.post(reverse("files:folder_create_root", args=["PWR"]), {"name": "Datasheets"})
        folder = Folder.objects.get(name="Datasheets")
        self.up(c, "ds.pdf", folder=folder)
        doc = Document.objects.get()
        self.assertEqual(doc.folder, folder)
        c.post(reverse("files:folder_action", args=["PWR", folder.pk]), {"action": "delete"})
        doc.refresh_from_db()
        self.assertIsNotNone(doc.deleted_at)
        self.assertIsNone(doc.folder)
        self.assertContains(c.get(reverse("files:trash")), "ds.pdf")
        c.post(reverse("files:trash_action", args=[doc.pk]), {"action": "restore"})
        doc.refresh_from_db()
        self.assertIsNone(doc.deleted_at)
        doc.trash(self.member)
        self.assertEqual(c.post(reverse("files:trash_action", args=[doc.pk]), {"action": "purge"}).status_code, 403)
        path = doc.latest.file.path
        signed_in(self.lead).post(reverse("files:trash_action", args=[doc.pk]), {"action": "purge"})
        self.assertFalse(Document.objects.exists())
        import os
        self.assertFalse(os.path.exists(path))

    def test_task_attachments_go_to_library(self):
        task = Task.objects.create(project=self.project, title="ESD")
        signed_in(self.member).post(reverse("projects:task_attach", args=["PWR", task.number]), {"file": upload("scope.png")})
        doc = Document.objects.get()
        self.assertEqual(doc.task, task)
        self.assertEqual([f.name for f in doc.folder.ancestors()] + [doc.folder.name], ["Task files", "PWR-1"])
        self.assertContains(signed_in(self.member).get(task.get_absolute_url()), "scope.png")

    def test_storage_page_admin_only_and_prune(self):
        c = signed_in(self.member)
        for i in range(5):
            self.up(c, "log.txt", f"v{i}".encode())
        self.assertEqual(c.get(reverse("files:storage")).status_code, 403)
        admin = signed_in(make_user("boss", role=User.Role.ADMIN))
        self.assertContains(admin.get(reverse("files:storage")), "log.txt")
        admin.post(reverse("files:prune_versions"), {"keep": 2})
        self.assertEqual(Document.objects.get().versions.count(), 2)
