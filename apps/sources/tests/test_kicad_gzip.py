"""Drawings are kept compressed beside the plain copy and sent compressed to browsers that accept it."""
import gzip
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources import checkin
from apps.sources.kicad import render

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"
PCB = (FIX / "v8_board.kicad_pcb").read_text()
SCH = (FIX / "v8_hier.kicad_sch").read_text()


def body(r):
    return b"".join(r.streaming_content) if r.streaming else r.content


class CompressedDrawingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_pcb": PCB, "pwr.kicad_sch": SCH})
        checkin.lock(self.f.repo, "pwr.kicad_pcb", self.f.eng)
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_pcb": PCB.replace("(at 10 15 90)", "(at 12 15 90)")})
        self.c = signed_in(self.f.eng)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def url(self, path, v=1):
        return reverse("sources:kicad_svg", args=["PWR", self.f.repo.slug, v, path])

    def test_sent_compressed_when_accepted(self):
        plain = body(self.c.get(self.url("pwr.kicad_pcb")))
        r = self.c.get(self.url("pwr.kicad_pcb"), HTTP_ACCEPT_ENCODING="gzip, deflate, br")
        self.assertEqual(r["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", r["Vary"])
        self.assertTrue(r["Content-Type"].startswith("image/svg+xml"))
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertEqual(gzip.decompress(body(r)), plain)
        self.assertLess(len(body(r)), len(plain))
        self.assertFalse(r.has_header("Content-Disposition"))

    def test_plain_when_not_accepted(self):
        r = self.c.get(self.url("pwr.kicad_sch"))
        self.assertFalse(r.has_header("Content-Encoding"))
        self.assertTrue(body(r).startswith(b"<svg"))

    def test_highlighted_drawings_too(self):
        r = self.c.get(self.url("pwr.kicad_pcb", 2) + "?compare=1", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(r["Content-Encoding"], "gzip")
        self.assertIn(b"hl-changed", gzip.decompress(body(r)))

    def test_missing_compressed_copy_falls_back(self):
        self.c.get(self.url("pwr.kicad_sch"))
        for p in render.cache_dir().glob("*.gz"):
            p.unlink()
        r = self.c.get(self.url("pwr.kicad_sch"), HTTP_ACCEPT_ENCODING="gzip")
        self.assertTrue(body(r).startswith(b"<svg") or gzip.decompress(body(r)).startswith(b"<svg"))

    def test_page_facts_do_not_read_the_drawing(self):
        self.c.get(self.url("pwr.kicad_sch"))
        meta = render.render_blob(self.f.repo.latest().files.get(path="pwr.kicad_sch").blob, "pwr.kicad_sch", svg="path")
        self.assertNotIn("svg", meta)
        self.assertTrue(Path(meta["svg_path"]).exists())

    def test_prune_removes_compressed_copies(self):
        self.c.get(self.url("pwr.kicad_sch"))
        self.assertTrue(list(render.cache_dir().glob("*.svg.gz")))
        with mock.patch.object(render, "MAX_CACHE_BYTES", 0):
            render.prune()
        self.assertEqual([p for p in render.cache_dir().iterdir() if p.is_file()], [])

    def test_large_drawings_are_compressed_too(self):
        with mock.patch.object(render, "MAX_BYTES", 1000):
            blob = self.f.repo.latest().files.get(path="pwr.kicad_sch").blob
            render.render_blob(blob, "pwr.kicad_sch")
            render._draw_wanted(block=True)
            meta = render.render_blob(blob, "pwr.kicad_sch", svg="path")
        self.assertTrue(meta["ok"], meta)
        self.assertTrue(Path(meta["svg_path"] + ".gz").exists())
        self.assertEqual(gzip.decompress(Path(meta["svg_path"] + ".gz").read_bytes()), Path(meta["svg_path"]).read_bytes())

    def test_drawings_from_before_the_update_are_compressed_by_the_scheduler(self):
        self.c.get(self.url("pwr.kicad_sch"))
        self.c.get(self.url("pwr.kicad_pcb"))
        for p in render.cache_dir().glob("*.gz"):
            p.unlink()  # as drawn by 1.8.0
        self.assertEqual(render.compress_missing(), 2)
        r = self.c.get(self.url("pwr.kicad_sch"), HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(r["Content-Encoding"], "gzip")
        self.assertTrue(gzip.decompress(body(r)).startswith(b"<svg"))
        self.assertEqual(render.compress_missing(), 0)  # only once

    def test_compressing_old_drawings_stops_when_the_time_is_up(self):
        self.c.get(self.url("pwr.kicad_sch"))
        self.c.get(self.url("pwr.kicad_pcb"))
        for p in render.cache_dir().glob("*.gz"):
            p.unlink()
        self.assertEqual(render.compress_missing(seconds=0), 0)
        self.assertEqual(render.compress_missing(), 2)

    def test_scheduler_compresses_old_drawings(self):
        self.c.get(self.url("pwr.kicad_sch"))
        for p in render.cache_dir().glob("*.gz"):
            p.unlink()
        render.prerender()
        meta = render.render_blob(self.f.repo.latest().files.get(path="pwr.kicad_sch").blob, "pwr.kicad_sch", svg="path")
        self.assertTrue(Path(meta["svg_path"] + ".gz").exists())
