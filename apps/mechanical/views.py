import io
import os
import zipfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings
from apps.core.storage import file_response
from apps.core.utils import audit
from apps.files.models import BLOCKED_EXTENSIONS
from apps.projects.models import Project, log_activity

from .forms import PartForm
from .models import FileKind, MechanicalFile, MechanicalPart


def _project(request, key, edit=False):
    project = get_object_or_404(Project, key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view this project but not change it.")
    return project


def _part(request, key, pk, edit=False):
    project = _project(request, key, edit=edit)
    return project, get_object_or_404(MechanicalPart, pk=pk, project=project)


def can_release(user, project):
    return not user.is_read_only and (user.can_manage_projects or project.lead_id == user.pk)


def _check_unlocked(request, project, part):
    if part.is_locked and not can_release(request.user, project):
        raise PermissionDenied(f"{part.name} rev {part.revision} is released, so its files are locked. "
                               "The project lead can change the status or start the next revision.")


@login_required
def part_edit(request, key, pk=None):
    project = _project(request, key, edit=True)
    part = get_object_or_404(MechanicalPart, pk=pk, project=project) if pk else None
    old_status = part.status if part else None
    form = PartForm(request.POST or None, instance=part, project=project)
    if request.method == "POST":
        if request.POST.get("action") == "delete" and part:
            if not can_release(request.user, project):
                raise PermissionDenied("Only the project lead or an administrator can delete a part.")
            name = part.name
            from apps.cad.jobs import delete_previews
            for f in part.files.all():
                delete_previews(f)
                f.file.delete(save=False)
                f.delete()
            part.delete()
            log_activity(project, f"deleted mechanical part {name}", actor=request.user)
            messages.success(request, f"{name} deleted.")
            return redirect("projects:hardware", project.key)
        if form.is_valid():
            new_status = form.cleaned_data["status"]
            if new_status != old_status and MechanicalPart.Status.RELEASED in (new_status, old_status) and not can_release(request.user, project):
                form.add_error("status", "Only the project lead or an administrator can release a part or un-release it.")
            else:
                p = form.save(commit=False)
                p.project = project
                if not part:
                    p.created_by = request.user
                p.save()
                form.save_m2m()
                audit(request, "mechanical.saved", p, status=p.status)
                if not part:
                    log_activity(project, f"added mechanical part {p.name}", actor=request.user, url=p.get_absolute_url())
                    messages.success(request, f"{p.name} added. Next: upload its CAD files.")
                else:
                    if old_status != p.status:
                        log_activity(project, f"marked {p.name} rev {p.revision} as {p.get_status_display()}", actor=request.user,
                                     url=p.get_absolute_url())
                    messages.success(request, "Part saved.")
                return redirect(p)
    return render(request, "mechanical/part_form.html", {"form": form, "project": project, "part": part,
                                                          "can_delete": part and can_release(request.user, project)})


@login_required
def part_detail(request, key, pk):
    project, part = _part(request, key, pk)
    current = list(part.files.filter(is_current=True).select_related("uploaded_by"))
    counts = dict(part.files.values_list("name").annotate(n=Count("id")))
    for f in current:
        f.version_count = counts.get(f.name, 1)
    chosen = None
    if request.GET.get("file"):
        chosen = next((f for f in current if str(f.pk) == request.GET["file"]), None)
    preview = chosen or part.preview_file
    groups = []
    for kind, label in FileKind.choices:
        fs = [f for f in current if f.kind == kind]
        if fs:
            groups.append((label, fs))
    return render(request, "mechanical/part_detail.html", {
        "project": project, "part": part, "groups": groups, "preview": preview, "files": current,
        "can_edit": project.can_edit(request.user) and (not part.is_locked or can_release(request.user, project)),
        "locked": part.is_locked, "boards": part.boards.all(),
        "total_size": part.files.aggregate(s=Sum("size"))["s"] or 0,
    })


@login_required
@require_POST
def upload(request, key, pk):
    project, part = _part(request, key, pk, edit=True)
    _check_unlocked(request, project, part)
    limit = SiteSettings.load().max_upload_mb
    added, same, errors = [], 0, []
    for f in request.FILES.getlist("files"):
        ext = os.path.splitext(f.name)[1].lower()
        if ext in BLOCKED_EXTENSIONS:
            errors.append(f"{f.name}: files of type {ext} aren't allowed.")
            continue
        if f.size > limit * 1024 * 1024:
            errors.append(f"{f.name}: larger than the {limit} MB limit.")
            continue
        obj, created = MechanicalFile.store(part, f, request.user, note=request.POST.get("note", ""))
        if created:
            added.append(obj)
        else:
            same += 1
    if added:
        names = ", ".join(o.name + (f" (v{o.version})" if o.version > 1 else "") for o in added[:4])
        more = f" and {len(added) - 4} more" if len(added) > 4 else ""
        log_activity(project, f"uploaded CAD files to {part.name}: {names}{more}", actor=request.user, url=part.get_absolute_url())
        audit(request, "mechanical.uploaded", part, files=len(added))
        messages.success(request, f"Uploaded {len(added)} file{'s' if len(added) != 1 else ''}.")
    if same:
        messages.info(request, f"{same} file{'s were' if same != 1 else ' was'} identical to the current version and skipped.")
    for e in errors:
        messages.error(request, e)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": not errors, "uploaded": [o.name for o in added], "errors": errors})
    return redirect(part)


def _file(part, file_id):
    return get_object_or_404(MechanicalFile, pk=file_id, part=part)


@login_required
def file_detail(request, key, pk, file_id):
    project, part = _part(request, key, pk)
    f = _file(part, file_id)
    versions = part.files.filter(name=f.name).select_related("uploaded_by").order_by("-version")
    return render(request, "mechanical/file_detail.html", {
        "project": project, "part": part, "f": f, "versions": versions, "kinds": FileKind.choices,
        "can_edit": project.can_edit(request.user) and (not part.is_locked or can_release(request.user, project)),
    })


@login_required
def download(request, key, pk, file_id):
    project, part = _part(request, key, pk)
    f = _file(part, file_id)
    inline = request.GET.get("inline") == "1" and f.ext in ("pdf", "png", "jpg", "jpeg")
    resp = file_response(f.file, filename=f.name if f.is_current else f"v{f.version}-{f.name}", inline=inline)
    if inline:
        resp["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                                           "frame-ancestors 'self'; sandbox allow-same-origin allow-downloads")
        resp["X-Frame-Options"] = "SAMEORIGIN"
    else:
        audit(request, "mechanical.downloaded", part, file=f.name, version=f.version)
    return resp


@login_required
def download_all(request, key, pk):
    project, part = _part(request, key, pk)
    buf = io.BytesIO()
    base = f"{project.key}-{part.name}-rev{part.revision}".replace(" ", "-")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in part.files.filter(is_current=True):
            with f.file.open("rb") as fh:
                z.writestr(f"{base}/{FileKind(f.kind).label}/{f.name}", fh.read())
    audit(request, "mechanical.downloaded_all", part)
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{base}.zip"'
    return resp


@login_required
@require_POST
def file_action(request, key, pk, file_id):
    project, part = _part(request, key, pk, edit=True)
    _check_unlocked(request, project, part)
    f = _file(part, file_id)
    action = request.POST.get("action")
    if action == "kind" and request.POST.get("kind") in FileKind.values:
        part.files.filter(name=f.name).update(kind=request.POST["kind"])
        messages.success(request, "File type changed.")
        return redirect(f)
    if action == "delete":
        all_versions = list(part.files.filter(name=f.name))
        from apps.cad.jobs import delete_previews
        for v in all_versions:
            delete_previews(v)
            v.file.delete(save=False)
            v.delete()
        log_activity(project, f"deleted {f.name} from {part.name}", actor=request.user, url=part.get_absolute_url())
        audit(request, "mechanical.file_deleted", part, file=f.name)
        messages.success(request, f"Deleted {f.name}" + (" and its older versions." if len(all_versions) > 1 else "."))
        return redirect(part)
    return redirect(f)


def parts_with_stats(project):
    return project.mechanical_parts.annotate(
        file_count=Count("files", filter=Q(files__is_current=True), distinct=True)).prefetch_related("boards")
