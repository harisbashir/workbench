from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources import blobs, diff

from .helpers import commit, make_repo


def b(text):
    return blobs.store_bytes(text.encode() if isinstance(text, str) else text)


class DiffTests(TestCase):
    def test_changes_lists_added_changed_deleted(self):
        old = {"a.c": b("1"), "b.c": b("1"), "c.c": b("1")}
        new = {"a.c": old["a.c"], "b.c": b("2"), "d.c": b("1")}
        self.assertEqual([(c["path"], c["status"]) for c in diff.changes(old, new)],
                         [("b.c", "changed"), ("c.c", "deleted"), ("d.c", "added")])

    def test_text_diff_rows_and_counts(self):
        d = diff.file_diff("m.c", b("int a;\nint b;\n"), b("int a;\nint c;\nint d;\n"))
        self.assertEqual(d["kind"], "text")
        self.assertEqual((d["added"], d["removed"]), (2, 1))
        tags = [r[0] for r in d["rows"]]
        self.assertEqual(tags, ["same", "del", "add", "add"])
        self.assertEqual(d["rows"][1][1:3], (2, None))
        self.assertEqual(d["rows"][3][1:3], (None, 3))
        self.assertIn("span", str(d["rows"][0][3]))
        self.assertEqual(len(d["split"]), 3)

    def test_unchanged_runs_folded(self):
        old = "".join(f"line {i}\n" for i in range(100))
        new = old.replace("line 50\n", "LINE 50\n")
        rows = diff.file_diff("x.txt", b(old), b(new))["rows"]
        folds = [r for r in rows if r[0] == "fold"]
        self.assertEqual(len(folds), 2)
        self.assertIn("47 unchanged lines", str(folds[0][3]))
        self.assertLess(len(rows), 20)

    def test_added_and_deleted_files(self):
        self.assertEqual(diff.file_diff("n.c", None, b("a\nb\n"))["added"], 2)
        self.assertEqual(diff.file_diff("n.c", b("a\n"), None)["removed"], 1)

    def test_binary_not_diffed(self):
        self.assertEqual(diff.file_diff("fw.bin", b(b"\x00\x01"), b(b"\x00\x02"))["kind"], "binary")

    def test_large_file_not_diffed(self):
        with mock.patch.object(diff, "DIFF_LINES", 5):
            d = diff.file_diff("x.txt", b("1\n" * 10), b("2\n" * 10))
        self.assertEqual(d["kind"], "too_large")


class CompareViewTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        commit(self.f.repo, self.f.eng, message="One", **{"src/main.c": "int a;\nint b;\n", "gone.c": "x\n"})
        commit(self.f.repo, self.f.eng, mode="replace", message="Two", **{"src/main.c": "int a;\nint c;\n", "new.h": "#pragma once\n"})
        self.url = reverse("sources:compare", args=["PWR", self.f.repo.slug])

    def test_compare_page(self):
        c = signed_in(self.f.viewer)
        page = c.get(self.url).content.decode()
        for text in ("src/main.c", "gone.c", "new.h", "+2", "−2", "int", "v1", "v2"):
            self.assertIn(text, page)
        self.assertEqual(c.get(self.url + "?a=1&b=2&view=split").status_code, 200)
        self.assertEqual(c.get(self.url + "?a=0&b=1").status_code, 200)
        self.assertEqual(c.get(self.url + "?a=1&b=7").status_code, 404)
        self.assertIn(signed_in(self.f.outsider).get(self.url).status_code, (403, 404))

    def test_history_links_to_changes(self):
        page = signed_in(self.f.eng).get(self.f.repo.get_absolute_url()).content.decode()
        self.assertIn(self.url + "?a=1&amp;b=2", page)
