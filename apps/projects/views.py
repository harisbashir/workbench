import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.chat.models import Channel, post_system_message
from apps.core.utils import audit, manager_required, notify

from .forms import AttachmentForm, CommentForm, ProjectForm, RevisionForm, TaskForm
from .models import Project, Revision, Task, TaskAttachment, log_activity

MENTION_RE = re.compile(r"@([\w.@+-]+)")


def _project_for(request, key, edit=False):
    project = get_object_or_404(Project.objects.select_related("lead"), key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project. Ask its lead to add you.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view this project but not change it.")
    return project


def _task_for(request, key, number, edit=False):
    project = _project_for(request, key, edit=edit)
    task = get_object_or_404(Task.objects.select_related("assignee", "reviewer", "revision", "created_by"),
                             project=project, number=number)
    return project, task


# --- Projects ----------------------------------------------------------------

@login_required
def project_list(request):
    show = request.GET.get("show", "active")
    qs = Project.objects.visible_to(request.user).select_related("lead").annotate(
        open_tasks=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE), distinct=True),
        done_tasks=Count("tasks", filter=Q(tasks__status=Task.Status.DONE), distinct=True),
        member_count=Count("members", distinct=True),
    )
    if show != "all":
        qs = qs.exclude(status=Project.Status.ARCHIVED)
    return render(request, "projects/project_list.html", {"projects": qs, "show": show})


@manager_required
def project_edit(request, key=None):
    project = get_object_or_404(Project, key=key.upper()) if key else None
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        creating = project is None
        project = form.save()
        if creating:
            Channel.objects.create(
                name=project.key.lower(), slug=Channel.unique_slug(project.key.lower()),
                kind=Channel.Kind.PROJECT, project=project, created_by=request.user,
                topic=f"Discussion for {project.name}. GitHub and task updates are posted here automatically.",
            )
            log_activity(project, "created the project", actor=request.user)
            for m in project.members.exclude(pk=request.user.pk):
                notify(m, f"You were added to project {project.key} · {project.name}", project.get_absolute_url())
            messages.success(request, f"Project {project.key} created. Next: add a revision (e.g. Rev A), then create tasks.")
        else:
            messages.success(request, "Project saved.")
        audit(request, "project.created" if creating else "project.updated", project)
        return redirect(project)
    return render(request, "projects/project_form.html", {"form": form, "project": project})


@login_required
def project_detail(request, key):
    project = _project_for(request, key)
    counts = {s: 0 for s, _ in Task.Status.choices}
    for row in project.tasks.values("status").annotate(n=Count("id")):
        counts[row["status"]] = row["n"]
    total = sum(counts.values())
    progress = round(100 * counts["done"] / total) if total else 0
    revisions = project.revisions.annotate(task_count=Count("tasks", distinct=True), bom_count=Count("bom_lines", distinct=True))
    return render(request, "projects/project_detail.html", {
        "project": project,
        "counts": counts, "total": total, "progress": progress, "progress_step": progress // 5 * 5,
        "revisions": revisions,
        "activity": project.activities.select_related("actor", "task")[:25],
        "pulls": project.pull_requests.exclude(state="closed")[:10],
        "members": project.members.all(),
        "overdue": project.tasks.filter(due_date__lt=timezone.localdate()).exclude(status=Task.Status.DONE).select_related("assignee"),
        "can_edit": project.can_edit(request.user),
    })


@login_required
def board(request, key):
    project = _project_for(request, key)
    tasks = project.tasks.select_related("assignee", "reviewer", "revision").prefetch_related("pull_requests")
    f = {k: request.GET.get(k, "") for k in ("assignee", "kind", "revision", "q")}
    if f["assignee"] == "me":
        tasks = tasks.filter(assignee=request.user)
    elif f["assignee"] == "none":
        tasks = tasks.filter(assignee__isnull=True)
    elif f["assignee"].isdigit():
        tasks = tasks.filter(assignee_id=int(f["assignee"]))
    if f["kind"]:
        tasks = tasks.filter(kind=f["kind"])
    if f["revision"].isdigit():
        tasks = tasks.filter(revision_id=int(f["revision"]))
    if f["q"]:
        tasks = tasks.filter(Q(title__icontains=f["q"]) | Q(description__icontains=f["q"]))
    # Hide tasks finished more than 14 days ago to keep the board readable.
    cutoff = timezone.now() - timezone.timedelta(days=14)
    tasks = tasks.exclude(status=Task.Status.DONE, completed_at__lt=cutoff)
    columns = [{"status": s, "label": label, "tasks": [t for t in tasks if t.status == s]} for s, label in Task.Status.choices]
    people = User.objects.filter(Q(projects=project) | Q(led_projects=project)).distinct()
    return render(request, "projects/board.html", {
        "project": project, "columns": columns, "filters": f, "people": people,
        "kinds": Task.Kind.choices, "revisions": project.revisions.all(),
        "can_edit": project.can_edit(request.user), "filtered": any(f.values()),
    })


@login_required
def task_list(request, key):
    project = _project_for(request, key)
    tasks = project.tasks.select_related("assignee", "reviewer", "revision")
    status = request.GET.get("status", "open")
    if status == "open":
        tasks = tasks.exclude(status=Task.Status.DONE)
    elif status in dict(Task.Status.choices):
        tasks = tasks.filter(status=status)
    return render(request, "projects/task_list.html", {"project": project, "tasks": tasks, "status": status,
                                                       "statuses": Task.Status.choices,
                                                       "can_edit": project.can_edit(request.user)})


# --- Revisions ---------------------------------------------------------------

@login_required
def revision_edit(request, key, pk=None):
    project = _project_for(request, key, edit=True)
    revision = get_object_or_404(Revision, pk=pk, project=project) if pk else None
    form = RevisionForm(request.POST or None, instance=revision)
    if request.method == "POST" and form.is_valid():
        creating = revision is None
        old_status = revision.status if revision else None
        rev = form.save(commit=False)
        rev.project = project
        rev.save()
        if creating:
            log_activity(project, f"added revision {rev.name}", actor=request.user)
            messages.success(request, f"{rev.name} added. Import its BOM from the Parts & BOM tab.")
        elif old_status != rev.status:
            log_activity(project, f"changed {rev.name} to “{rev.get_status_display()}”", actor=request.user)
            post_system_message(project, f"{request.user.display_name} marked {rev.name} as {rev.get_status_display()}.")
        audit(request, "revision.saved", rev, status=rev.status)
        return redirect(project)
    return render(request, "projects/revision_form.html", {"form": form, "project": project, "revision": revision})


# --- Tasks -------------------------------------------------------------------

def _notify_mentions(request, text, task, exclude=()):
    for username in set(MENTION_RE.findall(text)):
        u = User.objects.filter(username__iexact=username.rstrip("."), is_active=True).first()
        if u and u.pk not in exclude and task.project.can_view(u):
            notify(u, f"{request.user.display_name} mentioned you on {task.key}", task.get_absolute_url())


@login_required
def task_create(request, key):
    project = _project_for(request, key, edit=True)
    initial = {"status": request.GET.get("status", Task.Status.TODO)}
    if request.GET.get("revision", "").isdigit():
        initial["revision"] = int(request.GET["revision"])
    form = TaskForm(project, request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        task = form.save(commit=False)
        task.project = project
        task.created_by = request.user
        task.save()
        log_activity(project, f"created {task.key} “{task.title}”", actor=request.user, task=task, url=task.get_absolute_url())
        if task.assignee and task.assignee != request.user:
            notify(task.assignee, f"{request.user.display_name} assigned you {task.key}: {task.title}", task.get_absolute_url())
        _notify_mentions(request, task.description, task, exclude={request.user.pk})
        messages.success(request, f"Created {task.key}.")
        if "add_another" in request.POST:
            return redirect("projects:task_create", key=project.key)
        return redirect(task)
    return render(request, "projects/task_form.html", {"form": form, "project": project})


@login_required
def task_edit(request, key, number):
    project, task = _task_for(request, key, number, edit=True)
    before = {"assignee": task.assignee_id, "status": task.status, "reviewer": task.reviewer_id}
    form = TaskForm(project, request.POST or None, instance=task)
    if request.method == "POST" and form.is_valid():
        task = form.save()
        _after_status_change(request, task, before["status"])
        if task.assignee_id != before["assignee"] and task.assignee and task.assignee != request.user:
            notify(task.assignee, f"{request.user.display_name} assigned you {task.key}: {task.title}", task.get_absolute_url())
        messages.success(request, f"Saved {task.key}.")
        return redirect(task)
    return render(request, "projects/task_form.html", {"form": form, "project": project, "task": task})


def _after_status_change(request, task, old_status):
    if task.status == old_status:
        return
    if task.status == Task.Status.DONE and not task.completed_at:
        task.completed_at = timezone.now()
        task.save(update_fields=["completed_at"])
    elif task.status != Task.Status.DONE and task.completed_at:
        task.completed_at = None
        task.save(update_fields=["completed_at"])
    label = task.get_status_display()
    log_activity(task.project, f"moved {task.key} to {label}", actor=request.user, task=task, url=task.get_absolute_url())
    if task.status == Task.Status.REVIEW and task.reviewer and task.reviewer != request.user:
        notify(task.reviewer, f"{task.key} is ready for your review: {task.title}", task.get_absolute_url())
        post_system_message(task.project, f"{task.key} “{task.title}” is ready for review by {task.reviewer.display_name}.", task.get_absolute_url())
    if task.status == Task.Status.DONE and task.assignee and task.assignee != request.user:
        notify(task.assignee, f"{request.user.display_name} marked {task.key} as done", task.get_absolute_url())


@login_required
@require_POST
def task_move(request, key, number):
    """Change status — used by board drag-and-drop (JSON) and buttons (form)."""
    project, task = _task_for(request, key, number, edit=True)
    if request.content_type == "application/json":
        try:
            status = json.loads(request.body).get("status")
        except (ValueError, AttributeError):
            status = None
    else:
        status = request.POST.get("status")
    if status not in dict(Task.Status.choices):
        return JsonResponse({"ok": False, "error": "Unknown status"}, status=400)
    old = task.status
    task.status = status
    task.save(update_fields=["status", "updated_at"])
    _after_status_change(request, task, old)
    if request.content_type == "application/json":
        return JsonResponse({"ok": True, "status": status, "label": task.get_status_display()})
    messages.success(request, f"{task.key} moved to {task.get_status_display()}.")
    return redirect(request.POST.get("next") or task.get_absolute_url())


@login_required
def task_detail(request, key, number):
    project, task = _task_for(request, key, number)
    can_edit = project.can_edit(request.user)
    form = CommentForm(request.POST or None)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied("Your account can't comment on this project.")
        if form.is_valid():
            c = form.save(commit=False)
            c.task, c.author = task, request.user
            c.save()
            log_activity(project, f"commented on {task.key}", actor=request.user, task=task, url=task.get_absolute_url())
            notified = {request.user.pk}
            for person in (task.assignee, task.reviewer):
                if person and person.pk not in notified:
                    notify(person, f"{request.user.display_name} commented on {task.key}", task.get_absolute_url())
                    notified.add(person.pk)
            _notify_mentions(request, c.body, task, exclude=notified)
            return redirect(f"{task.get_absolute_url()}#comments")
    return render(request, "projects/task_detail.html", {
        "project": project, "task": task, "form": form, "can_edit": can_edit,
        "comments": task.comments.select_related("author"),
        "attachments": task.attachments.select_related("uploaded_by"),
        "attach_form": AttachmentForm(),
        "pulls": task.pull_requests.all(),
        "activity": task.activities.select_related("actor")[:20],
        "statuses": Task.Status.choices,
        "branch_name": f"{task.key.lower()}-" + re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40],
    })


@login_required
@require_POST
def task_attach(request, key, number):
    project, task = _task_for(request, key, number, edit=True)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        f = form.cleaned_data["file"]
        att = TaskAttachment.objects.create(task=task, file=f, name=f.name[:255], size=f.size, uploaded_by=request.user)
        log_activity(project, f"attached {att.name} to {task.key}", actor=request.user, task=task, url=task.get_absolute_url())
        audit(request, "file.uploaded", att, task=task.key, name=att.name)
        messages.success(request, f"Uploaded {att.name}.")
    else:
        messages.error(request, " ".join(form.errors.get("file", ["Upload failed."])))
    return redirect(f"{task.get_absolute_url()}#files")


@login_required
@require_POST
def task_delete(request, key, number):
    project, task = _task_for(request, key, number, edit=True)
    if not (request.user.can_manage_projects or task.created_by_id == request.user.pk):
        raise PermissionDenied("Only the person who created a task, or a lead, can delete it.")
    audit(request, "task.deleted", task, title=task.title)
    log_activity(project, f"deleted {task.key} “{task.title}”", actor=request.user)
    task.delete()
    messages.success(request, "Task deleted.")
    return redirect("projects:board", key=project.key)
