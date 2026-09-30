"""Regression tests for the pre-launch review of timesheets."""
import csv
import io
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.core.testing import make_user, signed_in
from apps.projects.models import Project, Task
from apps.timesheets.models import TimeEntry


class TimesheetFixTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.eng)
        self.task = Task.objects.create(project=self.project, title="=HYPERLINK(\"http://evil\")")
        self.c = signed_in(self.eng)

    def log(self, **data):
        return self.c.post(reverse("time:log_task", args=["PWR", self.task.number]), data)

    def test_nan_and_garbage_hours_are_rejected_not_500(self):
        for h in ("NaN", "Infinity", "-inf", "abc", ""):
            self.assertEqual(self.log(hours=h).status_code, 302, h)
        self.assertFalse(TimeEntry.objects.exists())

    def test_valid_hours_are_logged(self):
        self.log(hours="1.5", date="2026-09-01")
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("1.50"))

    def test_bad_date_falls_back_to_today(self):
        self.log(hours="1", date="2026-02-30")
        self.assertEqual(TimeEntry.objects.count(), 1)

    def test_extreme_weeks_do_not_crash(self):
        for week in ("0001-01-01", "9999-12-31", "nonsense"):
            self.assertEqual(self.c.get(reverse("time:mine") + "?week=" + week).status_code, 200, week)
            self.assertEqual(self.c.get(reverse("time:report") + "?week=" + week).status_code, 200, week)

    def test_csv_export_neutralises_formulas(self):
        TimeEntry.objects.create(user=self.eng, project=self.project, task=self.task, date=date(2026, 9, 1),
                                 hours=Decimal("2"), note="@SUM(A1:A2)")
        r = self.c.get(reverse("time:export") + "?from=2026-09-01&to=2026-09-30")
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[1][5], "'=HYPERLINK(\"http://evil\")")
        self.assertEqual(rows[1][7], "'@SUM(A1:A2)")
        self.assertEqual(rows[1][6], "2.00")

    def test_csv_export_swapped_range(self):
        TimeEntry.objects.create(user=self.eng, project=self.project, date=date(2026, 9, 5), hours=Decimal("1"))
        r = self.c.get(reverse("time:export") + "?from=2026-09-30&to=2026-09-01")
        self.assertEqual(len([row for row in csv.reader(io.StringIO(r.content.decode())) if row]), 3)
