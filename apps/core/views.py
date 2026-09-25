import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.inventory.models import Part
from apps.production.models import BuildOrder, PurchaseOrder
from apps.projects.models import Activity, Project, Task, TaskAttachment

from .models import AuditLog, Notification
from .utils import admin_required


@login_required
def dashboard(request):
    user = request.user
    projects = Project.objects.visible_to(user).filter(status=Project.Status.ACTIVE).annotate(
        open_tasks=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE)),
        review_tasks=Count("tasks", filter=Q(tasks__status=Task.Status.REVIEW)),
    ).select_related("lead")
    my_tasks = Task.objects.filter(assignee=user).exclude(status=Task.Status.DONE).select_related("project")[:8]
    to_review = Task.objects.filter(reviewer=user, status=Task.Status.REVIEW).select_related("project")[:8]
    overdue = Task.objects.filter(project__in=projects, due_date__lt=timezone.localdate()).exclude(status=Task.Status.DONE).count()
    activity = Activity.objects.filter(project__in=Project.objects.visible_to(user)).select_related("actor", "project", "task")[:12]
    low_stock = Part.objects.filter(stock__lt=F("min_stock")).count()
    open_pos = PurchaseOrder.objects.filter(status__in=["ordered", "partial"]).count()
    active_builds = BuildOrder.objects.filter(status__in=["planned", "in_progress"]).select_related("revision__project")[:5]
    checklist = _setup_checklist(user) if user.can_manage_projects else None
    return render(request, "core/dashboard.html", {
        "projects": projects, "my_tasks": my_tasks, "to_review": to_review, "overdue": overdue,
        "activity": activity, "low_stock": low_stock, "open_pos": open_pos, "active_builds": active_builds,
        "checklist": checklist,
        "show_checklist": checklist is not None and not all(s["done"] for s in checklist),
    })


def _setup_checklist(user):
    """Getting-started steps for admins and leads, until they're all done."""
    import os

    from django.urls import reverse

    from apps.accounts.models import User
    from apps.inventory.models import BomLine
    from apps.projects.models import Revision

    first = Project.objects.order_by("created_at").first()
    first_rev = Revision.objects.order_by("created_at").first()
    steps = []
    if user.is_admin_role:
        steps.append({"what": "Add your team", "why": "Create accounts and send each person their sign-in link.",
                      "done": User.objects.count() > 1, "url": reverse("accounts:user_create"), "cta": "Add person"})
    steps += [
        {"what": "Create your first project", "why": "A project is a product or board you're developing, e.g. “Power board”.",
         "done": first is not None, "url": reverse("projects:create"), "cta": "New project"},
        {"what": "Add a revision", "why": "Revisions (Rev A, Rev B…) hold the BOM and group tasks for each board spin.",
         "done": first_rev is not None,
         "url": reverse("projects:revision_create", args=[first.key]) if first else reverse("projects:create"), "cta": "Add revision"},
        {"what": "Import a BOM from KiCad", "why": "Upload the CSV from KiCad or KiBot. Parts are matched or added to the library.",
         "done": BomLine.objects.exists(),
         "url": reverse("inventory:bom_import", args=[first_rev.pk]) if first_rev else reverse("inventory:bom_index"), "cta": "Import BOM"},
        {"what": "Connect GitHub", "why": "Link a repository so pull requests move tasks and post to chat automatically.",
         "done": bool(os.environ.get("WORKBENCH_GITHUB_WEBHOOK_SECRET")) and Project.objects.exclude(github_repo="").exists(),
         "url": reverse("integrations:overview"), "cta": "Set up"},
    ]
    return steps


@login_required
def my_work(request):
    user = request.user
    tasks = Task.objects.filter(Q(assignee=user) | Q(reviewer=user)).exclude(status=Task.Status.DONE).select_related("project", "assignee", "reviewer")
    return render(request, "core/my_work.html", {"tasks": tasks})


@login_required
def search(request):
    q = request.GET.get("q", "").strip()
    results = {}
    if len(q) >= 2:
        visible = Project.objects.visible_to(request.user)
        task_filter = Q(title__icontains=q) | Q(description__icontains=q)
        if "-" in q:
            key, _, num = q.upper().partition("-")
            if num.isdigit():
                task_filter |= Q(project__key=key, number=int(num))
        results["tasks"] = Task.objects.filter(project__in=visible).filter(task_filter).select_related("project")[:25]
        results["projects"] = visible.filter(Q(name__icontains=q) | Q(key__icontains=q))[:10]
        results["parts"] = Part.objects.filter(Q(ipn__icontains=q) | Q(description__icontains=q) | Q(mpn__icontains=q) | Q(value__iexact=q))[:25]
        from apps.chat.models import Channel, Message
        channels = Channel.objects.visible_to(request.user)
        results["messages"] = Message.objects.filter(channel__in=channels, body__icontains=q, is_deleted=False).select_related("channel", "author").order_by("-created_at")[:20]
    return render(request, "core/search.html", {"q": q, "results": results,
                                                "total": sum(len(v) for v in results.values())})


@login_required
def notifications(request):
    items = request.user.notifications.all()[:100]
    return render(request, "core/notifications.html", {"items": items})


@login_required
@require_POST
def notifications_read(request):
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return redirect("core:notifications")


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.is_read = True
    n.save(update_fields=["is_read"])
    return redirect(n.url or "core:notifications")


@admin_required
def audit_log(request):
    qs = AuditLog.objects.select_related("actor")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(object_repr__icontains=q) | Q(actor__username__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/audit_log.html", {"page": page, "q": q})


@login_required
def help_page(request, topic="start"):
    topics = ["start", "projects", "reviews", "chat", "parts", "production", "github", "security"]
    if topic not in topics:
        raise Http404
    return render(request, f"core/help/{topic}.html", {"topic": topic, "topics": topics})


@login_required
def protected_file(request, pk):
    """Serves task attachments only to people who can see the project."""
    att = get_object_or_404(TaskAttachment.objects.select_related("task__project"), pk=pk)
    if not att.task.project.can_view(request.user):
        raise Http404
    path = Path(att.file.path).resolve()
    if not str(path).startswith(str(Path(settings.MEDIA_ROOT).resolve())) or not path.exists():
        raise Http404
    ctype = mimetypes.guess_type(att.name)[0] or "application/octet-stream"
    inline = ctype in ("application/pdf", "image/png", "image/jpeg", "image/svg+xml", "text/plain")
    resp = FileResponse(open(path, "rb"), content_type=ctype, as_attachment=not inline, filename=att.name)
    resp["X-Content-Type-Options"] = "nosniff"
    if ctype == "image/svg+xml":
        resp["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return resp


def error_403(request, exception=None):
    return render(request, "core/error.html", {"code": 403, "title": "Not allowed",
                                               "message": str(exception) if exception else "You don't have permission to see this page."}, status=403)


def error_404(request, exception=None):
    return render(request, "core/error.html", {"code": 404, "title": "Page not found",
                                               "message": "This page doesn't exist, or you don't have access to it."}, status=404)
