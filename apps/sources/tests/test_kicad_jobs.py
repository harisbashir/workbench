import os
import shutil
import tempfile
import time
from pathlib import Path

from django.test import TestCase, override_settings

from apps.sources.kicad import render

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"


class PrerenderTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prerender_fills_cache(self):
        f = make_repo(kind="kicad")
        commit(f.repo, f.eng, **{"a.kicad_sch": (FIX / "v8_hier.kicad_sch").read_text(),
                                 "b.kicad_pcb": (FIX / "v8_board.kicad_pcb").read_text(), "notes.txt": "x"})
        self.assertEqual(render.prerender(), 2)
        self.assertEqual(len(list(render.cache_dir().glob("*.json"))), 2)
        self.assertEqual(render.prerender(), 0)

    def test_cache_cleanup(self):
        old = render.cache_dir() / "old-sch-1.json"
        new = render.cache_dir() / "new-sch-1.json"
        old.write_text("{}")
        new.write_text("{}")
        past = time.time() - 40 * 86400
        os.utime(old, (past, past))
        self.assertEqual(render.prune(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_scheduler_runs_prerender(self):
        from apps.core.management.commands.run_scheduler import tick
        from apps.core.models import SiteSettings
        SiteSettings.objects.update_or_create(pk=SiteSettings.load().pk, defaults={"backup_enabled": False})
        f = make_repo(kind="kicad")
        commit(f.repo, f.eng, **{"a.kicad_sch": (FIX / "v8_hier.kicad_sch").read_text()})
        self.assertIn("kicad previews", tick())
