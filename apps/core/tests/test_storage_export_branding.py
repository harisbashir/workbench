import io
import zipfile
from unittest import mock

import boto3
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from moto import mock_aws

from apps.accounts.models import User
from apps.core import export, system
from apps.core.branding import clean_svg
from apps.core.models import ExportJob, SiteSettings
from apps.core.models import StorageMove, StorageSettings
from apps.core.storage import seal, storage_info, stored_files, unseal
from apps.core.testing import make_user, signed_in
from apps.files.models import Document
from apps.firmware.models import Firmware, FirmwareArtifact, FirmwareRelease
from apps.projects.models import Project, Revision

S3_CFG = {"kind": "s3", "provider": "aws", "bucket": "wb-test", "region": "us-east-1", "endpoint": "",
          "prefix": "workbench", "access_key": "test", "secret_key": "test"}


def use_storage(cfg):
    row = StorageSettings.load()
    row.active = seal(cfg)
    row.save()


def s3_client():
    return boto3.client("s3", region_name="us-east-1")


@mock_aws
class S3StorageTests(TransactionTestCase):
    def setUp(self):
        s3_client().create_bucket(Bucket="wb-test")
        self.admin = make_user("boss", role=User.Role.ADMIN)
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.admin)

    def upload(self, name, data, client=None):
        (client or signed_in(self.admin)).post(reverse("files:upload_root", args=["PWR"]), {"files": [SimpleUploadedFile(name, data)]})
        return Document.objects.get(name=name)

    def test_upload_download_and_purge_in_s3(self):
        use_storage(S3_CFG)
        c = signed_in(self.admin)
        doc = self.upload("scope.png", b"PNGDATA", c)
        obj = s3_client().get_object(Bucket="wb-test", Key=f"workbench/{doc.latest.file.name}")
        self.assertEqual(obj["Body"].read(), b"PNGDATA")
        self.assertEqual(obj.get("ServerSideEncryption"), "AES256")
        r = c.get(reverse("files:download", args=[doc.latest.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"PNGDATA")
        doc.trash(self.admin)
        doc.purge()
        self.assertEqual(s3_client().list_objects_v2(Bucket="wb-test").get("KeyCount", 0), 0)

    def test_backup_includes_s3_files_and_copies_offsite(self):
        use_storage(S3_CFG)
        self.upload("a.txt", b"hello s3")
        path = system.create_backup("test")
        with zipfile.ZipFile(path) as z:
            self.assertTrue(any(n.endswith("a.txt") and z.read(n) == b"hello s3" for n in z.namelist()))
        keys = [o["Key"] for o in s3_client().list_objects_v2(Bucket="wb-test")["Contents"]]
        self.assertIn(f"workbench/backups/{path.name}", keys)

    def page(self, c, **data):
        return c.post(reverse("core:storage"), data, follow=True)

    def s3_form(self, **extra):
        return {"kind": "s3", "provider": "aws", "bucket": "wb-test", "region": "us-east-1", "prefix": "workbench",
                "access_key": "test", "secret_key": "test", **extra}

    def test_move_local_to_s3_from_the_app(self):
        from apps.core import storage_move
        c = signed_in(self.admin)
        doc = self.upload("local.txt", b"move me", c)
        # A wrong bucket fails the test and can't be moved to.
        r = self.page(c, action="test", **self.s3_form(bucket="no-such-bucket"))
        self.assertContains(r, "bucket doesn")
        self.page(c, action="move", lock="1")
        self.assertFalse(StorageMove.objects.exists())
        # The right one works.
        r = self.page(c, action="test", **self.s3_form())
        self.assertContains(r, "Connected")
        with mock.patch("apps.core.storage_views._worker_alive", return_value=True):
            self.page(c, action="move", lock="1")
        move = StorageMove.objects.get()
        storage_move.run_move(move, settle_seconds=0)
        move.refresh_from_db()
        self.assertEqual((move.status, move.copied, move.total), ("done", 1, 1), move.failures)
        row = StorageSettings.objects.get()
        self.assertTrue(row.locked)
        self.assertEqual(row.active["bucket"], "wb-test")
        self.assertTrue(row.active["secret_key"].startswith("enc:"), "keys are stored encrypted")
        self.assertEqual(row.previous.get("kind"), "local")
        self.assertTrue(storage_info()["kind"] == "s3")
        key = f"workbench/{doc.latest.file.name}"
        self.assertEqual(s3_client().get_object(Bucket="wb-test", Key=key)["Body"].read(), b"move me")
        # New uploads go to S3.
        new = self.upload("new.txt", b"fresh", c)
        s3_client().head_object(Bucket="wb-test", Key=f"workbench/{new.latest.file.name}")
        # If an object goes missing, the old storage is used as a fallback…
        s3_client().delete_object(Bucket="wb-test", Key=key)
        r = c.get(reverse("files:download", args=[doc.latest.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"move me")
        # …until the admin stops using it.
        self.page(c, action="forget_previous")
        self.assertEqual(c.get(reverse("files:download", args=[doc.latest.pk])).status_code, 404)
        # Locked: moving elsewhere is refused, but keys for the same bucket can be updated.
        r = self.page(c, action="test", kind="local", path=str(settings.FILES_DIR))
        self.assertContains(r, "locked")
        r = self.page(c, action="test", **self.s3_form(access_key="test", secret_key="test2"))
        self.page(c, action="apply")
        self.assertEqual(unseal(StorageSettings.objects.get().active)["secret_key"], "test2")
        out = io.StringIO()
        call_command("storage", "--unlock", stdout=out)
        self.assertFalse(StorageSettings.objects.get().locked)

    def test_failed_move_keeps_old_storage(self):
        from apps.core import storage_move
        self.upload("a.txt", b"x")
        bad = seal(dict(S3_CFG, bucket="missing-bucket"))
        move = StorageMove.objects.create(source={}, target=bad)
        storage_move.run_move(move, settle_seconds=0)
        move.refresh_from_db()
        self.assertEqual(move.status, "failed")
        self.assertEqual(move.failed, 1)
        self.assertEqual(storage_info()["kind"], "local")

    def test_resumed_move_skips_copied_files(self):
        from apps.core import storage_move
        self.upload("a.txt", b"x")
        self.upload("b.txt", b"yy")
        move = StorageMove.objects.create(source={}, target=seal(S3_CFG))
        storage_move.run_move(move, settle_seconds=0)
        use_storage({"kind": "local", "path": str(settings.FILES_DIR)})  # pretend we came back
        again = StorageMove.objects.create(source={}, target=seal(S3_CFG))
        storage_move.run_move(again, settle_seconds=0)
        again.refresh_from_db()
        self.assertEqual((again.skipped, again.copied), (2, 0))

    def test_move_to_another_folder(self):
        import tempfile
        from pathlib import Path

        from apps.core import storage_move
        doc = self.upload("a.txt", b"disk")
        other = tempfile.mkdtemp()
        c = signed_in(self.admin)
        self.page(c, action="test", kind="local", path=other)
        with mock.patch("apps.core.storage_views._worker_alive", return_value=True):
            self.page(c, action="move")
        storage_move.run_move(StorageMove.objects.get(), settle_seconds=0)
        self.assertTrue((Path(other) / doc.latest.file.name).exists())
        self.assertEqual(storage_info()["path"], other)
        self.assertFalse(StorageSettings.objects.get().locked)


class StorageFormTests(TestCase):
    def setUp(self):
        self.c = signed_in(make_user("boss", role=User.Role.ADMIN))

    def errors(self, **data):
        from apps.core.forms import StorageLocationForm
        f = StorageLocationForm({"kind": "s3", **data})
        f.is_valid()
        return f.errors

    def test_validation(self):
        self.assertIn("bucket", self.errors(provider="aws", bucket="Bad_Name", region="x"))
        self.assertIn("region", self.errors(provider="aws", bucket="ok-bucket"))
        self.assertIn("endpoint", self.errors(provider="r2", bucket="ok-bucket", endpoint="https://ACCOUNT_ID.r2.cloudflarestorage.com",
                                              access_key="a", secret_key="b"))
        self.assertIn("access_key", self.errors(provider="wasabi", bucket="ok-bucket", region="ca-central-1"))
        self.assertIn("endpoint", self.errors(provider="wasabi", bucket="ok-bucket", region="x", endpoint="http://insecure.example",
                                              access_key="a", secret_key="b"))
        self.assertEqual(self.errors(provider="wasabi", bucket="ok-bucket", region="ca-central-1", access_key="a", secret_key="b"), {})
        from apps.core.forms import StorageLocationForm
        f = StorageLocationForm({"kind": "local", "path": "relative/path"})
        self.assertFalse(f.is_valid())
        f = StorageLocationForm({"kind": "local", "path": str(settings.BACKUP_DIR)})
        self.assertFalse(f.is_valid())

    def test_endpoints(self):
        from apps.core.storage import endpoint_for
        self.assertEqual(endpoint_for({"provider": "wasabi", "region": "ca-central-1"}), "https://s3.ca-central-1.wasabisys.com")
        self.assertEqual(endpoint_for({"provider": "spaces", "region": "tor1"}), "https://tor1.digitaloceanspaces.com")
        self.assertEqual(endpoint_for({"provider": "aws", "region": "us-east-1"}), "")
        self.assertEqual(endpoint_for({"provider": "r2", "endpoint": "https://abc.r2.cloudflarestorage.com/"}), "https://abc.r2.cloudflarestorage.com")

    def test_pinned_in_env(self):
        from apps.core.storage import reset_cache
        with self.settings(STORAGE_ENV={"kind": "local", "path": str(settings.FILES_DIR)}):
            reset_cache()
            r = self.c.get(reverse("core:storage"))
            self.assertContains(r, "set in the server")
            self.c.post(reverse("core:storage"), {"action": "test", "kind": "local", "path": "/tmp"})
            self.assertFalse(StorageSettings.objects.get().draft)
        reset_cache()

    def test_only_admins(self):
        self.assertEqual(signed_in(make_user("eng")).get(reverse("core:storage")).status_code, 403)
        self.assertEqual(self.c.get(reverse("core:storage")).status_code, 200)


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
