import os

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings
from apps.core.utils import admin_required, audit
from apps.projects.models import Project, Task, log_activity

from .models import BLOCKED_EXTENSIONS, SHARED, Document, DocumentVersion, Folder, space_label

TEXT_PREVIEW_BYTES = 200_000


# --- Helpers -----------------------------------------------------------------

def _space(request, space, edit=False):
    """Returns the Project for a space key (None for Shared) after checking access."""
    if space == SHARED:
        if edit and request.user.is_read_only:
            raise PermissionDenied("Your account is read-only.")
        return None
    project = get_object_or_404(Project, key=space.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view these files but not change them.")
    return project


def validate_upload(f):
    ext = os.path.splitext(f.name)[1].lower()
    limit = SiteSettings.load().max_upload_mb
    if ext in BLOCKED_EXTENSIONS:
        return f"{f.name}: files of type {ext} can't be uploaded for security reasons. Zip it first if you need to share it."
    if f.size > limit * 1024 * 1024:
        return f"{f.name}: larger than the {limit} MB limit."
    return None


def store_upload(f, *, project, folder, user, task=None, request=None):
    """Save an uploaded file. If a live document with the same name is already in
    the folder, the upload becomes its next version instead of a duplicate."""
    from .models import safe_name
    name = safe_name(f.name)
    doc = Document.objects.alive().filter(project=project, folder=folder, name=name).first()
    new = doc is None
    if new:
        doc = Document.objects.create(project=project, folder=folder, name=name, task=task, created_by=user, updated_by=user)
    elif task and not doc.task_id:
        doc.task = task
    version = doc.add_version(f, user)
    audit(request, "file.uploaded", doc, version=version.number, size=version.size)
    if project is not None:
        what = f"uploaded {doc.name}" if new else f"uploaded version {version.number} of {doc.name}"
        log_activity(project, what + (f" to {task.key}" if task else ""), actor=user, task=task, url=doc.get_absolute_url())
    return doc, version, new


class FolderForm(forms.Form):
    name = forms.CharField(max_length=120, label="Folder name")


class DocumentEditForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["name", "description", "folder", "task"]
        labels = {"task": "Linked task"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        doc = self.instance
        self.fields["folder"].queryset = Folder.objects.filter(project=doc.project)
        self.fields["folder"].empty_label = "(top level)"
        self.fields["folder"].label_from_instance = lambda f: " / ".join([a.name for a in f.ancestors()] + [f.name])
        self.fields["task"].queryset = Task.objects.filter(project=doc.project) if doc.project_id else Task.objects.none()
        self.fields["task"].empty_label = "None"
        if not doc.project_id:
            self.fields.pop("task")

    def clean_name(self):
        from .models import safe_name
        name = safe_name(self.cleaned_data["name"])
        if os.path.splitext(name)[1].lower() in BLOCKED_EXTENSIONS:
            raise forms.ValidationError("That file extension isn't allowed.")
        return name


# --- Library views -------------------------------------------------------------

@login_required
def index(request):
    visible = Project.objects.visible_to(request.user).exclude(status=Project.Status.ARCHIVED)
    stats = {row["project"]: row for row in Document.objects.alive().values("project").annotate(n=Count("id"), size=Sum("size"))}
    spaces = [{"key": SHARED, "label": "Shared", "desc": "Company-wide documents: datasheets, templates, procedures.",
               "n": stats.get(None, {}).get("n", 0), "size": stats.get(None, {}).get("size") or 0}]
    for p in visible:
        s = stats.get(p.pk, {})
        spaces.append({"key": p.key, "label": p.name, "desc": p.description, "n": s.get("n", 0), "size": s.get("size") or 0})
    recent = Document.objects.alive().visible_to(request.user).select_related("project", "updated_by").order_by("-updated_at")[:12]
    return render(request, "files/index.html", {"spaces": spaces, "recent": recent,
                                                "trash_count": Document.objects.trashed().visible_to(request.user).count()})


@login_required
def folder_view(request, space, folder_id=None):
    project = _space(request, space)
    folder = get_object_or_404(Folder, pk=folder_id, project=project) if folder_id else None
    q = request.GET.get("q", "").strip()
    docs = Document.objects.alive().filter(project=project).select_related("updated_by", "task", "task__project")
    if q:
        docs = docs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        folders = []
    else:
        docs = docs.filter(folder=folder)
        folders = Folder.objects.filter(project=project, parent=folder).annotate(
            n=Count("documents", filter=Q(documents__deleted_at__isnull=True), distinct=True),
            subs=Count("children", distinct=True))
    can_edit = (not request.user.is_read_only) and (project is None or project.can_edit(request.user))
    return render(request, "files/folder.html", {
        "space": space if space == SHARED else project.key, "space_label": space_label(space) if project is None else project.name,
        "project": project, "folder": folder, "crumbs": folder.ancestors() if folder else [],
        "folders": folders, "docs": docs, "q": q, "can_edit": can_edit, "folder_form": FolderForm(),
        "max_mb": SiteSettings.load().max_upload_mb,
    })


@login_required
@require_POST
def folder_create(request, space, folder_id=None):
    project = _space(request, space, edit=True)
    parent = get_object_or_404(Folder, pk=folder_id, project=project) if folder_id else None
    form = FolderForm(request.POST)
    if form.is_valid():
        name = form.cleaned_data["name"].strip().replace("/", "-")
        f, made = Folder.objects.get_or_create(project=project, parent=parent, name=name, defaults={"created_by": request.user})
        if made:
            messages.success(request, f"Folder “{name}” created.")
        return redirect(f)
    return redirect(parent or "files:space", *([] if parent else [space]))


@login_required
@require_POST
def folder_action(request, space, folder_id):
    project = _space(request, space, edit=True)
    folder = get_object_or_404(Folder, pk=folder_id, project=project)
    parent = folder.parent
    action = request.POST.get("action")
    if action == "rename":
        name = (request.POST.get("name") or "").strip().replace("/", "-")[:120]
        if name and not Folder.objects.filter(project=project, parent=parent, name=name).exclude(pk=folder.pk).exists():
            folder.name = name
            folder.save(update_fields=["name"])
            messages.success(request, "Folder renamed.")
        else:
            messages.error(request, "Choose a name that isn't already used here.")
        return redirect(folder)
    if action == "delete":
        ids = folder.descendant_ids()
        docs = Document.objects.alive().filter(folder_id__in=ids)
        n = docs.count()
        for d in docs:
            d.trash(request.user)
        Document.objects.filter(folder_id__in=ids).update(folder=None)
        audit(request, "folder.deleted", folder, documents=n)
        folder.delete()
        messages.success(request, f"Folder deleted. {n} file{'s' if n != 1 else ''} moved to the trash — restore them from Trash within {SiteSettings.load().trash_days} days.")
    return redirect(parent or "files:space", *([] if parent else [space]))


@login_required
@require_POST
def upload(request, space, folder_id=None):
    """Accepts one or many files (form or drag-and-drop)."""
    project = _space(request, space, edit=True)
    folder = get_object_or_404(Folder, pk=folder_id, project=project) if folder_id else None
    files = request.FILES.getlist("files")
    done, errors = [], []
    for f in files:
        err = validate_upload(f)
        if err:
            errors.append(err)
            continue
        doc, v, new = store_upload(f, project=project, folder=folder, user=request.user, request=request)
        done.append(f"{doc.name}{'' if new else f' (version {v.number})'}")
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": not errors, "uploaded": done, "errors": errors})
    if done:
        messages.success(request, "Uploaded: " + ", ".join(done))
    for e in errors:
        messages.error(request, e)
    if not files:
        messages.error(request, "Choose at least one file.")
    return redirect(folder or "files:space", *([] if folder else [space]))


def _document(request, pk, edit=False, include_deleted=False):
    doc = get_object_or_404(Document.objects.select_related("project", "folder", "task__project"), pk=pk)
    if not doc.can_view(request.user) or (doc.deleted_at and not include_deleted):
        raise Http404
    if edit and not doc.can_edit(request.user):
        raise PermissionDenied("You can view this file but not change it.")
    return doc


@login_required
def document(request, pk):
    doc = _document(request, pk)
    latest = doc.latest
    text = None
    if doc.preview_kind == "text" and latest:
        try:
            with latest.file.open("rb") as fh:
                raw = fh.read(TEXT_PREVIEW_BYTES)
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            text = None
    return render(request, "files/document.html", {
        "doc": doc, "latest": latest, "versions": doc.versions.select_related("uploaded_by"),
        "can_edit": doc.can_edit(request.user), "edit_form": DocumentEditForm(instance=doc),
        "text": text, "text_truncated": latest and latest.size > TEXT_PREVIEW_BYTES,
        "crumbs": (doc.folder.ancestors() + [doc.folder]) if doc.folder else [],
    })


@login_required
@require_POST
def document_edit(request, pk):
    doc = _document(request, pk, edit=True)
    form = DocumentEditForm(request.POST, instance=doc)
    if form.is_valid():
        if Document.objects.alive().filter(project=doc.project, folder=form.cleaned_data["folder"], name=form.cleaned_data["name"]).exclude(pk=doc.pk).exists():
            messages.error(request, "A file with that name is already in that folder.")
        else:
            form.save()
            messages.success(request, "Saved.")
    else:
        messages.error(request, "Couldn't save: " + " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(doc)


@login_required
@require_POST
def document_new_version(request, pk):
    doc = _document(request, pk, edit=True)
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose a file.")
        return redirect(doc)
    err = validate_upload(f)
    if err:
        messages.error(request, err)
        return redirect(doc)
    v = doc.add_version(f, request.user, note=request.POST.get("note", ""))
    audit(request, "file.new_version", doc, version=v.number)
    if doc.project:
        log_activity(doc.project, f"uploaded version {v.number} of {doc.name}", actor=request.user, task=doc.task, url=doc.get_absolute_url())
    messages.success(request, f"Version {v.number} uploaded.")
    return redirect(doc)


@login_required
@require_POST
def document_delete(request, pk):
    doc = _document(request, pk, edit=True)
    doc.trash(request.user)
    audit(request, "file.trashed", doc)
    messages.success(request, f"{doc.name} moved to the trash.")
    if doc.folder:
        return redirect(doc.folder)
    return redirect("files:space", doc.space)


def _serve(request, version, inline):
    from apps.core.storage import file_response
    doc = version.document
    ctype = None
    safe_inline = inline and doc.preview_kind in ("pdf", "image")
    if inline and doc.preview_kind == "text":
        ctype, safe_inline = "text/plain; charset=utf-8", True
    name = doc.name if version.number == doc.version_count else f"v{version.number}-{doc.name}"
    resp = file_response(version.file, filename=name, inline=safe_inline, content_type=ctype)
    if safe_inline:
        # Allow our own pages to show the preview in a frame, and sandbox it.
        resp["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox allow-same-origin allow-downloads"
        resp["X-Frame-Options"] = "SAMEORIGIN"
    if not inline:
        audit(request, "file.downloaded", doc, version=version.number)
    return resp


@login_required
def download(request, pk):
    version = get_object_or_404(DocumentVersion.objects.select_related("document__project"), pk=pk)
    if not version.document.can_view(request.user):
        raise Http404
    return _serve(request, version, inline=request.GET.get("inline") == "1")


# --- Trash & storage -------------------------------------------------------------

@login_required
def trash(request):
    docs = Document.objects.trashed().visible_to(request.user).select_related("project", "deleted_by").order_by("-deleted_at")
    days = SiteSettings.load().trash_days
    for d in docs:
        d.purge_on = d.deleted_at + timezone.timedelta(days=days)
    return render(request, "files/trash.html", {"docs": docs, "days": days})


@login_required
@require_POST
def trash_action(request, pk):
    doc = _document(request, pk, edit=True, include_deleted=True)
    if request.POST.get("action") == "restore":
        doc.restore()
        audit(request, "file.restored", doc)
        messages.success(request, f"{doc.name} restored.")
    elif request.POST.get("action") == "purge":
        if not request.user.can_manage_projects:
            raise PermissionDenied("Only leads and administrators can delete files permanently.")
        audit(request, "file.purged", doc, size=doc.total_size)
        doc.purge()
        messages.success(request, "File deleted permanently.")
    return redirect("files:trash")


@admin_required
def storage(request):
    from apps.core.storage import local_root, storage_info
    from apps.core.system import disk_usage
    alive = Document.objects.alive()
    by_space = []
    for row in DocumentVersion.objects.values("document__project__key").annotate(size=Sum("size"), n=Count("id")).order_by("-size"):
        by_space.append({"space": row["document__project__key"] or "Shared", "size": row["size"] or 0, "versions": row["n"]})
    by_user = DocumentVersion.objects.values("uploaded_by__first_name", "uploaded_by__last_name", "uploaded_by__username") \
        .annotate(size=Sum("size"), n=Count("id")).order_by("-size")[:10]
    return render(request, "files/storage.html", {
        "total": DocumentVersion.objects.aggregate(s=Sum("size"))["s"] or 0,
        "latest_total": alive.aggregate(s=Sum("size"))["s"] or 0,
        "doc_count": alive.count(), "version_count": DocumentVersion.objects.count(),
        "trash_size": DocumentVersion.objects.filter(document__deleted_at__isnull=False).aggregate(s=Sum("size"))["s"] or 0,
        "old_versions_size": DocumentVersion.objects.exclude(number=F("document__version_count")).aggregate(s=Sum("size"))["s"] or 0,
        "by_space": by_space, "by_user": by_user,
        "largest": alive.select_related("project").order_by("-size")[:15],
        "disk": disk_usage(local_root()), "storage": storage_info(),
    })


@admin_required
@require_POST
def prune_versions(request):
    """Deletes all but the newest N versions of every file."""
    keep = max(1, int(request.POST.get("keep", 3) or 3))
    removed = freed = 0
    for doc in Document.objects.filter(version_count__gt=keep):
        for v in doc.versions.order_by("-number")[keep:]:
            freed += v.size
            v.file.delete(save=False)
            v.delete()
            removed += 1
    audit(request, "storage.pruned_versions", None, keep=keep, removed=removed, freed=freed)
    messages.success(request, f"Removed {removed} old versions and freed {freed / 1024 / 1024:.1f} MB.")
    return redirect("files:storage")


@admin_required
@require_POST
def empty_trash(request):
    n = 0
    for doc in Document.objects.trashed():
        doc.purge()
        n += 1
    audit(request, "storage.emptied_trash", None, documents=n)
    messages.success(request, f"Trash emptied ({n} files deleted permanently).")
    return redirect("files:storage")
