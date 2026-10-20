import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.sources.kicad import render

from .helpers import commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "kicad"


def fixture(name):
    return (FIX / name).read_bytes()


class KicadViewTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.f = make_repo(kind="kicad")
        commit(self.f.repo, self.f.eng, message="Rev B", **{
            "pwr.kicad_sch": fixture("v8_hier.kicad_sch"), "buck.kicad_sch": fixture("v8_buck.kicad_sch"),
            "pwr.kicad_pcb": fixture("v8_board.kicad_pcb"), "broken.kicad_sch": b"(kicad_sch (version 20231120) (((",
            "sub/evil.kicad_sch": fixture("v8_hier.kicad_sch").replace(b'"buck.kicad_sch"', b'"../../evil.kicad_sch"'),
            "self.kicad_sch": fixture("v8_hier.kicad_sch").replace(b'"buck.kicad_sch"', b'"self.kicad_sch"')})
        self.c = signed_in(self.f.eng)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def url(self, name, path, v=1):
        return reverse(f"sources:{name}", args=["PWR", self.f.repo.slug, v, path])

    def test_file_view_redirects_to_preview(self):
        r = self.c.get(self.url("file", "pwr.kicad_sch"))
        self.assertRedirects(r, self.url("kicad", "pwr.kicad_sch"), fetch_redirect_response=False)
        r = self.c.get(self.url("file", "pwr.kicad_sch") + "?text=1")
        self.assertContains(r, 'id="L1"')

    def test_schematic_page(self):
        r = self.c.get(self.url("kicad", "pwr.kicad_sch"))
        self.assertContains(r, "R1")
        self.assertContains(r, "10k")
        self.assertContains(r, self.url("kicad", "buck.kicad_sch") + "?trail=pwr.kicad_sch")
        self.assertContains(r, self.url("kicad_svg", "pwr.kicad_sch"))
        child = self.c.get(self.url("kicad", "buck.kicad_sch") + "?trail=pwr.kicad_sch")
        self.assertContains(child, "U3")
        self.assertContains(child, self.url("kicad", "pwr.kicad_sch"))   # breadcrumb back to the root

    def test_svg_view_sandboxed(self):
        r = self.c.get(self.url("kicad_svg", "pwr.kicad_sch"))
        self.assertEqual(r["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertIn(b'data-ref="R1"', r.getvalue())
        r = self.c.get(self.url("kicad_svg", "pwr.kicad_pcb"))
        self.assertIn(b'data-view="top"', r.getvalue())

    def test_render_is_cached(self):
        self.c.get(self.url("kicad_svg", "pwr.kicad_sch"))
        self.assertTrue(any(Path(self.tmp, "cache", "kicad").iterdir()))

    def test_sheet_links_safe(self):
        for path in ("sub/evil.kicad_sch", "self.kicad_sch"):
            r = self.c.get(self.url("kicad", path))
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "not in this version")
            self.assertNotContains(r, "evil.kicad_sch?trail")

    def test_broken_file_message(self):
        r = self.c.get(self.url("kicad", "broken.kicad_sch"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "can't be read")
        self.assertEqual(self.c.get(self.url("kicad_svg", "broken.kicad_sch")).status_code, 404)

    def test_pcb_page(self):
        r = self.c.get(self.url("kicad", "pwr.kicad_pcb"))
        self.assertContains(r, "data-pcb-canvas")
        self.assertContains(r, "data-layer-toggle")
        self.assertContains(r, "R9")
        self.assertContains(r, "40.00 × 30.00 mm")

    def test_permissions(self):
        out = signed_in(self.f.outsider)
        for name in ("kicad", "kicad_svg"):
            self.assertIn(out.get(self.url(name, "pwr.kicad_sch")).status_code, (403, 404))
        self.assertEqual(signed_in(self.f.viewer).get(self.url("kicad", "pwr.kicad_sch")).status_code, 200)
        self.assertEqual(self.c.get(self.url("kicad", "missing.kicad_sch")).status_code, 404)

    def test_render_function_handles_limits(self):
        from unittest import mock

        from apps.sources.kicad import sexpr
        vf = self.f.repo.latest().files.get(path="pwr.kicad_sch")
        with mock.patch.object(sexpr, "MAX_NODES", 10):
            r = render.render_blob(vf.blob, "pwr.kicad_sch", use_cache=False)
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])
