import hashlib
import hmac
import json
import os
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import Notification
from apps.core.testing import make_user, signed_in
from apps.firmware.models import Firmware, FirmwareRelease, parse_version, sort_key
from apps.production.models import BuildOrder
from apps.projects.models import Project, Revision


def binfile(name="app.bin", data=b"\x00\x01firmware"):
    return SimpleUploadedFile(name, data)


class VersionTests(TestCase):
    def test_parse_and_order(self):
        self.assertEqual(parse_version("v1.4.2"), (1, 4, 2, ""))
        self.assertIsNone(parse_version("1.4"))
        self.assertIsNone(parse_version("01.2.3"))
        order = sorted(["1.10.0", "1.2.0", "2.0.0-rc.1", "2.0.0", "1.2.0-beta"], key=sort_key)
        self.assertEqual(order, ["1.2.0-beta", "1.2.0", "1.10.0", "2.0.0-rc.1", "2.0.0"])


class FirmwareFlowTests(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.outsider = make_user("out")
        self.p = Project.objects.create(key="PWR", name="Power", lead=self.lead, github_repo="acme/power")
        self.p.members.add(self.eng)
        self.rev_a = Revision.objects.create(project=self.p, name="Rev A")
        self.rev_b = Revision.objects.create(project=self.p, name="Rev B")
        self.fw = Firmware.objects.create(project=self.p, name="Main application")

    def create(self, client, version, revs, files=(), testing=True):
        data = {"version": version, "git_ref": f"v{version}", "revisions": [r.pk for r in revs], "notes": "Fixes PWR-1"}
        if testing:
            data["to_testing"] = "1"
        data["artifacts"] = list(files)
        return client.post(reverse("firmware:release_create", args=["PWR", self.fw.slug]), data)

    def status(self, client, rel, action, note=""):
        return client.post(reverse("firmware:release_status", args=["PWR", self.fw.slug, rel.version]), {"action": action, "note": note})

    def test_create_release_with_artifact_and_checksum(self):
        c = signed_in(self.eng)
        self.create(c, "1.0.0", [self.rev_a], [binfile(data=b"hello")])
        rel = FirmwareRelease.objects.get()
        self.assertEqual(rel.status, "testing")
        art = rel.artifacts.get()
        self.assertEqual(art.sha256, hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(art.kind, "image")
        r = c.get(reverse("firmware:artifact", args=[art.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"hello")
        self.assertEqual(r["X-Checksum-SHA256"], art.sha256)
        self.assertEqual(signed_in(self.outsider).get(reverse("firmware:artifact", args=[art.pk])).status_code, 404)

    def test_version_rules(self):
        c = signed_in(self.eng)
        self.create(c, "1.0.0", [self.rev_a])
        r = self.create(c, "1.0.0", [self.rev_a])
        self.assertContains(r, "already exists")
        r = self.create(c, "1.0", [self.rev_a])
        self.assertContains(r, "semantic versioning")
        self.assertEqual(FirmwareRelease.objects.count(), 1)

    def test_only_lead_releases_and_requirements(self):
        c = signed_in(self.eng)
        self.create(c, "1.0.0", [self.rev_a])
        rel = FirmwareRelease.objects.get()
        self.assertEqual(self.status(c, rel, "released").status_code, 403)
        lead = signed_in(self.lead)
        self.status(lead, rel, "released")
        rel.refresh_from_db()
        self.assertEqual(rel.status, "testing", "needs a file before release")
        c.post(reverse("firmware:release_upload", args=["PWR", self.fw.slug, "1.0.0"]), {"artifacts": [binfile()]})
        self.status(lead, rel, "released")
        rel.refresh_from_db()
        self.assertEqual((rel.status, rel.released_by), ("released", self.lead))

    def test_released_is_locked(self):
        c = signed_in(self.eng)
        self.create(c, "1.0.0", [self.rev_a], [binfile()])
        rel = FirmwareRelease.objects.get()
        self.status(signed_in(self.lead), rel, "released")
        self.assertEqual(c.post(reverse("firmware:release_upload", args=["PWR", self.fw.slug, "1.0.0"]), {"artifacts": [binfile("x.bin")]}).status_code, 403)
        art = rel.artifacts.get()
        self.assertEqual(c.post(reverse("firmware:artifact_delete", args=["PWR", self.fw.slug, "1.0.0", art.pk])).status_code, 403)
        r = c.get(reverse("firmware:release_edit", args=["PWR", self.fw.slug, "1.0.0"]))
        self.assertEqual(r.status_code, 302)

    def test_new_release_supersedes_and_recommendation_per_revision(self):
        c, lead = signed_in(self.eng), signed_in(self.lead)
        self.create(c, "1.0.0", [self.rev_a], [binfile()])
        self.create(c, "1.1.0", [self.rev_a, self.rev_b], [binfile()])
        self.create(c, "2.0.0-rc.1", [self.rev_b], [binfile()])
        v1, v11, rc = (FirmwareRelease.objects.get(version=v) for v in ("1.0.0", "1.1.0", "2.0.0-rc.1"))
        self.status(lead, v1, "released")
        self.assertEqual(self.fw.recommended_for(self.rev_a), v1)
        self.assertIsNone(self.fw.recommended_for(self.rev_b))
        self.status(lead, v11, "released")
        v1.refresh_from_db()
        self.assertEqual(v1.status, "deprecated")
        self.assertEqual(self.fw.recommended_for(self.rev_b), v11)
        self.assertEqual(self.fw.recommended_for(self.rev_b), v11, "testing releases are never recommended")
        page = c.get(reverse("firmware:project", args=["PWR"]))
        self.assertContains(page, "1.1.0")
        # compare changelog
        r = c.get(v11.get_absolute_url() + "?since=1.0.0")
        self.assertContains(r, "Fixes")
        self.assertEqual(rc.status, "testing")

    def test_recall_needs_reason_notifies_and_blocks_builds(self):
        c, lead = signed_in(self.eng), signed_in(self.lead)
        self.create(c, "1.0.0", [self.rev_a], [binfile()])
        rel = FirmwareRelease.objects.get()
        self.status(lead, rel, "released")
        b = BuildOrder.objects.create(revision=self.rev_a, quantity=1)
        b.firmware_releases.add(rel)
        self.status(lead, rel, "recalled")
        rel.refresh_from_db()
        self.assertEqual(rel.status, "released", "a reason is required")
        self.status(lead, rel, "recalled", "Watchdog resets under load")
        rel.refresh_from_db()
        self.assertEqual(rel.status, "recalled")
        self.assertTrue(Notification.objects.filter(user=self.eng, text__contains="recalled").exists())
        buyer = signed_in(make_user("buyer", role=User.Role.PROCUREMENT))
        self.assertContains(buyer.get(b.get_absolute_url()), "RECALLED")
        buyer.post(reverse("production:build_action", args=[b.pk]), {"action": "start", "force": "1"})
        b.refresh_from_db()
        self.assertEqual(b.status, "planned")

    def test_build_firmware_selection(self):
        c, lead = signed_in(self.eng), signed_in(self.lead)
        self.create(c, "1.0.0", [self.rev_a], [binfile()])
        rel = FirmwareRelease.objects.get()
        self.status(lead, rel, "released")
        b = BuildOrder.objects.create(revision=self.rev_b, quantity=5)
        buyer = signed_in(make_user("buyer", role=User.Role.PROCUREMENT))
        buyer.post(reverse("production:build_action", args=[b.pk]), {"action": "set_firmware", "firmware": [rel.pk]})
        self.assertEqual(list(b.firmware_releases.all()), [rel])
        self.assertContains(buyer.get(b.get_absolute_url()), "not marked compatible with Rev B")

    def test_viewer_and_outsider(self):
        viewer = make_user("viewer", role=User.Role.VIEWER)
        self.p.members.add(viewer)
        self.assertEqual(signed_in(viewer).get(reverse("firmware:project", args=["PWR"])).status_code, 200)
        self.assertEqual(signed_in(viewer).get(reverse("firmware:release_create", args=["PWR", self.fw.slug])).status_code, 403)
        self.assertEqual(signed_in(self.outsider).get(reverse("firmware:project", args=["PWR"])).status_code, 403)

    @mock.patch.dict(os.environ, {"WORKBENCH_GITHUB_WEBHOOK_SECRET": "s3cret"})
    def test_github_release_event_creates_testing_release(self):
        Firmware.objects.create(project=self.p, name="Bootloader", tag_prefix="boot-v")
        Firmware.objects.filter(pk=self.fw.pk).update()

        def send(tag, delivery):
            body = json.dumps({"action": "published", "repository": {"full_name": "acme/power"},
                               "release": {"tag_name": tag, "body": "Notes from GitHub", "html_url": "https://github.com/acme/power/releases/1",
                                           "author": {"login": "bilal"}}}).encode()
            sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            return self.client.post(reverse("integrations:github_webhook"), body, content_type="application/json",
                                    HTTP_X_GITHUB_EVENT="release", HTTP_X_GITHUB_DELIVERY=delivery, HTTP_X_HUB_SIGNATURE_256=sig)
        make_user("someone")
        send("v1.5.0", "d1")
        send("boot-v2.0.0", "d2")
        send("nightly-build", "d3")
        rel = FirmwareRelease.objects.get(firmware=self.fw)
        self.assertEqual((rel.version, rel.status, rel.notes), ("1.5.0", "testing", "Notes from GitHub"))
        self.assertTrue(FirmwareRelease.objects.filter(firmware__name="Bootloader", version="2.0.0").exists())
        self.assertEqual(FirmwareRelease.objects.count(), 2)
