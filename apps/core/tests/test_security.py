from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.projects.models import Project, Task


class HeaderTests(TestCase):
    def test_security_headers(self):
        r = self.client.get(reverse("accounts:login"))
        self.assertIn("default-src 'self'", r["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_signed_in_pages_are_not_cached(self):
        c = signed_in(make_user("eng"))
        self.assertEqual(c.get(reverse("core:dashboard"))["Cache-Control"], "no-store")


@override_settings(MEDIA_ROOT="/tmp/workbench-test-media")
class FileAccessTests(TestCase):
    def setUp(self):
        self.member = make_user("member")
        self.outsider = make_user("outsider")
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.member)
        self.task = Task.objects.create(project=self.project, title="Schematic")

    def upload(self, name, content=b"%PDF-1.4 test"):
        return signed_in(self.member).post(reverse("projects:task_attach", args=["PWR", self.task.number]),
                                           {"file": SimpleUploadedFile(name, content)})

    def test_members_only_download(self):
        self.upload("schematic.pdf")
        att = self.task.attachments.get()
        self.assertEqual(signed_in(self.member).get(reverse("core:file", args=[att.pk])).status_code, 200)
        self.assertEqual(signed_in(self.outsider).get(reverse("core:file", args=[att.pk])).status_code, 404)

    def test_dangerous_file_types_rejected(self):
        self.upload("evil.html", b"<script>alert(1)</script>")
        self.upload("run.exe", b"MZ")
        self.assertEqual(self.task.attachments.count(), 0)

    def test_every_page_renders_for_every_role(self):
        """Smoke test: the main pages load without errors for each role."""
        for role in User.Role.values:
            c = signed_in(make_user(f"u-{role}", role=role))
            for url in ["/", "/my-work/", "/projects/", "/chat/", "/parts/", "/parts/boms/", "/parts/suppliers/",
                        "/production/", "/production/orders/", "/integrations/", "/help/", "/help/github/",
                        "/notifications/", "/search/?q=pwr", "/accounts/profile/", "/accounts/security/"]:
                r = c.get(url, follow=True)
                self.assertEqual(r.status_code, 200, f"{role} {url}")
