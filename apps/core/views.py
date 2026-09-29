
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.inventory.models import Part
from apps.production.models import BuildOrder, PurchaseOrder
from apps.projects.models import Activity, Project, Task

from .models import AuditLog, Notification
from .utils import admin_required, audit


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
    from collections import defaultdict

    from apps.accounts.models import User
    zones = defaultdict(list)
    for person in User.objects.filter(is_active=True).only("first_name", "last_name", "username", "time_zone"):
        zones[person.time_zone or "UTC"].append(person.first_name or person.username)
    team_clock = sorted(({"tz": tz, "city": tz.split("/")[-1].replace("_", " "), "people": names} for tz, names in zones.items()),
                        key=lambda z: z["tz"])
    blocked = Task.objects.filter(project__in=projects).exclude(blocked_reason="").exclude(status=Task.Status.DONE).select_related("project")
    return render(request, "core/dashboard.html", {
        "projects": projects, "my_tasks": my_tasks, "to_review": to_review, "overdue": overdue,
        "activity": activity, "low_stock": low_stock, "open_pos": open_pos, "active_builds": active_builds,
        "checklist": checklist, "team_clock": team_clock, "blocked": blocked,
        "show_checklist": checklist is not None and not all(s["done"] for s in checklist),
    })


def _setup_checklist(user):
    """Getting-started steps for admins and leads, until they're all done."""
    from django.urls import reverse

    from apps.integrations.models import PullRequest

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
         "done": Project.objects.exclude(github_repo="").exists() and PullRequest.objects.exists(),
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
        from apps.files.models import Document
        results["files"] = Document.objects.alive().visible_to(request.user).filter(
            Q(name__icontains=q) | Q(description__icontains=q)).select_related("project")[:20]
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
    topics = ["start", "projects", "hardware", "reviews", "firmware", "chat", "files", "time", "parts", "production", "github", "security", "install"]
    if topic not in topics:
        raise Http404
    return render(request, f"core/help/{topic}.html", {"topic": topic, "topics": topics})


def error_403(request, exception=None):
    return render(request, "core/error.html", {"code": 403, "title": "Not allowed",
                                               "message": str(exception) if exception else "You don't have permission to see this page."}, status=403)


def error_404(request, exception=None):
    return render(request, "core/error.html", {"code": 404, "title": "Page not found",
                                               "message": "This page doesn't exist, or you don't have access to it."}, status=404)


# --- First-run setup ------------------------------------------------------------

def setup(request):
    from django.conf import settings as dj
    from django.contrib.auth import login

    from apps.accounts.models import User

    from .forms import SetupForm
    from .middleware import FirstRunSetupMiddleware
    from .models import SiteSettings

    if User.objects.exists():
        return redirect("core:dashboard")
    initial = {"site_url": f"{request.scheme}://{request.get_host()}"}
    form = SetupForm(request.POST or None, initial=initial, expected_code=dj.SETUP_TOKEN)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        site = SiteSettings.load()
        site.company_name = d["company_name"]
        site.site_url = d["site_url"].rstrip("/")
        site.time_zone = d["time_zone"]
        site.setup_completed_at = timezone.now()
        site.save()
        user = User.objects.create_user(username=d["username"], password=d["password1"], email=d["email"],
                                        first_name=d["first_name"], last_name=d["last_name"], time_zone=d["time_zone"],
                                        role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        FirstRunSetupMiddleware._done = True
        audit(request, "setup.completed", user, actor=user)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["mfa_verified"] = False
        return redirect("accounts:mfa_setup")
    return render(request, "core/setup.html", {"form": form})


def healthz(request):
    from django.http import JsonResponse

    from .system import health
    data = health()
    return JsonResponse(data, status=200 if data["ok"] else 503)


# --- System (administrators) ---------------------------------------------------------

@admin_required
def system_page(request, tab="overview"):
    from django.conf import settings as dj
    from django.urls import reverse

    from apps.files.models import DocumentVersion

    from . import system
    from .forms import EmailSettingsForm, GeneralSettingsForm, StorageSettingsForm
    from .models import SiteSettings

    site = SiteSettings.load()
    forms_by_tab = {"general": GeneralSettingsForm, "email": EmailSettingsForm, "backups": StorageSettingsForm}
    form = None
    if tab in forms_by_tab:
        form = forms_by_tab[tab](request.POST or None, instance=site)
        if request.method == "POST" and form.is_valid():
            form.save()
            audit(request, f"settings.{tab}", site)
            messages.success(request, "Settings saved.")
            return redirect("core:system_tab", tab=tab)
    ctx = {"tab": tab, "form": form, "site": site}
    if tab == "general":
        from .forms import LogoForm
        ctx["logo_form"] = LogoForm()
    if tab == "overview":
        from django.db.models import Sum as S
        ctx.update({
            "health": system.health(), "disk": system.disk_usage(), "db_size": system.database_size(),
            "files_size": DocumentVersion.objects.aggregate(s=S("size"))["s"] or 0,
            "backups": system.list_backups()[:1], "data_dir": dj.DATA_DIR, "storage": __import__("apps.core.storage", fromlist=["x"]).storage_info(), "https": dj.HTTPS, "domain": dj.DOMAIN,
            "version": system.VERSION, "db_vendor": "SQLite" if system.is_sqlite() else "PostgreSQL",
            "user_count": __import__("apps.accounts.models", fromlist=["User"]).User.objects.filter(is_active=True).count(),
        })
    elif tab == "backups":
        from .models import ExportJob
        ctx.update({"backups": system.list_backups(), "backup_dir": dj.BACKUP_DIR,
                    "exports": ExportJob.objects.select_related("requested_by")[:8]})
    elif tab == "github":
        import os
        ctx.update({"webhook_url": site.absolute_url(reverse("integrations:github_webhook"), request),
                    "secret": site.github_secret, "env_override": bool(os.environ.get("WORKBENCH_GITHUB_WEBHOOK_SECRET"))})
    return render(request, "core/system/system.html", ctx)


@admin_required
@require_POST
def system_action(request):
    import secrets as pysecrets


    from . import email, system
    from .models import SiteSettings

    action = request.POST.get("action")
    site = SiteSettings.load()
    if action == "backup_now":
        try:
            path = system.create_backup(f"manual by {request.user.username}")
            system.prune_backups(site.backup_keep)
            audit(request, "backup.created", None, file=path.name)
            messages.success(request, f"Backup created: {path.name}")
        except Exception as e:  # disk full, permissions…
            messages.error(request, f"Backup failed: {e}")
        return redirect("core:system_tab", tab="backups")
    if action == "request_export":
        from . import export as exporter
        from .models import ExportJob
        if ExportJob.objects.filter(status__in=["pending", "running"]).exists():
            messages.info(request, "An export is already being prepared.")
            return redirect("/system/backups/#exports")
        job = ExportJob.objects.create(requested_by=request.user, include_versions=bool(request.POST.get("include_versions")))
        audit(request, "export.requested", None, include_versions=job.include_versions)
        beat = system.health().get("scheduler_seen")
        if not beat or (timezone.now() - timezone.datetime.fromisoformat(beat)).total_seconds() > 180:
            exporter.run_job(job)  # no background worker running (e.g. development): do it now
            exporter.prune_exports()
            messages.success(request, "Export ready." if job.status == "ready" else f"Export failed: {job.message}")
        else:
            messages.success(request, "Preparing your export. It takes a minute or two for large installations — you'll get a notification when it's ready.")
        return redirect("/system/backups/#exports")
    if action == "download_export":
        from .export import export_dir
        name = request.POST.get("name", "")
        p = export_dir() / name
        if not (name.startswith("workbench-export-") and name.endswith(".zip") and "/" not in name and p.exists()):
            raise Http404
        audit(request, "export.downloaded", None, file=name)
        return FileResponse(open(p, "rb"), as_attachment=True, filename=name)
    if action in ("download_backup", "delete_backup"):
        p = system.backup_path(request.POST.get("name", ""))
        if p is None:
            raise Http404
        if action == "delete_backup":
            p.unlink()
            audit(request, "backup.deleted", None, file=p.name)
            messages.success(request, f"Deleted {p.name}.")
            return redirect("core:system_tab", tab="backups")
        audit(request, "backup.downloaded", None, file=p.name)
        return FileResponse(open(p, "rb"), as_attachment=True, filename=p.name)
    if action == "test_email":
        if not request.user.email:
            messages.error(request, "Add an email address to your profile first.")
        else:
            try:
                email.send(request.user.email, "Test email", "Email from Workbench is working.", site)
                messages.success(request, f"Test email sent to {request.user.email}.")
            except Exception as e:
                messages.error(request, f"Couldn't send: {e}")
        return redirect("core:system_tab", tab="email")
    if action in ("upload_logo", "remove_logo"):
        from django.core.files.base import ContentFile

        from .forms import LogoForm
        if action == "remove_logo":
            field = request.POST.get("which") if request.POST.get("which") in ("logo", "logo_dark") else "logo"
            getattr(site, field).delete(save=False)
            setattr(site, field, "")
        else:
            form = LogoForm(request.POST, request.FILES)
            if not form.is_valid():
                for errs in form.errors.values():
                    for e in errs:
                        messages.error(request, e)
                return redirect("core:system_tab", tab="general")
            changed = False
            for field in ("logo", "logo_dark"):
                if form.cleaned_data.get(field):
                    data, ext = form.cleaned_data[field]
                    getattr(site, field).delete(save=False)
                    getattr(site, field).save(f"{field}-{pysecrets.token_hex(4)}.{ext}", ContentFile(data), save=False)
                    changed = True
            if not changed:
                messages.error(request, "Choose a logo file first.")
                return redirect("core:system_tab", tab="general")
        site.logo_updated_at = timezone.now()
        site.save()
        audit(request, f"branding.{action}", site)
        messages.success(request, "Logo updated." if action == "upload_logo" else "Logo removed.")
        return redirect("core:system_tab", tab="general")
    if action == "rotate_github_secret":
        site.github_secret = pysecrets.token_hex(32)
        site.save(update_fields=["_github_secret"])
        audit(request, "github.secret_rotated", site)
        messages.success(request, "New webhook secret generated. Update it in GitHub's webhook settings now.")
        return redirect("core:system_tab", tab="github")
    return redirect("core:system")


def logo(request, variant):
    """Serves the company logo (public: it's shown on the sign-in page)."""
    from django.http import HttpResponse

    from .models import SiteSettings
    site = SiteSettings.load()
    f = site.logo_dark if variant == "dark" and site.logo_dark else site.logo
    if not f:
        raise Http404
    try:
        with f.storage.open(f.name, "rb") as fh:
            data = fh.read()
    except Exception:
        raise Http404
    ext = f.name.rsplit(".", 1)[-1].lower()
    ctype = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "application/octet-stream")
    resp = HttpResponse(data, content_type=ctype)
    # Even a cleaned SVG is served in a sandbox so nothing in it can run.
    resp["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "public, max-age=31536000, immutable" if request.GET.get("v") else "no-cache"
    return resp
