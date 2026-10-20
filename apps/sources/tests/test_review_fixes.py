"""Tests for the problems found in the 1.6.0 review."""
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.testing import signed_in
from apps.firmware.models import Firmware
from apps.sources import blobs, checkin, diff, intake
from apps.sources.models import Blob, Draft

from .helpers import commit, files, make_repo


class BlobRaceTests(TestCase):
    def test_losing_writer_keeps_shared_file(self):
        winner = blobs.store_bytes(b"shared content")
        loser = Blob(sha256=winner.sha256)
        loser.file.name = winner.file.name   # S3 overwrites the same key
        blobs._discard(loser, winner)
        self.assertTrue(winner.file.storage.exists(winner.file.name))

    def test_losing_writer_removes_its_own_copy(self):
        winner = blobs.store_bytes(b"shared content 2")
        loser = blobs.store_bytes(b"something else")
        name = loser.file.name
        Blob.objects.filter(pk=loser.pk).delete()
        blobs._discard(loser, winner)
        self.assertFalse(winner.file.storage.exists(name))

    def test_reuse_refreshes_age(self):
        b = blobs.store_bytes(b"old orphan")
        Blob.objects.filter(pk=b.pk).update(created_at=timezone.now() - timedelta(days=5))
        blobs.store_bytes(b"old orphan")
        b.refresh_from_db()
        self.assertGreater(b.created_at, timezone.now() - timedelta(minutes=1))

    def test_missing_file_is_restored_on_reuse(self):
        b = blobs.store_bytes(b"heal me")
        b.file.storage.delete(b.file.name)
        again = blobs.store_bytes(b"heal me")
        self.assertTrue(again.file.storage.exists(again.file.name))
        self.assertEqual(again.read(), b"heal me")


class DiffLimitTests(TestCase):
    def test_slow_diff_gives_too_large(self):
        a = blobs.store_bytes("".join(f"line {i}\n" for i in range(3000)).encode())
        b = blobs.store_bytes("".join(f"line {i}{'x' if i % 2 else ''}\n" for i in range(3000)).encode())
        with mock.patch.object(diff, "DIFF_SECONDS", 0.0):
            self.assertEqual(diff.file_diff("x.txt", a, b)["kind"], "too_large")
            self.assertEqual(diff.count_lines(a, b), (0, 0))

    def test_page_budget(self):
        budget = diff.Budget(seconds=0.0)
        a, b = blobs.store_bytes(b"1\n2\n"), blobs.store_bytes(b"1\n3\n")
        self.assertEqual(diff.file_diff("x.txt", a, b, budget=budget)["kind"], "too_large")


class ViewerLimitTests(TestCase):
    def test_big_plain_file_not_rendered(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"big.txt": "a\n" * 50})
        from apps.sources import highlight
        with mock.patch.object(highlight, "HIGHLIGHT_LINES", 10):
            r = signed_in(f.eng).get(reverse("sources:file", args=["PWR", f.repo.slug, 1, "big.txt"]))
        self.assertContains(r, "too large to show here")
        self.assertNotContains(r, 'id="L2"')

    def test_plain_fallback_line_count(self):
        from apps.sources import highlight
        with mock.patch.object(highlight, "HIGHLIGHT_BYTES", 1):
            lines, _ = highlight.render("x.txt", "a\r\nb\r\nc\r\n")
        self.assertEqual([str(x) for x in lines], ["a", "b", "c"])


class CleanupRaceTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        commit(self.f.repo, self.f.eng, **{"a.c": "1", "b.c": "1"})

    def test_missing_blob_in_draft_is_refused_not_deleted(self):
        good = blobs.store_bytes(b"2")
        draft = Draft.objects.create(repository=self.f.repo, user=self.f.eng, base_number=1, mode="replace",
                                     files={"a.c": 999999, "b.c": good.pk})
        url = reverse("sources:checkin_preview", args=["PWR", self.f.repo.slug, draft.pk])
        r = signed_in(self.f.eng).post(url, {"message": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "upload it again")
        signed_in(self.f.eng).post(url, {"message": "x", "allow_partial": "1"})
        self.assertIn("a.c", checkin.tree(self.f.repo.latest()))


class NameClashTests(TestCase):
    def test_case_and_file_folder_clashes_refused(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"src/main.c": "1", "docs": "x"})
        plan = checkin.plan(f.repo, files(**{"SRC/main.c": "2", "docs/a.md": "y", "Src/new.c": "z", "src/main.c/x": "w"}),
                            "update", 1, f.eng)
        self.assertEqual(sorted(p for p, _ in plan.refused), ["SRC/main.c", "Src/new.c", "docs/a.md", "src/main.c/x"])
        self.assertEqual(sorted(plan.new_tree), ["docs", "src/main.c"])

    def test_replacing_with_different_case_is_fine(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"readme.md": "1"})
        plan = checkin.plan(f.repo, files(**{"README.md": "1"}), "replace", 1, f.eng)
        self.assertEqual((plan.refused, plan.added, plan.deleted), ([], ["README.md"], ["readme.md"]))


class FileTypeTests(TestCase):
    def test_scripts_and_web_files_allowed(self):
        r = intake.take([(n, SimpleUploadedFile(n, b"x")) for n in ("build.sh", "web/index.html", "web/app.js", "tool.exe")])
        self.assertEqual(sorted(r.files), ["build.sh", "web/app.js", "web/index.html"])
        self.assertEqual([p for p, _ in r.refused], ["tool.exe"])

    def test_html_never_served_inline(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"index.html": "<script>alert(1)</script>"})
        c = signed_in(f.eng)
        r = c.get(reverse("sources:file", args=["PWR", f.repo.slug, 1, "index.html"]) + "?raw=1&inline=1")
        self.assertEqual(r["Content-Type"], "application/octet-stream")
        page = c.get(reverse("sources:file", args=["PWR", f.repo.slug, 1, "index.html"])).content.decode()
        self.assertNotIn("<script>alert", page)


class SmallFixTests(TestCase):
    def test_control_and_bidi_characters_refused(self):
        for bad in ("a\nb.c", "x\x07.c", "‮gpj.exe", "a‏b.c"):
            self.assertIsNone(intake.normalise_path(bad), repr(bad))

    def test_bad_into_folder_refused(self):
        f = make_repo()
        url = reverse("sources:checkin", args=["PWR", f.repo.slug])
        r = signed_in(f.eng).post(url, {"files": [SimpleUploadedFile("a.c", b"1")], "into": "../evil", "mode": "update", "base": 0},
                                  follow=True)
        self.assertContains(r, "folder name")
        self.assertFalse(Draft.objects.exists())

    def test_promote_double_click_no_error(self):
        f = make_repo()
        f.repo.firmware = Firmware.objects.create(project=f.p, name="App")
        f.repo.save()
        commit(f.repo, f.eng, **{"a.c": "1"})
        url = reverse("sources:promote", args=["PWR", f.repo.slug, 1])
        with mock.patch("apps.sources.views.FirmwareRelease.objects.create", side_effect=IntegrityError):
            r = signed_in(f.eng).post(url, {"version": "1.0.0"}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already exists")

    def test_lock_race_no_error(self):
        f = make_repo()
        commit(f.repo, f.eng, **{"a.c": "1"})
        with mock.patch("apps.sources.checkin.Lock.objects.create", side_effect=IntegrityError):
            self.assertIn("try again", checkin.lock(f.repo, "a.c", f.eng).lower())
