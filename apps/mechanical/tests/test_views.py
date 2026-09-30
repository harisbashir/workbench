import io
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.cad import jobs
from apps.core.testing import make_user, signed_in
from apps.mechanical.models import MechanicalFile, MechanicalPart
from apps.projects.models import Project


class MechanicalTests(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.project = Project.objects.create(key="ME", name="Mech", lead=self.lead)
        self.project.members.add(self.eng)
        self.part = MechanicalPart.objects.create(project=self.project, name="Case", revision="A",
                                                  status=MechanicalPart.Status.RELEASED)
        self.edit_url = reverse("mechanical:edit", args=[self.project.key, self.part.pk])

    def form(self, **kw):
        data = {"name": "Case", "kind": "enclosure", "revision": "A", "status": "released", "quantity": 1}
        data.update(kw)
        return data

    def test_released_part_details_are_locked_for_engineers(self):
        c = signed_in(self.eng)
        self.assertEqual(c.get(self.edit_url).status_code, 403)
        self.assertEqual(c.post(self.edit_url, self.form(revision="B", material="ABS")).status_code, 403)
        self.part.refresh_from_db()
        self.assertEqual((self.part.revision, self.part.material), ("A", ""))

    def test_lead_can_edit_released_part(self):
        r = signed_in(self.lead).post(self.edit_url, self.form(revision="B", status="prototype"))
        self.assertEqual(r.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual((self.part.revision, self.part.status), ("B", "prototype"))

    def test_engineer_can_edit_unreleased_part(self):
        MechanicalPart.objects.filter(pk=self.part.pk).update(status="prototype")
        r = signed_in(self.eng).post(self.edit_url, self.form(status="prototype", material="PETG"))
        self.assertEqual(r.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.material, "PETG")

    def test_download_all_streams_a_zip(self):
        with mock.patch.object(jobs, "_start_thread"):
            for name, data in (("case.step", b"ISO-10303-21;" + b"x" * 5000), ("notes.txt", b"hello")):
                MechanicalFile.store(self.part, SimpleUploadedFile(name, data), self.lead)
        r = signed_in(self.eng).get(reverse("mechanical:download_all", args=[self.project.key, self.part.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.streaming)
        self.assertIn("attachment", r["Content-Disposition"])
        z = zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content)))
        names = sorted(z.namelist())
        self.assertEqual(len(names), 2)
        self.assertEqual(z.read([n for n in names if n.endswith("notes.txt")][0]), b"hello")
