"""Conversions run in a separate, limited process; stuck ones are retried or failed by the scheduler tick."""
import struct
import sys
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.cad import jobs, samples
from apps.cad.mesh import read_header
from apps.cad.models import MeshJob, MeshStatus
from apps.core.testing import make_user, signed_in
from apps.mechanical.models import MechanicalFile, MechanicalPart
from apps.projects.models import Project


class JobTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.project = Project.objects.create(key="JOB", name="Jobs", lead=self.eng)
        self.part = MechanicalPart.objects.create(project=self.project, name="Case")

    def store(self, name, data, background=False):
        f, _ = MechanicalFile.store(self.part, SimpleUploadedFile(name, data), self.eng, background=background)
        f.refresh_from_db()
        return f

    def test_conversion_in_a_subprocess(self):
        f = self.store("cube.stl", samples.stl_binary())
        self.assertEqual(f.mesh_status, MeshStatus.READY, f.mesh_message)
        self.assertEqual(f.mesh_info["triangles"], 12)
        self.assertEqual(read_header(f.mesh.read())["triangles"], 12)
        self.assertTrue(f.thumb)
        self.assertFalse(MeshJob.objects.exists(), "the job row is removed when the conversion ends")

    def test_hostile_file_fails_cleanly(self):
        data = samples.stl_binary()
        f = self.store("bad.3mf", b"PK\x03\x04 not really a zip" + data)
        self.assertEqual(f.mesh_status, MeshStatus.FAILED)
        self.assertIn("zip", f.mesh_message)

    def test_upload_request_doesnt_convert(self):
        c = signed_in(self.eng)
        with mock.patch.object(jobs, "_start_thread") as start, mock.patch.object(jobs, "run_worker") as work:
            r = c.post(reverse("mechanical:upload", args=[self.project.key, self.part.pk]),
                       {"files": [SimpleUploadedFile("cube.stl", samples.stl_binary())]})
        self.assertEqual(r.status_code, 302)
        f = MechanicalFile.objects.get()
        self.assertEqual(f.mesh_status, MeshStatus.PENDING)
        start.assert_called_once()
        work.assert_not_called()

    @override_settings(CAD_CONVERT_TIMEOUT=1)
    def test_wall_clock_timeout(self):
        with mock.patch.object(jobs, "_worker_command", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            f = self.store("cube.stl", samples.stl_binary())
        self.assertEqual(f.mesh_status, MeshStatus.FAILED)
        self.assertIn("longer than 1 seconds", f.mesh_message)

    def test_crash_of_the_converter(self):
        with mock.patch.object(jobs, "_worker_command", return_value=[sys.executable, "-c", "import os; os.abort()"]):
            f = self.store("cube.step", b"ISO-10303-21;")
        self.assertEqual(f.mesh_status, MeshStatus.FAILED)
        self.assertIn("crashed", f.mesh_message)

    @override_settings(CAD_CONVERT_MEMORY_MB=300)
    def test_memory_limit(self):
        # 2.5 million triangles: welding needs far more than 300 MB
        n = 2_500_000
        tris = struct.pack("<12f", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0) + b"\0\0"
        data = b"big".ljust(80, b" ") + struct.pack("<I", n) + tris * n
        f = self.store("big.stl", data)
        self.assertEqual(f.mesh_status, MeshStatus.FAILED)
        self.assertRegex(f.mesh_message, "too large|memory")

    def test_stuck_conversion_is_retried_then_failed(self):
        with mock.patch.object(jobs, "_start_thread"):
            f = self.store("cube.stl", samples.stl_binary(), background=True)
        old = timezone.now() - jobs.stale_after() - timedelta(minutes=1)
        MechanicalFile.objects.filter(pk=f.pk).update(mesh_status=MeshStatus.CONVERTING)
        MeshJob.objects.create(model="mechanical.MechanicalFile", object_id=f.pk, attempts=1, started_at=old)
        self.assertEqual(jobs.recover_stuck(), 1)
        f.refresh_from_db()
        self.assertEqual(f.mesh_status, MeshStatus.PENDING)
        self.assertEqual(MeshJob.objects.get().attempts, 1, "attempts are kept for the next try")
        # third attempt also dies → failed with a clear message
        MechanicalFile.objects.filter(pk=f.pk).update(mesh_status=MeshStatus.CONVERTING)
        MeshJob.objects.filter(object_id=f.pk).update(attempts=3, started_at=old)
        jobs.recover_stuck()
        f.refresh_from_db()
        self.assertEqual(f.mesh_status, MeshStatus.FAILED)
        self.assertIn("failed 3 times", f.mesh_message)
        self.assertFalse(MeshJob.objects.exists())

    def test_running_conversion_is_left_alone(self):
        with mock.patch.object(jobs, "_start_thread"):
            f = self.store("cube.stl", samples.stl_binary(), background=True)
        MechanicalFile.objects.filter(pk=f.pk).update(mesh_status=MeshStatus.CONVERTING)
        MeshJob.objects.create(model="mechanical.MechanicalFile", object_id=f.pk, attempts=1, started_at=timezone.now())
        self.assertEqual(jobs.recover_interrupted(), 0)
        f.refresh_from_db()
        self.assertEqual(f.mesh_status, MeshStatus.CONVERTING)

    def test_converting_without_a_job_row_is_requeued(self):
        with mock.patch.object(jobs, "_start_thread"):
            f = self.store("cube.stl", samples.stl_binary(), background=True)
        MechanicalFile.objects.filter(pk=f.pk).update(mesh_status=MeshStatus.CONVERTING)
        jobs.recover_stuck()
        f.refresh_from_db()
        self.assertEqual(f.mesh_status, MeshStatus.PENDING)

    def test_tick_converts_pending_files_within_the_parallel_limit(self):
        with mock.patch.object(jobs, "_start_thread"):
            a = self.store("a.stl", samples.stl_binary(), background=True)
            b = self.store("b.stl", samples.stl_ascii(), background=True)
        with override_settings(CAD_MAX_PARALLEL=1):
            with mock.patch.object(jobs, "_start_thread") as start:
                self.assertEqual(jobs.tick(), 1)
                start.assert_called_once_with(MechanicalFile, a.pk)
            self.assertEqual(jobs.tick(block=True), 1)
            self.assertEqual(jobs.tick(block=True), 1)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.mesh_status, b.mesh_status), (MeshStatus.READY, MeshStatus.READY))

    def test_status_endpoint_reports_failure(self):
        f = self.store("bad.3mf", b"nope")
        r = signed_in(self.eng).get(f.mesh_status_url)
        self.assertEqual(r.json()["status"], "failed")
        self.assertTrue(r.json()["message"])
