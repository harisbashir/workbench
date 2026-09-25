import hashlib
import hmac
import json
import os
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.chat.models import Channel, Message
from apps.core.models import Notification
from apps.core.testing import make_user
from apps.integrations.models import PullRequest, WebhookDelivery
from apps.projects.models import Project, Task

SECRET = "test-webhook-secret"


def pr_event(action, number=7, title="PWR-1 Add ESD", branch="pwr-1-esd", merged=False, state="open", draft=False):
    return {"action": action, "repository": {"full_name": "acme/power", "default_branch": "main"},
            "pull_request": {"number": number, "title": title, "html_url": f"https://github.com/acme/power/pull/{number}",
                             "user": {"login": "sana"}, "state": state, "merged": merged, "draft": draft, "body": "",
                             "head": {"ref": branch, "sha": "a" * 40}, "created_at": "2026-09-20T10:00:00Z"}}


@mock.patch.dict(os.environ, {"WORKBENCH_GITHUB_WEBHOOK_SECRET": SECRET})
class WebhookTests(TestCase):
    def setUp(self):
        self.eng = make_user("sana", github_username="sana")
        self.reviewer = make_user("haris")
        self.project = Project.objects.create(key="PWR", name="Power", github_repo="acme/power")
        Channel.objects.create(name="pwr", slug="pwr", kind="project", project=self.project)
        self.task = Task.objects.create(project=self.project, title="ESD", assignee=self.eng, reviewer=self.reviewer,
                                        status=Task.Status.IN_PROGRESS)
        self.n = 0

    def send(self, event, payload, secret=SECRET, delivery=None):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.n += 1
        return self.client.post(reverse("integrations:github_webhook"), body, content_type="application/json",
                                HTTP_X_GITHUB_EVENT=event, HTTP_X_GITHUB_DELIVERY=delivery or f"d-{self.n}",
                                HTTP_X_HUB_SIGNATURE_256=sig)

    def test_rejects_bad_signature(self):
        r = self.send("pull_request", pr_event("opened"), secret="wrong")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(PullRequest.objects.exists())

    def test_rejects_when_no_secret_configured(self):
        with mock.patch.dict(os.environ, {"WORKBENCH_GITHUB_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.send("ping", {"zen": "hi"}).status_code, 403)

    def test_replay_is_ignored(self):
        self.send("pull_request", pr_event("opened"), delivery="same")
        r = self.send("pull_request", pr_event("opened"), delivery="same")
        self.assertContains(r, "Duplicate")
        self.assertEqual(WebhookDelivery.objects.filter(delivery_id="same").count(), 1)

    def test_ping(self):
        self.assertEqual(self.send("ping", {"zen": "hi", "repository": {"full_name": "acme/power"}}).json()["status"], "processed")

    def test_pr_lifecycle_moves_task(self):
        self.send("pull_request", pr_event("opened"))
        self.task.refresh_from_db()
        pr = PullRequest.objects.get()
        self.assertEqual(self.task.status, Task.Status.REVIEW)
        self.assertIn(self.task, pr.tasks.all())
        self.assertTrue(Notification.objects.filter(user=self.reviewer).exists())
        self.assertTrue(Message.objects.filter(kind="github", body__contains="opened PR #7").exists())

        self.send("pull_request_review", {"action": "submitted", "repository": {"full_name": "acme/power"},
                                          "review": {"state": "changes_requested", "user": {"login": "haris"}, "html_url": "https://github.com/x"},
                                          "pull_request": {"number": 7, "title": "PWR-1 Add ESD", "html_url": "https://github.com/x"}})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)

        self.send("pull_request", pr_event("closed", merged=True, state="closed"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertEqual(PullRequest.objects.get().state, "merged")

    def test_draft_pr_does_not_move_task(self):
        self.send("pull_request", pr_event("opened", draft=True))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)

    def test_other_projects_keys_are_not_linked(self):
        other = Project.objects.create(key="SENS", name="Sensor")
        t = Task.objects.create(project=other, title="x")
        self.send("pull_request", pr_event("opened", title="SENS-1 sneaky", branch="x"))
        self.assertFalse(t.pull_requests.exists())

    def test_failed_ci_notifies_assignee(self):
        self.send("pull_request", pr_event("opened"))
        self.send("workflow_run", {"action": "completed", "repository": {"full_name": "acme/power"},
                                   "workflow_run": {"name": "KiBot", "conclusion": "failure", "head_sha": "a" * 40,
                                                    "html_url": "https://github.com/run/1", "head_branch": "pwr-1-esd"}})
        self.assertEqual(PullRequest.objects.get().checks, "failure")
        self.assertTrue(Notification.objects.filter(user=self.eng, text__contains="KiBot failed").exists())

    def test_unknown_repository_ignored(self):
        payload = pr_event("opened")
        payload["repository"]["full_name"] = "someone/else"
        self.assertEqual(self.send("pull_request", payload).json()["status"], "ignored")

    def test_push_links_commits(self):
        self.send("push", {"ref": "refs/heads/main", "repository": {"full_name": "acme/power", "default_branch": "main"},
                           "pusher": {"name": "sana"}, "compare": "https://github.com/c",
                           "commits": [{"message": "PWR-1 place TVS diode", "url": "https://github.com/c/1", "author": {"username": "sana"}}]})
        self.assertTrue(self.task.activities.filter(text__contains="place TVS diode").exists())
