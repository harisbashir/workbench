import io
import zipfile

from django.test import TestCase
from django.urls import reverse

from apps.core.testing import signed_in
from apps.projects.models import default_board
from apps.sources.models import Repository

from .helpers import commit, make_repo


class BrowseTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        commit(self.f.repo, self.f.eng, message="First version",
               **{"src/main.c": "int main(void) {\n  return 0;\n}\n", "README.md": "# Power firmware\n\nBuild with **make**.\n",
                  "docs/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary", "fw.bin": b"\x00\x01\x02\x03", "icon.svg": "<svg/>"})
        commit(self.f.repo, self.f.eng2, message="Second version", **{"src/main.c": "int main(void) {\n  return 1;\n}\n"})
        self.c = signed_in(self.f.eng)

    def url(self, name, *args):
        return reverse(f"sources:{name}", args=["PWR", self.f.repo.slug, *args])

    def test_project_page_lists_repos(self):
        page = self.c.get(reverse("sources:project", args=["PWR"])).content.decode()
        self.assertIn("Main firmware", page)
        self.assertNotIn("Other code", page)
        page = self.c.get(reverse("sources:overview")).content.decode()
        self.assertIn("Main firmware", page)
        self.assertNotIn("Other code", page)

    def test_repo_page_shows_tree_and_history(self):
        page = self.c.get(self.f.repo.get_absolute_url()).content.decode()
        for text in ("src", "README.md", "fw.bin", "Second version", "First version", "v2", "Power firmware"):
            self.assertIn(text, page)
        old = self.c.get(self.f.repo.get_absolute_url() + "?v=1").content.decode()
        self.assertIn("v1", old)

    def test_folder_navigation(self):
        page = self.c.get(self.f.repo.get_absolute_url() + "?path=src/").content.decode()
        self.assertIn("main.c", page)
        self.assertNotIn("fw.bin", page)

    def test_text_file_highlighted(self):
        r = self.c.get(self.url("file", 2, "src/main.c"))
        self.assertContains(r, '<span class="')
        self.assertContains(r, 'id="L2"')
        self.assertContains(r, "return")
        self.assertContains(self.c.get(self.url("file", 1, "src/main.c")), "0")

    def test_binary_file_view(self):
        r = self.c.get(self.url("file", 2, "fw.bin"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Download")

    def test_raw_download_headers(self):
        r = self.c.get(self.url("file", 2, "fw.bin") + "?raw=1")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        r = self.c.get(self.url("file", 2, "icon.svg") + "?raw=1&inline=1")
        self.assertIn("attachment", r["Content-Disposition"])
        r = self.c.get(self.url("file", 2, "docs/logo.png") + "?raw=1&inline=1")
        self.assertIn("inline", r["Content-Disposition"])
        self.assertEqual(r["Content-Type"], "image/png")

    def test_missing_file_and_version_404(self):
        self.assertEqual(self.c.get(self.url("file", 2, "nope.c")).status_code, 404)
        self.assertEqual(self.c.get(self.url("file", 9, "src/main.c")).status_code, 404)

    def test_version_zip_contains_tree(self):
        r = self.c.get(self.url("version_zip", 1))
        z = zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content)))
        self.assertIn("src/main.c", z.namelist())
        self.assertIn(b"return 0", z.read("src/main.c"))

    def test_permissions_matrix(self):
        out = signed_in(self.f.outsider)
        for u in (reverse("sources:project", args=["PWR"]), self.f.repo.get_absolute_url(), self.url("file", 2, "src/main.c"),
                  self.url("version_zip", 2), self.url("repo_edit"), reverse("sources:repo_create", args=["PWR"])):
            self.assertIn(out.get(u).status_code, (403, 404), u)
        viewer = signed_in(self.f.viewer)
        self.assertEqual(viewer.get(self.f.repo.get_absolute_url()).status_code, 200)
        self.assertEqual(viewer.get(self.url("file", 2, "src/main.c")).status_code, 200)
        self.assertIn(viewer.get(reverse("sources:repo_create", args=["PWR"])).status_code, (403, 404))
        self.assertIn(viewer.post(self.url("repo_edit"), {"name": "x"}).status_code, (403, 404))
        self.assertEqual(self.c.post(self.url("repo_delete")).status_code, 403)
        page = viewer.get(self.f.repo.get_absolute_url()).content.decode()
        self.assertNotIn(self.url("repo_edit"), page)
        r = self.c.get(reverse("sources:repo", args=["PWR", self.f.other_repo.slug]))
        self.assertEqual(r.status_code, 404)

    def test_create_repo_for_board(self):
        r = self.c.post(reverse("sources:repo_create", args=["PWR"]),
                        {"name": "Power board KiCad", "kind": "kicad", "board": self.f.board.pk, "description": ""})
        repo = Repository.objects.get(name="Power board KiCad")
        self.assertRedirects(r, repo.get_absolute_url(), fetch_redirect_response=False)
        other_board = default_board(self.f.other)
        r = self.c.post(reverse("sources:repo_create", args=["PWR"]), {"name": "Sneaky", "kind": "other", "board": other_board.pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Repository.objects.filter(name="Sneaky").exists())

    def test_manager_deletes_repo(self):
        r = signed_in(self.f.lead).post(self.url("repo_delete"))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Repository.objects.filter(pk=self.f.repo.pk).exists())

    def test_board_page_lists_repositories(self):
        page = self.c.get(reverse("projects:hw_board", args=["PWR", self.f.board.pk])).content.decode()
        self.assertIn(self.f.repo.get_absolute_url(), page)
