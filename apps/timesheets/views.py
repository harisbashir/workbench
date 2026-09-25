import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.production.models import BuildOrder
from apps.projects.models import Activity, Project, Task

from .models import TimeEntry


class DateInput(forms.DateInput):
    input_type = "date"


class TimeEntryForm(forms.ModelForm):
    class Meta:
        model = TimeEntry
        fields = ["date", "hours", "project", "task", "note"]
        widgets = {"date": DateInput(), "hours": forms.NumberInput(attrs={"step": "0.25", "min": "0.25", "max": "16"}),
                   "note": forms.TextInput(attrs={"placeholder": "What did you work on?"})}
        help_texts = {"task": "Optional. Choosing a task fills in its project."}

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        visible = Project.objects.visible_to(user).exclude(status=Project.Status.ARCHIVED)
        self.fields["project"].queryset = visible
        self.fields["project"].required = False
        self.fields["task"].queryset = Task.objects.filter(project__in=visible).exclude(status=Task.Status.DONE).select_related("project")
        self.fields["task"].label_from_instance = lambda t: f"{t.key} · {t.title[:60]}"
        self.fields["task"].empty_label = "No specific task"

    def clean(self):
        data = super().clean()
        if data.get("task"):
            data["project"] = data["task"].project
        if not data.get("project"):
            self.add_error("project", "Choose a project or a task.")
        return data


def week_start(d):
    return d - timedelta(days=d.weekday())


def parse_week(request):
    try:
        d = date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        d = timezone.localdate()
    return week_start(d)


@login_required
def my_time(request):
    start = parse_week(request)
    end = start + timedelta(days=6)
    initial = {"date": timezone.localdate()}
    if request.GET.get("task", "").isdigit():
        initial["task"] = int(request.GET["task"])
    form = TimeEntryForm(request.user, request.POST or None, initial=initial)
    if request.method == "POST":
        if request.user.is_read_only:
            raise PermissionDenied
        if form.is_valid():
            entry = form.save(commit=False)
            entry.user = request.user
            entry.project = form.cleaned_data["project"]
            entry.save()
            messages.success(request, f"Logged {entry.hours} h on {entry.date:%a %d %b}.")
            nxt = request.POST.get("next")
            if nxt and nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(f"{request.path}?week={week_start(entry.date).isoformat()}")
    entries = TimeEntry.objects.filter(user=request.user, date__range=(start, end)).select_related("project", "task", "task__project")
    days = [{"date": start + timedelta(days=i), "entries": [], "total": Decimal(0)} for i in range(7)]
    for e in entries:
        d = days[(e.date - start).days]
        d["entries"].append(e)
        d["total"] += e.hours
    return render(request, "timesheets/my_time.html", {
        "form": form, "days": days, "start": start, "end": end,
        "total": sum((d["total"] for d in days), Decimal(0)),
        "prev": (start - timedelta(days=7)).isoformat(), "next": (start + timedelta(days=7)).isoformat(),
        "this_week": week_start(timezone.localdate()) == start,
    })


@login_required
@require_POST
def delete_entry(request, pk):
    entry = get_object_or_404(TimeEntry, pk=pk, user=request.user)
    week = week_start(entry.date).isoformat()
    entry.delete()
    messages.success(request, "Entry deleted.")
    return redirect(f"/time/?week={week}")


@login_required
@require_POST
def log_for_task(request, key, number):
    """Quick 'log time' form on the task page."""
    task = get_object_or_404(Task.objects.select_related("project"), project__key=key.upper(), number=number)
    if not task.project.can_edit(request.user):
        raise PermissionDenied
    try:
        hours = Decimal(request.POST.get("hours", "0"))
        day = date.fromisoformat(request.POST.get("date") or timezone.localdate().isoformat())
    except Exception:
        hours = Decimal(0)
        day = timezone.localdate()
    if not (Decimal("0.25") <= hours <= Decimal("16")):
        messages.error(request, "Enter between 0.25 and 16 hours.")
    else:
        TimeEntry.objects.create(user=request.user, project=task.project, task=task, date=day, hours=hours,
                                 note=(request.POST.get("note") or "")[:200])
        messages.success(request, f"Logged {hours} h on {task.key}.")
    return redirect(f"{task.get_absolute_url()}#time")


def _people_for(user):
    if user.can_manage_projects:
        return User.objects.filter(is_active=True)
    return User.objects.filter(pk=user.pk)


@login_required
def weekly_report(request):
    """What happened this week: hours, finished work, reviews, blockers, production."""
    start = parse_week(request)
    end = start + timedelta(days=6)
    user = request.user
    visible = Project.objects.visible_to(user)
    people = _people_for(user)
    entries = TimeEntry.objects.filter(date__range=(start, end), user__in=people, project__in=visible)
    by_person = defaultdict(lambda: {"total": Decimal(0), "projects": defaultdict(Decimal)})
    projects_seen = {}
    for e in entries.select_related("user", "project"):
        row = by_person[e.user]
        row["total"] += e.hours
        row["projects"][e.project.key] += e.hours
        projects_seen[e.project.key] = e.project
    keys = sorted(projects_seen)
    table = [{"person": p, "total": r["total"], "cells": [r["projects"].get(k, 0) for k in keys]}
             for p, r in sorted(by_person.items(), key=lambda kv: -kv[1]["total"])]
    col_totals = [sum((r["cells"][i] for r in table), Decimal(0)) for i in range(len(keys))]
    rng = (timezone.make_aware(timezone.datetime.combine(start, timezone.datetime.min.time())),
           timezone.make_aware(timezone.datetime.combine(end + timedelta(days=1), timezone.datetime.min.time())))
    tasks = Task.objects.filter(project__in=visible).select_related("project", "assignee")
    return render(request, "timesheets/weekly_report.html", {
        "start": start, "end": end, "prev": (start - timedelta(days=7)).isoformat(), "next": (start + timedelta(days=7)).isoformat(),
        "keys": keys, "table": table, "col_totals": col_totals, "grand_total": sum(col_totals, Decimal(0)),
        "completed": tasks.filter(completed_at__range=rng).order_by("project__key", "number"),
        "to_review": Activity.objects.filter(project__in=visible, created_at__range=rng, text__contains="to In review").select_related("task", "project"),
        "blocked": tasks.exclude(blocked_reason="").exclude(status=Task.Status.DONE),
        "overdue": tasks.filter(due_date__lt=timezone.localdate()).exclude(status=Task.Status.DONE),
        "created_count": tasks.filter(created_at__range=rng).count(),
        "builds_done": BuildOrder.objects.filter(completed_at__range=rng, revision__project__in=visible).select_related("revision__project"),
        "is_manager": user.can_manage_projects,
    })


@login_required
def export_csv(request):
    """Time entries as CSV (all people for leads/admins, otherwise your own)."""
    try:
        start = date.fromisoformat(request.GET.get("from", ""))
        end = date.fromisoformat(request.GET.get("to", ""))
    except ValueError:
        end = timezone.localdate()
        start = end.replace(day=1)
    entries = TimeEntry.objects.filter(date__range=(start, end), user__in=_people_for(request.user),
                                       project__in=Project.objects.visible_to(request.user)).select_related("user", "project", "task")
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="time-{start}-to-{end}.csv"'
    w = csv.writer(resp)
    w.writerow(["Date", "Person", "Username", "Project", "Task", "Task title", "Hours", "Note"])
    for e in entries.order_by("date", "user__username"):
        w.writerow([e.date, e.user.display_name, e.user.username, e.project.key, e.task.key if e.task else "",
                    e.task.title if e.task else "", e.hours, e.note])
    w.writerow([])
    w.writerow(["", "", "", "", "", "Total", entries.aggregate(s=Sum("hours"))["s"] or 0, ""])
    return resp
