"""Regression tests for the pre-launch review of the GitHub webhook."""
import hashlib
import hmac
import json
import os
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.chat.models import Channel, Message
from apps.core.testing import make_user
from apps.integrations import github
from apps.integrations.models import PullRequest, WebhookDelivery
from apps.projects.models import Project, Task

from .test_github import SECRET, pr_event


@mock.patch.dict(os.environ, {"WORKBENCH_GITHUB_WEBHOOK_SECRET": SECRET})
class WebhookFixTests(TestCase):
    def setUp(self):
        self.eng = make_user("sana", github_username="sana")
        self.project = Project.objects.create(key="PWR", name="Power", github_repo="acme/power")
        Channel.objects.create(name="pwr", slug="pwr", kind="project", project=self.project)
        self.task = Task.objects.create(project=self.project, title="ESD", status=Task.Status.IN_PROGRESS)

    def send(self, event, payload, delivery):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(reverse("integrations:github_webhook"), body, content_type="application/json",
                                HTTP_X_GITHUB_EVENT=event, HTTP_X_GITHUB_DELIVERY=delivery, HTTP_X_HUB_SIGNATURE_256=sig)

    def test_same_body_under_new_delivery_id_is_a_replay(self):
        payload = pr_event("opened")
        self.send("pull_request", payload, "d1")
        r = self.send("pull_request", payload, "d2")
        self.assertContains(r, "Duplicate")
        self.assertEqual(Message.objects.filter(kind="github").count(), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_errored_delivery_can_be_redelivered(self):
        payload = pr_event("opened")
        with mock.patch.dict(github.HANDLERS, {"pull_request": mock.Mock(side_effect=RuntimeError("secret db detail"))}):
            r = self.send("pull_request", payload, "d1")
        self.assertEqual(r.json()["status"], "error")
        self.assertNotIn("secret db detail", r.content.decode())
        self.assertNotIn("secret db detail", WebhookDelivery.objects.get().message)
        r = self.send("pull_request", payload, "d1")  # GitHub "Redeliver" keeps the ID
        self.assertEqual(r.json()["status"], "processed")
        self.assertTrue(PullRequest.objects.exists())
        self.assertContains(self.send("pull_request", payload, "d1"), "Duplicate")

    def test_non_object_payload_is_an_error_not_a_crash(self):
        r = self.send("pull_request", b"[1, 2]", "d1")
        self.assertEqual(r.json()["status"], "error")

    def test_lowercase_branch_links_task(self):
        payload = pr_event("opened", title="Add ESD protection", branch="pwr-1-add-esd")
        self.send("pull_request", payload, "d1")
        self.task.refresh_from_db()
        self.assertEqual(self.task.pull_requests.count(), 1)
        self.assertEqual(self.task.status, Task.Status.REVIEW)

    def test_key_of_other_project_or_inside_word_is_not_linked(self):
        self.assertEqual(github._tasks_in(self.project, "sens-1 xpwr-1 PWR-99"), [])
        self.assertEqual(github._tasks_in(self.project, "feature/Pwr-1"), [self.task])

    def push(self, ref, commits, delivery):
        return self.send("push", {"ref": ref, "repository": {"full_name": "acme/power", "default_branch": "main"},
                                  "commits": commits, "pusher": {"name": "sana"}, "compare": "https://x"}, delivery)

    def test_push_with_empty_commit_message(self):
        r = self.push("refs/heads/main", [{"message": "", "url": "https://x/1"}, {"message": "PWR-1 fix", "url": "https://x/2"}], "p1")
        self.assertEqual(r.json()["status"], "processed")
        self.assertTrue(self.task.activities.filter(text__contains="PWR-1 fix").exists())

    def test_tag_push_is_not_announced_as_branch_push(self):
        self.push("refs/tags/main", [{"message": "PWR-1 tag", "url": "https://x"}], "p1")
        self.assertFalse(Message.objects.filter(kind="github").exists())
        self.assertFalse(self.task.activities.exists())

    def test_branch_with_slash_is_not_default_branch(self):
        self.push("refs/heads/user/main", [{"message": "PWR-1 wip", "url": "https://x"}], "p1")
        self.assertFalse(Message.objects.filter(kind="github").exists())
        self.assertTrue(self.task.activities.filter(text__contains="user/main").exists())

    def test_branch_from_ref(self):
        self.assertEqual(github.branch_from_ref("refs/heads/feature/x"), "feature/x")
        self.assertEqual(github.branch_from_ref("refs/tags/v1"), "")
