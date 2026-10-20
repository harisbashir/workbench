"""Tests for the smaller problems left over from the 1.6.0 review."""
import io
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.testing import signed_in
from apps.projects.models import Controller, Revision
from apps.sources import intake
from apps.sources.models import Repository

from .helpers import commit, make_repo


def count_queries(fn):
    """Database queries `fn` makes, not counting the storage check that tests repeat on every file opened."""
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return sum(1 for q in ctx.captured_queries if "core_storagesettings" not in q["sql"])


def zip_of(n):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(n):
            z.writestr(f"f{i}.txt", "x")
    return SimpleUploadedFile("many.zip", buf.getvalue())


class SlugRaceTests(TestCase):
    def test_same_name_created_at_once_gets_next_slug(self):
        f = make_repo()
        with mock.patch.object(Repository, "_slug_taken", return_value=False):  # the other request hasn't committed yet
            repo = Repository.objects.create(project=f.p, name="Main firmware")
        self.assertEqual(repo.slug, "main-firmware-2")


class ZipEntryLimitTests(TestCase):
    def test_zip_with_too_many_entries_is_refused_before_reading(self):
        upload = zip_of(6)
        with mock.patch.object(intake, "MAX_ZIP_ENTRIES", 5), mock.patch.object(intake.zipfile, "ZipFile") as opened:
            result = intake.take([("many.zip", upload)])
        opened.assert_not_called()
        self.assertEqual(result.files, {})
        self.assertIn("too many files", result.refused[0][1])

    def test_zip_under_limit_still_unpacks(self):
        with mock.patch.object(intake, "MAX_ZIP_ENTRIES", 5):
            result = intake.take([("many.zip", zip_of(5))])
        self.assertEqual(len(result.files), 5)

    def test_zip64_entry_count_is_read(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", allowZip64=True) as z:
            for i in range(3):
                z.writestr(f"f{i}.txt", "x")
        data = buf.getvalue()
        self.assertEqual(intake.zip_entry_count(io.BytesIO(data)), 3)
        self.assertIsNone(intake.zip_entry_count(io.BytesIO(b"not a zip at all")))


class FolderWithoutScriptTests(TestCase):
    def test_folder_picker_hidden_until_script_runs(self):
        f = make_repo()
        page = signed_in(f.eng).get(reverse("sources:checkin", args=["PWR", f.repo.slug])).content.decode()
        start = page.index("data-folder-field")
        self.assertIn("hidden", page[page.rindex("<div", 0, start):start + 40])
        self.assertIn("<noscript>", page)


class ExportNameTests(TestCase):
    def test_repositories_with_the_same_name_get_separate_folders(self):
        from apps.core.export import _repo_folders
        f = make_repo()
        twin = Repository.objects.create(project=f.p, name="Main firmware")
        lone = Repository.objects.create(project=f.p, name="Docs")
        names = _repo_folders([f.repo, twin, lone])
        self.assertEqual(names[lone.pk], "Docs")
        self.assertEqual(len({names[f.repo.pk], names[twin.pk]}), 2)
        self.assertIn(twin.slug, names[twin.pk])


class DeleteLinkedRepoTests(TestCase):
    def setUp(self):
        self.f = make_repo(kind="kicad")
        self.v = commit(self.f.repo, self.f.eng, **{"pwr.kicad_sch": "(kicad_sch)"})
        self.rev = Revision.objects.create(project=self.f.p, board=self.f.board, name="Rev A", source_version=self.v)
        self.c = signed_in(self.f.lead)
        self.url = reverse("sources:repo_delete", args=["PWR", self.f.repo.slug])

    def test_released_revision_blocks_delete(self):
        Revision.objects.filter(pk=self.rev.pk).update(status=Revision.Status.RELEASED)
        r = self.c.post(self.url, follow=True)
        self.assertTrue(Repository.objects.filter(pk=self.f.repo.pk).exists())
        self.assertContains(r, "Rev A")

    def test_unreleased_link_is_named_in_the_warning(self):
        page = self.c.get(reverse("sources:repo_edit", args=["PWR", self.f.repo.slug])).content.decode()
        self.assertIn("Rev A will no longer", page)
        self.c.post(self.url)
        self.assertFalse(Repository.objects.filter(pk=self.f.repo.pk).exists())


class SourcesQueryTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        self.c = signed_in(self.f.eng)

    def add_repos(self, n):
        for i in range(n):
            ctrl = Controller.objects.create(board=self.f.board, designator=f"U{i + 2}", name=f"MCU {i}")
            Repository.objects.create(project=self.f.p, board=self.f.board, controller=ctrl, name=f"Code {i}")

    def steady(self, url, grow):
        self.c.get(url)  # the first request also does one-off session work
        few = count_queries(lambda: self.c.get(url))
        grow()
        many = count_queries(lambda: self.assertEqual(self.c.get(url).status_code, 200))
        self.assertEqual(many, few)

    def test_overview(self):
        self.steady(reverse("sources:overview"), lambda: self.add_repos(4))

    def test_project_page(self):
        self.steady(reverse("sources:project", args=["PWR"]), lambda: self.add_repos(4))

    def test_repo_page_with_many_files_and_locks(self):
        commit(self.f.repo, self.f.eng, **{"a.c": "1"})
        url = reverse("sources:repo", args=["PWR", self.f.repo.slug])
        self.steady(url, lambda: commit(self.f.repo, self.f.eng, **{f"f{i}.c": str(i) for i in range(10)}))

    def test_compare_many_files(self):
        commit(self.f.repo, self.f.eng, **{"a.c": "1"})
        commit(self.f.repo, self.f.eng, **{"a.c": "2"})
        url = reverse("sources:compare", args=["PWR", self.f.repo.slug])

        def grow():
            commit(self.f.repo, self.f.eng, **{f"f{i}.c": "1" for i in range(10)})
            commit(self.f.repo, self.f.eng, **{f"f{i}.c": "2" for i in range(10)})
        self.steady(url, grow)


class WindowsNameTests(TestCase):
    """Names Windows can't save are refused, so a version can always be downloaded and opened there."""
    def test_names_windows_cannot_save_are_refused(self):
        bad = ["src/a<b>.c", 'q"uote.txt', "pipe|.txt", "what?.txt", "star*.txt", "colon:.txt", "dot.",
               "con.txt", "NUL", "dir/aux.h", "lpt1.c", "COM9.txt", "trail./x.c"]
        result = intake.take([(name, SimpleUploadedFile("x", b"1")) for name in bad])
        self.assertEqual(result.files, {})
        self.assertEqual(len(result.refused), len(bad))
        self.assertTrue(all("Windows" in reason for _, reason in result.refused), result.refused)

    def test_ordinary_names_still_allowed(self):
        good = ["console.c", "nul_driver.h", "a.b.c", "x-y_z (1).txt", "Ünïcode.md", "com10.txt"]
        result = intake.take([(name, SimpleUploadedFile("x", name.encode())) for name in good])
        self.assertEqual(sorted(result.files), sorted(good))

    def test_stale_message_does_not_blame_someone_else(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"a.c": "1"})
        commit(f.repo, f.eng, **{"a.c": "2"})
        from apps.sources import checkin
        from .helpers import files
        plan = checkin.plan(f.repo, files(**{"a.c": "3"}), "update", 1, f.eng)
        reason = dict(plan.refused)["a.c"]
        self.assertNotIn("someone else", reason)
        self.assertIn("v1", reason)
