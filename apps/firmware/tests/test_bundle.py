import io
import json
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.testing import signed_in
from apps.firmware import sets
from apps.firmware.models import FirmwareArtifact
from apps.production.models import BuildOrder
from apps.projects.models import Revision

from .helpers import make_fixture, release


class BundleTests(TestCase):
    def setUp(self):
        self.f = make_fixture()
        for rel, name in ((self.f.boot100, "boot.bin"), (self.f.app100, "app.bin")):
            FirmwareArtifact.store(rel, SimpleUploadedFile(name, name.encode() * 10), self.f.eng)
            FirmwareArtifact.store(rel, SimpleUploadedFile(name.replace(".bin", ".elf"), b"symbols"), self.f.eng)
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: self.f.boot100.pk, self.f.app_slot.pk: self.f.app100.pk}, self.f.eng)
        self.url = reverse("firmware:revision_bundle", args=["PWR", self.f.rev.pk])

    def unzip(self, response):
        self.assertEqual(response.status_code, 200)
        return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    def test_manifest_and_files(self):
        z = self.unzip(signed_in(self.f.eng).get(self.url))
        names = sorted(n for n in z.namelist() if n != "manifest.json")
        self.assertEqual(names, ["01-U1-bootloader-1.0.0/boot.bin", "02-U1-main-application-1.0.0/app.bin"])
        manifest = json.loads(z.read("manifest.json"))
        items = {i["file"]: i for i in manifest["items"]}
        app = items["02-U1-main-application-1.0.0/app.bin"]
        self.assertEqual(app["sha256"], self.f.app100.artifacts.get(name="app.bin").sha256)
        self.assertEqual((app["controller"], app["designator"], app["firmware"], app["version"], app["flash_order"]),
                         ("U1 Main MCU", "U1", "Main application", "1.0.0", 2))

    def test_bundle_refuses_recalled(self):
        self.f.app100.status = "recalled"
        self.f.app100.save()
        r = signed_in(self.f.eng).get(self.url, follow=True)
        self.assertContains(r, "is recalled")

    @override_settings(MAX_UPLOAD_MB=0)
    def test_bundle_size_cap(self):
        r = signed_in(self.f.eng).get(self.url, follow=True)
        self.assertContains(r, "too large")

    def test_bundle_empty(self):
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: None, self.f.app_slot.pk: None}, self.f.eng)
        r = signed_in(self.f.eng).get(self.url, follow=True)
        self.assertContains(r, "There are no firmware files to download yet.")

    def test_bundle_permissions(self):
        self.assertIn(signed_in(self.f.outsider).get(self.url).status_code, (403, 404))
        other_rev = Revision.objects.create(project=self.f.other, name="Rev X")
        r = signed_in(self.f.eng).get(reverse("firmware:revision_bundle", args=["PWR", other_rev.pk]))
        self.assertEqual(r.status_code, 404)

    def test_build_bundle_uses_snapshot(self):
        b = BuildOrder.objects.create(revision=self.f.rev, quantity=2)
        b.copy_firmware_from_revision()
        newer = release(self.f.app, "1.0.1", revisions=[self.f.rev])
        FirmwareArtifact.store(newer, SimpleUploadedFile("app101.bin", b"new"), self.f.eng)
        sets.save_set(self.f.rev, {self.f.app_slot.pk: newer.pk}, self.f.eng)
        url = reverse("firmware:build_bundle", args=[b.pk])
        z = self.unzip(signed_in(self.f.eng).get(url))
        self.assertIn("02-U1-main-application-1.0.0/app.bin", z.namelist())
        self.assertIn(signed_in(self.f.outsider).get(url).status_code, (403, 404))


class GuessKindTests(TestCase):
    def test_bootloader_debug_symbols_are_debug(self):
        self.assertEqual(FirmwareArtifact.guess_kind("bootloader.elf"), "debug")
        self.assertEqual(FirmwareArtifact.guess_kind("bootloader.bin"), "bootloader")
