import io
import stat
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.sources import intake


def zipped(entries):
    """entries: [(name, data)] or [(ZipInfo, data)]"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def up(name, data):
    return SimpleUploadedFile(name, data)


class PathTests(TestCase):
    def test_paths_normalised_and_refused(self):
        n = intake.normalise_path
        self.assertEqual(n("src\\main.c"), "src/main.c")
        self.assertEqual(n("./src//main.c"), "src/main.c")
        self.assertEqual(n("/etc/passwd"), "etc/passwd")
        for bad in ("../x", "a/../b", "C:/x", "a\x00b", "", "/", "x" * 401, "a/./"):
            self.assertIsNone(n(bad), bad)

    def test_ignored_files_dropped(self):
        for p in (".git/config", "board-backups/board-2026.zip", "fp-info-cache", "board.kicad_prl",
                  "_autosave-board.kicad_sch", "~board.kicad_sch.lck", "x/.DS_Store", "Thumbs.db",
                  "src/__pycache__/a.pyc", "__MACOSX/._a"):
            self.assertTrue(intake.is_ignored(p), p)
        self.assertFalse(intake.is_ignored("src/main.c"))


class TakeTests(TestCase):
    def test_loose_files_with_paths_and_folder(self):
        r = intake.take([("src/main.c", up("main.c", b"int x;\n")), ("README.md", up("README.md", b"# Hi\n"))], into="fw")
        self.assertEqual(sorted(r.files), ["fw/README.md", "fw/src/main.c"])
        self.assertEqual(r.files["fw/src/main.c"].read(), b"int x;\n")

    def test_zip_expanded_with_paths(self):
        r = intake.take([("code.zip", up("code.zip", zipped([("proj/src/a.c", b"a"), ("proj/.git/HEAD", b"ref")])))])
        self.assertEqual(list(r.files), ["proj/src/a.c"])
        self.assertEqual(r.ignored, 1)

    def test_zip_kept_when_asked(self):
        r = intake.take([("code.zip", up("code.zip", zipped([("a.c", b"a")])))], keep_zips=True)
        self.assertEqual(list(r.files), ["code.zip"])

    def test_zip_with_traversal_entry_refused(self):
        r = intake.take([("x.zip", up("x.zip", zipped([("../../evil.c", b"x"), ("ok.c", b"y")])))])
        self.assertEqual(list(r.files), ["ok.c"])
        self.assertEqual(len(r.refused), 1)

    def test_zip_bomb_refused(self):
        r = intake.take([("bomb.zip", up("bomb.zip", zipped([("big.txt", b"\x00" * 2_000_000)])))])
        self.assertEqual(r.files, {})
        self.assertIn("compressed", r.refused[0][1])

    def test_symlink_in_zip_refused(self):
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        r = intake.take([("l.zip", up("l.zip", zipped([(info, b"/etc/passwd")])))])
        self.assertEqual(r.files, {})
        self.assertEqual(len(r.refused), 1)

    def test_blocked_extension_refused(self):
        r = intake.take([("tool.exe", up("tool.exe", b"MZ")), ("page.html", up("page.html", b"<p>"))])
        self.assertEqual(r.files, {})
        self.assertEqual(len(r.refused), 2)

    def test_duplicate_paths_case_insensitive(self):
        r = intake.take([("Main.c", up("Main.c", b"1")), ("main.c", up("main.c", b"2"))])
        self.assertEqual(list(r.files), ["main.c"])
        self.assertIn("twice", r.refused[0][1])

    def test_too_many_files(self):
        with mock.patch.object(intake, "MAX_FILES", 3):
            r = intake.take([(f"f{i}.c", up(f"f{i}.c", str(i).encode())) for i in range(5)])
        self.assertEqual(len(r.files), 3)
        self.assertTrue(any("5,000" in reason or "files" in reason for _, reason in r.refused))

    def test_total_size_limit(self):
        with mock.patch.object(intake, "total_limit", return_value=10):
            r = intake.take([("a.c", up("a.c", b"123456")), ("b.c", up("b.c", b"123456"))])
        self.assertEqual(list(r.files), ["a.c"])
        self.assertIn("too large", r.refused[0][1])
