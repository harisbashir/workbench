from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.projects.models import Project, Task
from apps.timesheets.models import TimeEntry


class TimesheetTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng", first_name="Bilal")
        self.other = make_user("other", first_name="Sana")
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.eng, self.other)
        self.task = Task.objects.create(project=self.p, title="Brown-out")

    def test_log_from_task_page(self):
        c = signed_in(self.eng)
        c.post(reverse("time:log_task", args=["PWR", self.task.number]), {"hours": "1.5", "note": "PVD tests"})
        e = TimeEntry.objects.get()
        self.assertEqual((e.hours, e.project, e.task, e.user), (1.5, self.p, self.task, self.eng))
        self.assertContains(c.get(self.task.get_absolute_url()), "1.50 h logged")
        c.post(reverse("time:log_task", args=["PWR", self.task.number]), {"hours": "40"})
        self.assertEqual(TimeEntry.objects.count(), 1)

    def test_timesheet_form_fills_project_from_task(self):
        c = signed_in(self.eng)
        today = timezone.localdate()
        c.post(reverse("time:mine"), {"date": today.isoformat(), "hours": "2", "task": self.task.pk})
        self.assertEqual(TimeEntry.objects.get().project, self.p)
        r = c.get(reverse("time:mine"))
        self.assertContains(r, "Brown-out")

    def test_cannot_log_to_other_projects(self):
        hidden = Project.objects.create(key="SEC", name="Secret")
        r = signed_in(self.eng).post(reverse("time:mine"), {"date": date.today().isoformat(), "hours": "1", "project": hidden.pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(TimeEntry.objects.exists())

    def test_only_owner_deletes(self):
        e = TimeEntry.objects.create(user=self.eng, project=self.p, date=date.today(), hours=1)
        self.assertEqual(signed_in(self.other).post(reverse("time:delete", args=[e.pk])).status_code, 404)

    def test_weekly_report_scope(self):
        today = timezone.localdate()
        TimeEntry.objects.create(user=self.eng, project=self.p, date=today, hours=3)
        TimeEntry.objects.create(user=self.other, project=self.p, date=today, hours=5)
        self.task.status = Task.Status.DONE
        self.task.completed_at = timezone.now()
        self.task.save()
        lead = signed_in(self.lead).get(reverse("time:report"))
        self.assertContains(lead, "Sana")
        self.assertContains(lead, "Bilal")
        self.assertContains(lead, "Brown-out")
        own = signed_in(self.eng).get(reverse("time:report"))
        self.assertNotContains(own, "Sana")
        last_week = (today - timedelta(days=7)).isoformat()
        self.assertNotContains(signed_in(self.lead).get(reverse("time:report") + f"?week={last_week}"), "Bilal")

    def test_csv_export(self):
        TimeEntry.objects.create(user=self.eng, project=self.p, task=self.task, date=date.today(), hours=2, note="x")
        r = signed_in(self.lead).get(reverse("time:export"))
        body = r.content.decode()
        self.assertIn("PWR-1", body)
        self.assertIn("Brown-out", body)
