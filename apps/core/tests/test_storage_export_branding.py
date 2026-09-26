import io
import zipfile

import boto3
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from moto import mock_aws

from apps.accounts.models import User
from apps.core import export, system
from apps.core.branding import clean_svg
from apps.core.models import ExportJob, SiteSettings
from apps.core.storage import backend, stored_files
from apps.core.testing import make_user, signed_in
from apps.files.models import Document
from apps.firmware.models import Firmware, FirmwareArtifact, FirmwareRelease
from apps.projects.models import Project, Revision

def _app_s3_config():
    """The exact S3 configuration the app builds from WORKBENCH_S3_* settings."""
    import os
    from unittest import mock

    import config.settings as cs
    env = {"WORKBENCH_S3_ACCESS_KEY_ID": "test", "WORKBENCH_S3_SECRET_ACCESS_KEY": "test"}
    with mock.patch.dict(cs.S3, {"bucket": "wb-test", "region": "us-east-1", "prefix": "workbench", "endpoint": ""}), \
            mock.patch.dict(os.environ, env):
        return cs.storage_backend("s3")


S3_CONFIG = _app_s3_config()


def s3_settings(**extra):
    from django.conf import settings
    configs = dict(settings.STORAGE_CONFIGS, s3=S3_CONFIG)
    storages = dict(settings.STORAGES, default=S3_CONFIG)
    return override_settings(STORAGE_KIND="s3", STORAGES=storages, STORAGE_CONFIGS=configs,
                             S3={"bucket": "wb-test", "region": "us-east-1", "endpoint": "", "prefix": "workbench"}, **extra)


@mock_aws
class S3StorageTests(TransactionTestCase):
    def setUp(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="wb-test")
        self.admin = make_user("boss", role=User.Role.ADMIN)
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.admin)

    def test_upload_download_and_purge_in_s3(self):
        with s3_settings():
            c = signed_in(self.admin)
            c.post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile("scope.png", b"PNGDATA")]})
            doc = Document.objects.get()
            key = f"workbench/{doc.latest.file.name}"
            obj = boto3.client("s3", region_name="us-east-1").get_object(Bucket="wb-test", Key=key)
            self.assertEqual(obj["Body"].read(), b"PNGDATA")
            self.assertEqual(obj.get("ServerSideEncryption"), "AES256")
            r = c.get(reverse("files:download", args=[doc.latest.pk]))
            self.assertEqual(b"".join(r.streaming_content), b"PNGDATA")
            doc.trash(self.admin)
            doc.purge()
            listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="wb-test").get("KeyCount", 0)
            self.assertEqual(listed, 0)

    def test_backup_includes_s3_files_and_copies_offsite(self):
        with s3_settings():
            signed_in(self.admin).post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile("a.txt", b"hello s3")]})
            path = system.create_backup("test")
            with zipfile.ZipFile(path) as z:
                self.assertTrue(any(n.endswith("a.txt") and z.read(n) == b"hello s3" for n in z.namelist()))
            keys = [o["Key"] for o in boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="wb-test")["Contents"]]
            self.assertIn(f"workbench/backups/{path.name}", keys)

    def test_migrate_storage_local_to_s3(self):
        # Upload while on local storage…
        signed_in(self.admin).post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile("local.txt", b"move me")]})
        doc = Document.objects.get()
        with s3_settings():
            out = io.StringIO()
            call_command("migrate_storage", "--to", "s3", "--from", "local", stdout=out)
            self.assertIn("Copied 1 files", out.getvalue())
            self.assertTrue(backend("s3").exists(doc.latest.file.name))
            out = io.StringIO()
            call_command("migrate_storage", "--to", "s3", "--from", "local", stdout=out)
            self.assertIn("1 already there", out.getvalue())
            # …and it's served from S3 now.
            r = signed_in(self.admin).get(reverse("files:download", args=[doc.latest.pk]))
            self.assertEqual(b"".join(r.streaming_content), b"move me")


class ExportTests(TransactionTestCase):
    def setUp(self):
        from pathlib import Path

        from django.conf import settings
        (Path(settings.DATA_DIR) / "scheduler.heartbeat").unlink(missing_ok=True)  # no worker: export runs inline

    def test_organised_export(self):
        admin = make_user("boss", role=User.Role.ADMIN, first_name="Haris")
        p = Project.objects.create(key="PWR", name="Power board")
        p.members.add(admin)
        rev = Revision.objects.create(project=p, name="Rev B")
        c = signed_in(admin)
        c.post(reverse("files:folder_create_root", args=["PWR"]), {"name": "Test reports"})
        from apps.files.models import Folder
        folder = Folder.objects.get()
        for content in (b"v1", b"v2"):
            c.post(reverse("files:upload", args=["PWR", folder.pk]), {"files": [SimpleUploadedFile("thermal.csv", content)]})
        c.post(reverse("files:upload_root", args=["shared"]), {"files": [SimpleUploadedFile("policy.md", b"# policy")]})
        fw = Firmware.objects.create(project=p, name="Main app")
        rel = FirmwareRelease.objects.create(firmware=fw, version="1.2.0", status="released", notes="Notes here")
        rel.revisions.add(rev)
        FirmwareArtifact.store(rel, SimpleUploadedFile("app.bin", b"\x01\x02"), admin)

        c.post(reverse("core:system_action"), {"action": "request_export", "include_versions": "1"})
        job = ExportJob.objects.get()
        self.assertEqual(job.status, "ready", job.message)
        with zipfile.ZipFile(export.export_dir() / job.file_name) as z:
            names = z.namelist()
            root = names[0].split("/")[0]
            def has(suffix):
                return any(n == f"{root}/{suffix}" for n in names)
            self.assertTrue(has("Projects/PWR - Power board/Files/Test reports/thermal.csv"), names)
            self.assertTrue(has("Projects/PWR - Power board/Files/Test reports/_older versions/thermal.csv/v1 - thermal.csv"))
            self.assertTrue(has("Shared files/policy.md"))
            self.assertTrue(has("Projects/PWR - Power board/Firmware/Main app/1.2.0 (Released)/app.bin"))
            notes = z.read(f"{root}/Projects/PWR - Power board/Firmware/Main app/1.2.0 (Released)/RELEASE NOTES.md").decode()
            self.assertIn("Rev B", notes)
            self.assertIn("Notes here", notes)
            self.assertTrue(has("Projects/PWR - Power board/Revisions/Rev B/BOM.csv"))
            self.assertTrue(has("Parts/Parts.csv") and has("People.csv") and has("README.txt"))
            self.assertEqual(z.read(f"{root}/Projects/PWR - Power board/Files/Test reports/thermal.csv"), b"v2")
        r = c.post(reverse("core:system_action"), {"action": "download_export", "name": job.file_name})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post(reverse("core:system_action"), {"action": "download_export", "name": "../secrets.json"}).status_code, 404)
        self.assertEqual(signed_in(make_user("eng")).post(reverse("core:system_action"), {"action": "request_export"}).status_code, 403)

    def test_names_are_made_safe(self):
        self.assertEqual(export.clean('a/b:c*?"<>|'), "a-b-c------")
        self.assertEqual(export.clean("  ..  "), "untitled")


class LogoTests(TestCase):
    def setUp(self):
        self.admin = signed_in(make_user("boss", role=User.Role.ADMIN))

    def upload(self, name, data, field="logo"):
        return self.admin.post(reverse("core:system_action"), {"action": "upload_logo", field: SimpleUploadedFile(name, data)})

    def test_svg_is_cleaned(self):
        evil = (b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40" onload="alert(1)">'
                b'<script>alert(1)</script><rect width="10" height="10" fill="red" onclick="x()"/>'
                b'<a href="javascript:alert(1)"><text>hi</text></a><image href="https://evil.example/x.png"/>'
                b'<foreignObject><div>html</div></foreignObject><use href="#r"/></svg>')
        out = clean_svg(evil).decode()
        for bad in ("script", "onload", "onclick", "javascript", "foreignObject", "evil.example"):
            self.assertNotIn(bad, out)
        self.assertIn("<rect", out)
        self.assertIn('viewBox="0 0 100 40"', out)
        self.assertIn('href="#r"', out)

    def test_rejects_entities_and_non_images(self):
        from django import forms
        with self.assertRaises(forms.ValidationError):
            clean_svg(b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>')
        self.upload("logo.png", b"not really a png")
        self.assertFalse(SiteSettings.load().logo)

    def test_upload_png_and_svg_served_safely(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (200, 50), (0, 0, 0, 0)).save(buf, "PNG")
        self.upload("logo.png", buf.getvalue())
        site = SiteSettings.load()
        self.assertTrue(site.logo.name.endswith(".png"))
        self.upload("white.svg", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4" cx="5" cy="5" fill="#fff"/></svg>', "logo_dark")
        self.client.logout()
        r = self.client.get(reverse("core:logo", args=["dark"]))
        self.assertEqual(r["Content-Type"], "image/svg+xml")
        self.assertIn("sandbox", r["Content-Security-Policy"])
        page = self.client.get(reverse("accounts:login"))
        self.assertContains(page, "/branding/logo-light")
        self.admin.post(reverse("core:system_action"), {"action": "remove_logo", "which": "logo_dark"})
        self.assertFalse(SiteSettings.load().logo_dark)

    def test_only_admins(self):
        eng = signed_in(make_user("eng"))
        self.assertEqual(eng.post(reverse("core:system_action"), {"action": "upload_logo"}).status_code, 403)


class StoredFilesTests(TestCase):
    def test_lists_documents_and_firmware(self):
        admin = make_user("boss", role=User.Role.ADMIN)
        p = Project.objects.create(key="PWR", name="Power")
        p.members.add(admin)
        signed_in(admin).post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile("a.txt", b"x")]})
        fw = Firmware.objects.create(project=p, name="App")
        rel = FirmwareRelease.objects.create(firmware=fw, version="1.0.0")
        FirmwareArtifact.store(rel, SimpleUploadedFile("app.bin", b"y"), admin)
        names = [n for n, _ in stored_files()]
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.startswith("firmware/PWR/app/1.0.0/") for n in names))
