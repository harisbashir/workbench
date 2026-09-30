"""Regression tests for the pre-launch review of projects (checklist, boards, N+1, forms)."""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.projects.forms import TaskForm
from apps.projects.models import Board, Project, Revision, RevisionCheck, Task


class Base(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.project = Project.objects.create(key="PWR", name="Power", lead=self.lead)
        self.project.members.add(self.eng)

    def rev_with_checks(self, **kw):
        rev = Revision.objects.create(project=self.project, name="Rev A", **kw)
        rev.add_default_checks()
        return rev

    def check(self, client, rev, **data):
        return client.post(reverse("projects:revision_check", args=["PWR", rev.pk]), data)


class ReleaseChecklistTests(Base):
    def test_engineer_cannot_delete_checklist_items(self):
        rev = self.rev_with_checks()
        item = rev.checks.first()
        r = self.check(signed_in(self.eng), rev, action="delete", item=item.pk)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(RevisionCheck.objects.filter(pk=item.pk).exists())

    def test_lead_can_delete_checklist_items(self):
        rev = self.rev_with_checks()
        item = rev.checks.first()
        self.check(signed_in(self.lead), rev, action="delete", item=item.pk)
        self.assertFalse(RevisionCheck.objects.filter(pk=item.pk).exists())

    def test_engineer_can_tick_and_add_before_release(self):
        rev = self.rev_with_checks()
        c = signed_in(self.eng)
        item = rev.checks.first()
        self.check(c, rev, action="toggle", item=item.pk)
        self.check(c, rev, action="add", text="Thermal test passed")
        item.refresh_from_db()
        self.assertIsNotNone(item.done_at)
        self.assertTrue(rev.checks.filter(text="Thermal test passed").exists())

    def test_released_checklist_is_locked_for_engineers(self):
        rev = self.rev_with_checks(status=Revision.Status.RELEASED)
        c = signed_in(self.eng)
        item = rev.checks.first()
        self.assertEqual(self.check(c, rev, action="toggle", item=item.pk).status_code, 403)
        self.assertEqual(self.check(c, rev, action="add", text="x").status_code, 403)
        self.assertEqual(self.check(signed_in(self.lead), rev, action="toggle", item=item.pk).status_code, 302)

    def test_bad_item_id_is_404_not_500(self):
        rev = self.rev_with_checks()
        self.assertEqual(self.check(signed_in(self.eng), rev, action="toggle", item="abc").status_code, 404)

    def test_engineer_cannot_release_even_with_complete_checklist(self):
        rev = self.rev_with_checks()
        rev.checks.update(done_at="2026-09-01T00:00:00Z")
        r = signed_in(self.eng).post(reverse("projects:revision_edit", args=["PWR", rev.pk]),
                                     {"board": rev.board_id, "name": "Rev A", "status": "released"})
        self.assertEqual(r.status_code, 200)
        rev.refresh_from_db()
        self.assertEqual(rev.status, Revision.Status.DESIGN)

    def test_lead_releases_with_complete_checklist(self):
        rev = self.rev_with_checks()
        rev.checks.update(done_at="2026-09-01T00:00:00Z")
        r = signed_in(self.lead).post(reverse("projects:revision_edit", args=["PWR", rev.pk]),
                                      {"board": rev.board_id, "name": "Rev A", "status": "released"})
        self.assertEqual(r.status_code, 302)
        rev.refresh_from_db()
        self.assertEqual(rev.status, Revision.Status.RELEASED)

    def test_engineer_cannot_unrelease(self):
        rev = self.rev_with_checks(status=Revision.Status.RELEASED)
        signed_in(self.eng).post(reverse("projects:revision_edit", args=["PWR", rev.pk]),
                                 {"board": rev.board_id, "name": "Rev A", "status": "design"})
        rev.refresh_from_db()
        self.assertEqual(rev.status, Revision.Status.RELEASED)

    def test_checklist_controls_hidden_when_not_allowed(self):
        rev = self.rev_with_checks(status=Revision.Status.RELEASED)
        html = signed_in(self.eng).get(rev.get_absolute_url()).content.decode()
        self.assertNotIn('value="toggle"', html)
        self.assertNotIn('value="delete"', html)


class BoardTests(Base):
    def test_hardware_page_does_not_create_board_on_get(self):
        signed_in(self.lead).get(reverse("projects:hardware", args=["PWR"]))
        signed_in(self.lead).get(reverse("projects:revision_create", args=["PWR"]))
        self.assertFalse(self.project.boards.exists())

    def test_revision_create_post_still_makes_default_board(self):
        r = signed_in(self.eng).post(reverse("projects:revision_create", args=["PWR"]), {"name": "Rev A", "status": "design"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.project.boards.count(), 1)

    def test_engineer_cannot_delete_board(self):
        b = Board.objects.create(project=self.project, name="Power board")
        r = signed_in(self.eng).post(reverse("projects:hw_board_edit", args=["PWR", b.pk]), {"action": "delete"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Board.objects.filter(pk=b.pk).exists())

    def test_board_with_diagrams_is_not_deleted(self):
        from apps.diagrams.models import Diagram
        b = Board.objects.create(project=self.project, name="Power board")
        Diagram.objects.create(project=self.project, board=b, name="Block")
        signed_in(self.lead).post(reverse("projects:hw_board_edit", args=["PWR", b.pk]), {"action": "delete"})
        self.assertTrue(Board.objects.filter(pk=b.pk).exists())

    def test_lead_deletes_empty_board(self):
        b = Board.objects.create(project=self.project, name="Power board")
        signed_in(self.lead).post(reverse("projects:hw_board_edit", args=["PWR", b.pk]), {"action": "delete"})
        self.assertFalse(Board.objects.filter(pk=b.pk).exists())

    def test_procurement_sees_no_revision_or_board_links(self):
        Revision.objects.create(project=self.project, name="Rev A")
        c = signed_in(self.buyer)
        for url in (reverse("projects:detail", args=["PWR"]), reverse("projects:hardware", args=["PWR"]),
                    self.project.boards.first().get_absolute_url()):
            html = c.get(url).content.decode()
            self.assertNotIn(reverse("projects:revision_create", args=["PWR"]), html, url)
            self.assertNotIn(reverse("projects:hw_board_create", args=["PWR"]), html, url)
            self.assertNotIn(reverse("projects:edit", args=["PWR"]), html, url)
        self.assertEqual(c.get(reverse("projects:revision_create", args=["PWR"])).status_code, 403)


class QueryCountTests(Base):
    def count(self, url):
        c = signed_in(self.eng)
        c.get(url)  # warm up (session, site settings)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(c.get(url).status_code, 200)
        return len(ctx.captured_queries)

    def test_board_and_list_queries_do_not_grow_per_task(self):
        Board.objects.create(project=self.project, name="Second board")
        rev = Revision.objects.create(project=self.project, name="Rev A")
        for url in (reverse("projects:board", args=["PWR"]), reverse("projects:task_list", args=["PWR"])):
            Task.objects.all().delete()
            for i in range(3):
                Task.objects.create(project=self.project, title=f"T{i}", revision=rev)
            few = self.count(url)
            for i in range(20):
                Task.objects.create(project=self.project, title=f"U{i}", revision=rev)
            self.assertEqual(self.count(url), few, url)


class TaskTests(Base):
    def test_task_form_keeps_obsolete_revision_and_former_member(self):
        rev = Revision.objects.create(project=self.project, name="Rev A", status=Revision.Status.OBSOLETE)
        gone = make_user("gone")
        task = Task.objects.create(project=self.project, title="x", revision=rev, assignee=gone)
        form = TaskForm(self.project, instance=task)
        self.assertIn(rev, form.fields["revision"].queryset)
        self.assertIn(gone, form.fields["assignee"].queryset)
        self.assertNotIn(gone, TaskForm(self.project).fields["assignee"].queryset)
        self.assertNotIn(rev, TaskForm(self.project).fields["revision"].queryset)

    def test_move_ignores_offsite_next(self):
        task = Task.objects.create(project=self.project, title="x")
        r = signed_in(self.eng).post(reverse("projects:task_move", args=["PWR", task.number]),
                                     {"status": "done", "next": "https://evil.example/"})
        self.assertEqual(r["Location"], task.get_absolute_url())
        r = signed_in(self.eng).post(reverse("projects:task_move", args=["PWR", task.number]),
                                     {"status": "todo", "next": "/projects/PWR/board/"})
        self.assertEqual(r["Location"], "/projects/PWR/board/")

    def test_task_numbers_after_deletion_stay_unique(self):
        a = Task.objects.create(project=self.project, title="a")
        b = Task.objects.create(project=self.project, title="b")
        b.delete()
        c = Task.objects.create(project=self.project, title="c")
        self.assertEqual((a.number, c.number), (1, 2))

    def test_task_description_with_control_characters_renders(self):
        task = Task.objects.create(project=self.project, title="x", description="a \x013\x01 b")
        self.assertEqual(signed_in(self.eng).get(task.get_absolute_url()).status_code, 200)
