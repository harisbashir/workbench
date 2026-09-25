import json

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.chat.models import Channel
from apps.core.models import Notification
from apps.core.testing import make_user, signed_in
from apps.projects.models import Project, Task


class ProjectTests(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.reviewer = make_user("rev")
        self.outsider = make_user("out")
        self.viewer = make_user("view", role=User.Role.VIEWER)
        self.project = Project.objects.create(key="PWR", name="Power board", lead=self.lead)
        self.project.members.add(self.eng, self.reviewer, self.viewer)

    def test_lead_creates_project_with_channel(self):
        c = signed_in(self.lead)
        r = c.post(reverse("projects:create"), {"name": "Sensor", "key": "sens", "status": "active", "members": [self.eng.pk]})
        self.assertEqual(r.status_code, 302)
        p = Project.objects.get(key="SENS")
        self.assertTrue(Channel.objects.filter(project=p, kind="project").exists())
        self.assertTrue(Notification.objects.filter(user=self.eng).exists())

    def test_engineer_cannot_create_projects(self):
        self.assertEqual(signed_in(self.eng).get(reverse("projects:create")).status_code, 403)

    def test_non_member_cannot_see_project(self):
        c = signed_in(self.outsider)
        self.assertEqual(c.get(reverse("projects:detail", args=["PWR"])).status_code, 403)
        self.assertNotContains(c.get(reverse("projects:list")), "Power board")

    def test_task_numbers_are_sequential_per_project(self):
        a = Task.objects.create(project=self.project, title="A")
        b = Task.objects.create(project=self.project, title="B")
        other = Project.objects.create(key="SENS", name="Sensor")
        c = Task.objects.create(project=other, title="C")
        self.assertEqual((a.key, b.key, c.key), ("PWR-1", "PWR-2", "SENS-1"))

    def test_create_task_and_assign(self):
        c = signed_in(self.eng)
        r = c.post(reverse("projects:task_create", args=["PWR"]), {
            "title": "Route USB", "kind": "pcb", "assignee": self.reviewer.pk, "priority": 2, "status": "todo"})
        task = Task.objects.get(title="Route USB")
        self.assertRedirects(r, task.get_absolute_url(), fetch_redirect_response=False)
        self.assertTrue(Notification.objects.filter(user=self.reviewer, text__contains="assigned you").exists())

    def test_drag_to_review_notifies_reviewer(self):
        task = Task.objects.create(project=self.project, title="FW", assignee=self.eng, reviewer=self.reviewer)
        c = signed_in(self.eng)
        r = c.post(reverse("projects:task_move", args=["PWR", task.number]), json.dumps({"status": "review"}),
                   content_type="application/json")
        self.assertEqual(r.json()["status"], "review")
        self.assertTrue(Notification.objects.filter(user=self.reviewer, text__contains="ready for your review").exists())
        r = c.post(reverse("projects:task_move", args=["PWR", task.number]), json.dumps({"status": "done"}), content_type="application/json")
        task.refresh_from_db()
        self.assertIsNotNone(task.completed_at)

    def test_invalid_status_rejected(self):
        task = Task.objects.create(project=self.project, title="FW")
        r = signed_in(self.eng).post(reverse("projects:task_move", args=["PWR", task.number]), json.dumps({"status": "hacked"}),
                                     content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_viewer_is_read_only(self):
        task = Task.objects.create(project=self.project, title="FW")
        c = signed_in(self.viewer)
        self.assertEqual(c.get(task.get_absolute_url()).status_code, 200)
        self.assertEqual(c.get(reverse("projects:task_create", args=["PWR"])).status_code, 403)
        self.assertEqual(c.post(task.get_absolute_url(), {"body": "hi"}).status_code, 403)

    def test_mention_in_comment_notifies(self):
        task = Task.objects.create(project=self.project, title="FW")
        signed_in(self.eng).post(task.get_absolute_url(), {"body": "@rev can you look?"})
        self.assertTrue(Notification.objects.filter(user=self.reviewer, text__contains="mentioned you").exists())

    def test_board_renders_with_filters(self):
        Task.objects.create(project=self.project, title="Mine", assignee=self.eng)
        Task.objects.create(project=self.project, title="Theirs", assignee=self.reviewer)
        r = signed_in(self.eng).get(reverse("projects:board", args=["PWR"]) + "?assignee=me")
        self.assertContains(r, "Mine")
        self.assertNotContains(r, "Theirs")


class RevisionChecklistTests(TestCase):
    def setUp(self):
        from apps.projects.models import Revision
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.project = Project.objects.create(key="PWR", name="Power board", lead=self.lead)
        self.project.members.add(self.eng)
        self.Revision = Revision

    def test_new_revision_gets_standard_checklist(self):
        signed_in(self.eng).post(reverse("projects:revision_create", args=["PWR"]), {"name": "Rev B", "status": "design"})
        rev = self.Revision.objects.get()
        self.assertGreaterEqual(rev.checks.count(), 8)

    def test_release_blocked_until_checklist_done(self):
        c = signed_in(self.lead)
        c.post(reverse("projects:revision_create", args=["PWR"]), {"name": "Rev B", "status": "design"})
        rev = self.Revision.objects.get()
        r = c.post(reverse("projects:revision_edit", args=["PWR", rev.pk]), {"name": "Rev B", "status": "released"})
        self.assertContains(r, "Finish the release checklist")
        for item in rev.checks.all():
            signed_in(self.eng).post(reverse("projects:revision_check", args=["PWR", rev.pk]), {"action": "toggle", "item": item.pk})
        self.assertTrue(Notification.objects.filter(user=self.lead, text__contains="checklist is complete").exists())
        c.post(reverse("projects:revision_edit", args=["PWR", rev.pk]), {"name": "Rev B", "status": "released"})
        rev.refresh_from_db()
        self.assertEqual(rev.status, "released")
        self.assertEqual(rev.checks.first().done_by, self.eng)

    def test_blocked_task_notifies_lead(self):
        task = Task.objects.create(project=self.project, title="Layout", assignee=self.eng, priority=2)
        signed_in(self.eng).post(reverse("projects:task_edit", args=["PWR", task.number]), {
            "title": "Layout", "kind": "pcb", "priority": 2, "status": "in_progress", "assignee": self.eng.pk,
            "blocked_reason": "Waiting for connector footprint"})
        self.assertTrue(Notification.objects.filter(user=self.lead, text__contains="is blocked").exists())
        r = signed_in(self.lead).get(reverse("projects:board", args=["PWR"]) + "?blocked=1")
        self.assertContains(r, "Waiting for connector")
        self.assertContains(signed_in(self.lead).get("/"), "Waiting for connector")
