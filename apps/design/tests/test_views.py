import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.design import demo_board
from apps.design.models import DesignFile
from apps.projects.models import Project, Revision


def up(name, data):
    return SimpleUploadedFile(name, data)


class DesignFileTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.viewer = make_user("viewer", role=User.Role.VIEWER)
        self.outsider = make_user("out")
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.eng, self.viewer)
        self.rev = Revision.objects.create(project=self.p, name="Rev B")
        self.c = signed_in(self.eng)

    def url(self, name, *extra):
        return reverse(f"design:{name}", args=["PWR", self.rev.pk, *extra])

    def upload(self, client, *files, **extra):
        return client.post(self.url("upload"), {"files": list(files), **extra})

    def test_upload_detects_types_and_versions(self):
        self.upload(self.c, up("pwr-board-gerbers.zip", demo_board.gerber_zip()), up("pwr-board-pos.csv", demo_board.pos_csv()),
                    up("PWR_schematic.pdf", b"%PDF-1.4 x"), up("pwr-board.kicad_pcb", b"(kicad_pcb)"), up("x.html", b"<b>"))
        cats = dict(DesignFile.objects.values_list("name", "category"))
        self.assertEqual(cats, {"pwr-board-gerbers.zip": "gerber", "pwr-board-pos.csv": "pnp", "PWR_schematic.pdf": "schematic",
                                "pwr-board.kicad_pcb": "pcb"})
        # same content again: skipped; changed content: version 2, v1 kept
        self.upload(self.c, up("pwr-board.kicad_pcb", b"(kicad_pcb)"))
        self.assertEqual(DesignFile.objects.filter(name="pwr-board.kicad_pcb").count(), 1)
        self.upload(self.c, up("pwr-board.kicad_pcb", b"(kicad_pcb v2)"))
        versions = DesignFile.objects.filter(name="pwr-board.kicad_pcb").order_by("version")
        self.assertEqual([(v.version, v.is_current) for v in versions], [(1, False), (2, True)])
        r = self.c.get(self.url("files"))
        self.assertContains(r, "pwr-board-gerbers.zip")
        self.assertContains(r, "50.0 × 32.0 mm")

    def test_fetch_upload_reports_errors(self):
        r = self.c.post(self.url("upload"), {"files": [up("x.exe", b"MZ")]}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json()["errors"], ["x.exe: files of type .exe aren't allowed."])

    def test_viewer_and_svg(self):
        self.upload(self.c, up("g.zip", demo_board.gerber_zip()))
        r = self.c.get(self.url("pcb"))
        self.assertContains(r, "data-pcb-canvas")
        self.assertContains(r, "Top copper")
        svg = self.c.get(self.url("pcb_svg"))
        self.assertEqual(svg["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn("sandbox", svg["Content-Security-Policy"])
        self.assertTrue(svg.content.startswith(b"<svg"))
        thumb = self.c.get(self.url("pcb_svg") + "?view=thumb")
        self.assertNotIn(b'data-view="layers"', thumb.content)
        # the revision page shows the board
        self.assertContains(self.c.get(self.rev.get_absolute_url()), "Open board viewer")

    def test_broken_gerbers_show_a_message(self):
        self.upload(self.c, up("gerbers.zip", b"PK\x05\x06" + b"\0" * 18), category="gerber")
        r = self.c.get(self.url("pcb"))
        self.assertContains(r, "couldn")

    def test_file_previews_and_download_all(self):
        self.upload(self.c, up("pwr-board-pos.csv", demo_board.pos_csv()), up("notes.txt", b"hello"))
        pos = DesignFile.objects.get(name="pwr-board-pos.csv")
        r = self.c.get(pos.get_absolute_url())
        self.assertContains(r, "PosX")
        self.assertContains(r, "USB_C_Receptacle")
        z = zipfile.ZipFile(io.BytesIO(b"".join(self.c.get(self.url("download_all")).streaming_content)))
        self.assertIn("PWR-Rev-B/Pick and place (CPL)/pwr-board-pos.csv", z.namelist())

    def test_permissions_and_release_lock(self):
        self.upload(self.c, up("a.txt", b"a"))
        f = DesignFile.objects.get()
        self.assertEqual(signed_in(self.outsider).get(self.url("files")).status_code, 403)
        self.assertEqual(signed_in(self.outsider).get(self.url("download", f.pk)).status_code, 403)
        self.assertEqual(self.upload(signed_in(self.viewer), up("b.txt", b"b")).status_code, 403)
        self.assertEqual(signed_in(self.viewer).get(self.url("download", f.pk)).status_code, 200)
        self.rev.status = Revision.Status.RELEASED
        self.rev.save()
        self.assertEqual(self.upload(self.c, up("c.txt", b"c")).status_code, 403)
        self.assertEqual(self.c.post(self.url("delete", f.pk)).status_code, 403)
        self.assertContains(self.c.get(self.url("files")), "locked")

    def test_delete_removes_all_versions(self):
        self.upload(self.c, up("a.txt", b"1"))
        self.upload(self.c, up("a.txt", b"2"))
        f = DesignFile.objects.filter(is_current=True).get()
        self.c.post(self.url("delete", f.pk))
        self.assertFalse(DesignFile.objects.exists())

    def test_design_files_are_backed_up_and_exported(self):
        from apps.core.storage import stored_files
        self.upload(self.c, up("a.txt", b"1"))
        self.assertTrue(any(n.startswith("design/PWR/") for n, _ in stored_files()))

    def test_design_files_in_full_export(self):
        from pathlib import Path

        from django.conf import settings

        from apps.core import export
        (Path(settings.DATA_DIR) / "scheduler.heartbeat").unlink(missing_ok=True)
        self.upload(self.c, up("pwr-board-pos.csv", demo_board.pos_csv()))
        path, _counts = export.build_export(include_versions=True)
        with zipfile.ZipFile(path) as z:
            self.assertTrue(any(n.endswith("Revisions/Rev B/Design files/Pick and place (CPL)/pwr-board-pos.csv") for n in z.namelist()))
