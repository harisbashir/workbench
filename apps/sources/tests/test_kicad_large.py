"""Large KiCad files are drawn by the scheduler in a separate, resource-limited process."""
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources.kicad import render

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"
PCB = (FIX / "v8_board.kicad_pcb").read_text()
SCH = (FIX / "v8_hier.kicad_sch").read_text()


class LargeFileTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.small = mock.patch.object(render, "MAX_BYTES", 1000)  # the fixtures count as large here
        self.small.start()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, **{"pwr.kicad_pcb": PCB, "pwr.kicad_sch": SCH})
        self.pcb = self.f.repo.latest().files.get(path="pwr.kicad_pcb").blob
        self.sch = self.f.repo.latest().files.get(path="pwr.kicad_sch").blob

    def tearDown(self):
        self.small.stop()
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_says_it_is_drawn_in_the_background(self):
        result = render.render_blob(self.pcb, "pwr.kicad_pcb")
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending"])
        self.assertTrue(list(render.cache_dir().glob("want-*")))
        page = signed_in(self.f.eng).get(reverse("sources:kicad", args=["PWR", self.f.repo.slug, 1, "pwr.kicad_pcb"]))
        self.assertContains(page, "drawn in the background")
        self.assertNotContains(page, "can't be drawn")

    def test_scheduler_draws_it_in_a_separate_process(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        with mock.patch.object(render.subprocess, "run", wraps=subprocess.run) as ran:
            render.prerender()
        self.assertTrue(ran.called)
        self.assertIn("apps.sources.kicad.worker", ran.call_args[0][0])
        result = render.render_blob(self.pcb, "pwr.kicad_pcb")
        self.assertTrue(result["ok"], result)
        self.assertIn("<svg", result["svg"])
        self.assertEqual(result["info"]["vias"], 1)
        self.assertFalse(list(render.cache_dir().glob("want-*-pcb.json")))

    def test_schematics_too(self):
        render.prerender()  # new check-ins are queued, then drawn
        render.prerender()
        result = render.render_blob(self.sch, "pwr.kicad_sch", svg=False)
        self.assertTrue(result["ok"], result)
        self.assertIn("R7", [p["ref"] for p in result["parts"]])

    def test_one_large_file_per_run(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        render.render_blob(self.sch, "pwr.kicad_sch")
        with mock.patch.object(render, "draw_large", wraps=render.draw_large) as drew:
            render.prerender()
        self.assertEqual(drew.call_count, 1)

    def test_too_slow_is_reported_and_not_retried(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        with mock.patch.object(render.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            render.prerender()
        result = render.render_blob(self.pcb, "pwr.kicad_pcb")
        self.assertFalse(result["ok"])
        self.assertNotIn("pending", result)
        self.assertIn("longer than", result["error"])
        self.assertFalse(list(render.cache_dir().glob("want-*-pcb.json")))

    def test_out_of_memory_is_reported(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        with mock.patch.object(render.subprocess, "run", return_value=subprocess.CompletedProcess([], -9)):
            render.prerender()
        self.assertIn("too large or complex", render.render_blob(self.pcb, "pwr.kicad_pcb")["error"])

    def test_over_the_background_limit_is_refused_at_once(self):
        with mock.patch.object(render, "LARGE_MAX_BYTES", 1500):
            result = render.render_blob(self.pcb, "pwr.kicad_pcb")
        self.assertFalse(result["ok"])
        self.assertNotIn("pending", result)
        self.assertFalse(list(render.cache_dir().glob("want-*-pcb.json")))

    def test_compare_and_copies_fall_back_to_the_plain_drawing(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        render.prerender()
        lit = render.highlighted(self.pcb, self.pcb, "pwr.kicad_pcb", older=True)
        self.assertTrue(lit["ok"])
        self.assertNotIn("hl-", lit["svg"])
        copy = render.render_blob(self.pcb, "pwr.kicad_pcb", instance="/a/b")
        self.assertTrue(copy["ok"])

    @override_settings(TESTING=False)
    def test_scheduler_is_not_held_up(self):
        render.render_blob(self.pcb, "pwr.kicad_pcb")
        started = __import__("threading").Event()
        release = __import__("threading").Event()

        def slow(blob, path):
            started.set()
            release.wait(10)
        with mock.patch.object(render, "draw_large", side_effect=slow):
            render.prerender()  # returns while the drawing runs
            self.assertTrue(started.wait(5))
            self.assertEqual(render._draw_wanted(), 0)  # only one at a time
            release.set()
            for _ in range(50):
                if render._drawing.acquire(blocking=False):
                    render._drawing.release()
                    break
                __import__("time").sleep(0.1)

    def test_cleanup_removes_folders_left_by_a_restart(self):
        import os
        import time
        old = render.cache_dir() / "draw-abc"
        old.mkdir()
        (old / "input").write_text("x")
        os.utime(old, (time.time() - 86400, time.time() - 86400))
        fresh = render.cache_dir() / "draw-new"
        fresh.mkdir()
        render.prune()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
