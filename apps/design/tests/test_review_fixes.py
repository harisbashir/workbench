"""Resource limits of the Gerber renderer, render caching, and robustness of design views."""
import io
import tempfile
import time
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core.testing import make_user, signed_in
from apps.design import demo_board, render
from apps.design.board import render_board
from apps.design.gerber import Budget, GerberError, GerberLimitError, parse_gerber
from apps.design.models import DesignFile
from apps.projects.models import Project, Revision

HEAD = "%FSLAX46Y46*%%MOMM*%G04 test*\n"


class GerberLimitTests(SimpleTestCase):
    def test_huge_step_and_repeat_is_refused_quickly(self):
        text = HEAD + "%ADD10C,0.1*%%SRX3000Y3000I1J1*%D10*X0Y0D03*%SR*%M02*"
        t = time.monotonic()
        with self.assertRaises(GerberLimitError):
            render_board([("f.gtl", text.encode())])
        self.assertLess(time.monotonic() - t, 2)

    def test_small_step_and_repeat_still_works(self):
        text = HEAD + "%ADD10C,0.5*%%SRX3Y2I5J5*%D10*X0Y0D03*%SR*%M02*"
        L = parse_gerber(text, "panel.gtl")
        self.assertEqual(sum(len(els) for _d, els in L.segments), 6)

    def test_polygon_aperture_with_millions_of_vertices_is_not_built(self):
        L = parse_gerber(HEAD + "%ADD10P,1X30000000*%D10*X0Y0D03*M02*", "p.gtl")
        self.assertIn("Couldn't read aperture D10", L.warnings)
        self.assertLess(len("".join(L.defs)), 200)

    def test_polygon_macro_primitive_is_limited(self):
        L = parse_gerber(HEAD + "%AMPOLY*5,1,30000000,0,0,1,0*%%ADD10POLY*%D10*X0Y0D03*M02*", "p.gtl")
        self.assertIn("Couldn't read aperture D10", L.warnings)

    def test_moire_with_many_rings_or_zero_pitch_terminates(self):
        t = time.monotonic()
        L = parse_gerber(HEAD + "%AMM*6,0,0,1,0,0,300000000,0.1,1,0*%%ADD10M*%D10*X0Y0D03*M02*", "m.gtl")
        self.assertIn("Couldn't read aperture D10", L.warnings)
        L = parse_gerber(HEAD + "%AMM*6,0,0,1,0,0,50,0.1,1,0*%%ADD11M*%D11*X0Y0D03*M02*", "m.gtl")
        self.assertLess("".join(L.defs).count("<path"), 10)  # zero pitch: one ring, not 50 identical ones
        self.assertLess(time.monotonic() - t, 2)

    def test_element_budget(self):
        text = HEAD + "%ADD10C,0.1*%D10*" + "".join(f"X{i * 1000}Y0D03*" for i in range(500)) + "M02*"
        with self.assertRaises(GerberLimitError):
            parse_gerber(text, "f.gtl", budget=Budget(max_elements=100))

    def test_time_budget(self):
        text = HEAD + "%ADD10C,0.1*%D10*" + "".join(f"X{i * 1000}Y0D03*" for i in range(5000)) + "M02*"
        with self.assertRaises(GerberLimitError):
            render_board([("f.gtl", text.encode())], budget=Budget(seconds=-1))

    def test_out_of_range_coordinates(self):
        with self.assertRaises(GerberError):
            parse_gerber(HEAD + "%ADD10C,0.1*%D10*X" + "9" * 400 + ".0Y0D03*M02*", "f.gtl")

    def test_many_loose_outline_pieces_are_fast(self):
        body = HEAD + "%ADD10C,0.1*%D10*" + "".join(
            f"X{i * 300000}Y0D02*X{i * 300000 + 100000}Y0D01*" for i in range(8000)) + "M02*"
        t = time.monotonic()
        result = render_board([("b-Edge_Cuts.gbr", body.encode())])
        self.assertLess(time.monotonic() - t, 10)
        self.assertTrue(result["svg"].startswith("<svg"))

    def test_real_board_still_renders(self):
        result = render_board([("pwr.zip", demo_board.gerber_zip())])
        self.assertGreater(result["info"]["copper_layers"], 0)


class RenderCacheTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Project.objects.create(key="PWR", name="Power")
        self.rev = Revision.objects.create(project=p, name="Rev A")

    def test_failures_are_cached_by_checksum(self):
        bad = HEAD + "%ADD10C,0.1*%%SRX3000Y3000I1J1*%D10*X0Y0D03*%SR*%M02*"
        f, _ = DesignFile.store(self.rev, SimpleUploadedFile("bad.gtl", bad.encode()), None, background=False)
        with override_settings(DATA_DIR=self.tmp.name):
            with mock.patch.object(render, "render_board", wraps=render.render_board) as spy:
                first = render.render_files([f])
                second = render.render_files([f])
        self.assertFalse(first["ok"])
        self.assertIn("step & repeat", first["error"])
        self.assertEqual(second["error"], first["error"])
        self.assertEqual(spy.call_count, 1)

    def test_a_render_in_progress_is_not_started_again(self):
        f, _ = DesignFile.store(self.rev, SimpleUploadedFile("b.zip", demo_board.gerber_zip()), None,
                                category="gerber", background=False)
        inner = {}

        def slow(entries, *a, **k):
            inner.update(render.render_files([f]))  # a second request while the first is still drawing
            return {"svg": "<svg/>"}

        with override_settings(DATA_DIR=self.tmp.name):
            with mock.patch.object(render, "render_board", side_effect=slow):
                outer = render.render_files([f])
            self.assertEqual(list(render.cache_dir().glob("*.lock")), [])
        self.assertTrue(inner["pending"])
        self.assertFalse(inner["ok"])
        self.assertTrue(outer["ok"])


class DesignViewRobustnessTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.eng)
        self.rev = Revision.objects.create(project=self.p, name="Rev B")
        self.c = signed_in(self.eng)

    def test_non_numeric_file_id_is_404_not_500(self):
        r = self.c.get(reverse("design:pcb", args=["PWR", self.rev.pk]) + "?file=abc")
        self.assertEqual(r.status_code, 404)

    def test_zip_listing_reads_the_directory(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(400):
                z.writestr(f"f{i}.txt", "x")
        f, _ = DesignFile.store(self.rev, SimpleUploadedFile("misc.zip", buf.getvalue()), self.eng, background=False)
        r = self.c.get(reverse("design:file", args=["PWR", self.rev.pk, f.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["zip_list"]), 300)
