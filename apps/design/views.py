import csv
import io
import os
import shutil
import tempfile
import zipfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings
from apps.core.storage import file_response
from apps.core.utils import audit
from apps.files.models import BLOCKED_EXTENSIONS
from apps.projects.models import Project, Revision, log_activity

from .board import FINISHES, MASK_COLOURS
from .models import CATEGORY_HELP, Category, DesignFile
from .render import gerber_files, render_files

MASKS = [("green", "#2c7a48"), ("black", "#262626"), ("blue", "#1f4ea3"), ("red", "#b0302c"),
         ("purple", "#5d3389"), ("yellow", "#dcbc3a"), ("white", "#f3f3ef")]


def _revision(request, key, pk, edit=False):
    project = get_object_or_404(Project, key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    rev = get_object_or_404(Revision, pk=pk, project=project)
    if edit:
        if not project.can_edit(request.user):
            raise PermissionDenied("You can view this project but not change it.")
        if rev.status == Revision.Status.RELEASED:
            raise PermissionDenied(f"{rev.title} is released, so its design files are locked. Start a new revision for changes.")
    return project, rev


def _file(rev, pk):
    if not str(pk).isdigit():
        raise Http404
    return get_object_or_404(DesignFile.objects.select_related("uploaded_by"), pk=pk, revision=rev)


def board_summary(rev):
    """Board facts for the revision page (renders on first use, then cached)."""
    files = gerber_files(rev)
    if not files:
        return None
    result = render_files(files)
    return result


@login_required
def design_files(request, key, pk):
    project, rev = _revision(request, key, pk)
    current = list(DesignFile.objects.filter(revision=rev, is_current=True).select_related("uploaded_by"))
    counts = {}
    for f in DesignFile.objects.filter(revision=rev).values_list("name", flat=True):
        counts[f] = counts.get(f, 0) + 1
    groups = []
    for value, label in Category.choices:
        items = [f for f in current if f.category == value]
        if items:
            groups.append({"key": value, "label": label, "files": items})
    for f in current:
        f.version_count = counts.get(f.name, 1)
    board = board_summary(rev)
    missing = [(c, Category(c).label, CATEGORY_HELP[c]) for c in ("gerber", "schematic", "pnp", "bom")
               if not any(f.category == c for f in current)]
    return render(request, "design/files.html", {
        "project": project, "rev": rev, "groups": groups, "board": board, "missing": missing,
        "can_edit": project.can_edit(request.user) and rev.status != Revision.Status.RELEASED,
        "locked": rev.status == Revision.Status.RELEASED, "categories": Category.choices,
        "max_mb": SiteSettings.load().max_upload_mb, "revisions": project.revisions.all(),
    })


@login_required
@require_POST
def upload(request, key, pk):
    project, rev = _revision(request, key, pk, edit=True)
    limit = SiteSettings.load().max_upload_mb
    category = request.POST.get("category") or None
    if category and category not in Category.values:
        category = None
    added, same, errors = [], 0, []
    for f in request.FILES.getlist("files"):
        ext = os.path.splitext(f.name)[1].lower()
        if ext in BLOCKED_EXTENSIONS:
            errors.append(f"{f.name}: files of type {ext} aren't allowed.")
            continue
        if f.size > limit * 1024 * 1024:
            errors.append(f"{f.name}: larger than the {limit} MB limit.")
            continue
        obj, created = DesignFile.store(rev, f, request.user, category=category, note=request.POST.get("note", ""))
        if created:
            added.append(obj)
            audit(request, "design.uploaded", rev, file=obj.name, version=obj.version, sha256=obj.sha256)
        else:
            same += 1
    if added:
        names = ", ".join(f"{o.name}" + (f" (v{o.version})" if o.version > 1 else "") for o in added[:4])
        more = f" and {len(added) - 4} more" if len(added) > 4 else ""
        log_activity(project, f"uploaded design files to {rev.title}: {names}{more}", actor=request.user,
                     url=f"{rev.get_absolute_url()}design/")
        messages.success(request, f"Uploaded {len(added)} file{'s' if len(added) != 1 else ''}.")
    if same:
        messages.info(request, f"{same} file{'s were' if same != 1 else ' was'} identical to the current version and skipped.")
    if request.headers.get("x-requested-with") == "fetch":
        for e in errors:
            messages.error(request, e)
        return JsonResponse({"ok": not errors, "uploaded": [o.name for o in added], "errors": errors})
    for e in errors:
        messages.error(request, e)
    return redirect("design:files", key=project.key, pk=rev.pk)


@login_required
def file_detail(request, key, pk, file_id):
    project, rev = _revision(request, key, pk)
    f = _file(rev, file_id)
    versions = DesignFile.objects.filter(revision=rev, name=f.name).select_related("uploaded_by").order_by("-version")
    preview = None
    if f.viewer == "csv":
        preview = _csv_preview(f)
    elif f.viewer == "text":
        preview = _text_preview(f)
    zip_list = None
    if f.ext == "zip":
        try:
            with f.file.open("rb") as fh, zipfile.ZipFile(fh) as z:  # reads only the directory, not the whole file
                zip_list = []
                for i in z.infolist():
                    if not i.is_dir():
                        zip_list.append((i.filename, i.file_size))
                        if len(zip_list) >= 300:
                            break
        except (zipfile.BadZipFile, OSError, ValueError):
            zip_list = []
    return render(request, "design/file_detail.html", {
        "project": project, "rev": rev, "f": f, "versions": versions, "preview": preview, "zip_list": zip_list,
        "categories": Category.choices,
        "can_edit": project.can_edit(request.user) and rev.status != Revision.Status.RELEASED,
    })


def _csv_preview(f, limit=300):
    try:
        with f.file.open("rb") as fh:
            text = fh.read(2 * 1024 * 1024).decode("utf-8-sig", "replace")
    except OSError:
        return None
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if f.ext == "pos" and rows and rows[0] and rows[0][0].startswith("#"):
        # KiCad .pos (ASCII): whitespace-separated with a commented header
        rows = [line.split() for line in text.splitlines() if line.strip() and not line.startswith("## ")]
        if rows and rows[0][0] == "#":
            rows[0] = rows[0][1:]
        rows = [r for r in rows if r and r[0] != "#"] if rows else rows
    return {"header": rows[0] if rows else [], "rows": rows[1:limit + 1], "total": max(len(rows) - 1, 0)}


def _text_preview(f, limit=200 * 1024):
    try:
        with f.file.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return None
    return {"text": data[:limit].decode("utf-8", "replace"), "truncated": len(data) > limit}


@login_required
def download(request, key, pk, file_id):
    project, rev = _revision(request, key, pk)
    f = _file(rev, file_id)
    inline = request.GET.get("inline") == "1" and f.viewer in ("pdf", "image")
    name = f.name if f.is_current else f"v{f.version}-{f.name}"
    resp = file_response(f.file, filename=name, inline=inline)
    if inline:
        resp["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                                           "frame-ancestors 'self'; sandbox allow-same-origin allow-downloads")
        resp["X-Frame-Options"] = "SAMEORIGIN"
    else:
        audit(request, "design.downloaded", rev, file=f.name, version=f.version)
    return resp


@login_required
def download_all(request, key, pk):
    """Every current design file of the revision in one zip, in folders by type."""
    project, rev = _revision(request, key, pk)
    files = list(DesignFile.objects.filter(revision=rev, is_current=True))
    if not files:
        raise Http404
    # Spooled to a temporary file, so a revision with large files doesn't need its whole size in memory.
    buf = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    base = f"{project.key}-{rev.title}".replace(" ", "-")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            with f.file.open("rb") as fh, z.open(f"{base}/{Category(f.category).label}/{f.name}", "w",
                                                   force_zip64=(f.size or 0) > 2 ** 30) as out:
                shutil.copyfileobj(fh, out, 1024 * 1024)
    audit(request, "design.downloaded_all", rev, files=len(files))
    buf.seek(0)
    return FileResponse(buf, as_attachment=True, filename=f"{base}-design-files.zip")


@login_required
@require_POST
def delete(request, key, pk, file_id):
    project, rev = _revision(request, key, pk, edit=True)
    f = _file(rev, file_id)
    all_versions = DesignFile.objects.filter(revision=rev, name=f.name)
    n = all_versions.count()
    from apps.cad.jobs import delete_previews
    for v in all_versions:
        delete_previews(v)
        v.file.delete(save=False)
        v.delete()
    audit(request, "design.deleted", rev, file=f.name, versions=n)
    messages.success(request, f"Deleted {f.name}" + (f" and its {n - 1} older version{'s' if n > 2 else ''}." if n > 1 else "."))
    return redirect("design:files", key=project.key, pk=rev.pk)


@login_required
@require_POST
def set_category(request, key, pk, file_id):
    project, rev = _revision(request, key, pk, edit=True)
    f = _file(rev, file_id)
    cat = request.POST.get("category")
    if cat in Category.values:
        DesignFile.objects.filter(revision=rev, name=f.name).update(category=cat)
    return redirect("design:files", key=project.key, pk=rev.pk)


# --- PCB viewer -------------------------------------------------------------------------

def _board_for(request, rev):
    file_id = request.GET.get("file")
    f = _file(rev, file_id) if file_id else None
    files = gerber_files(rev, f)
    return f, files, render_files(files)


@login_required
def pcb_viewer(request, key, pk):
    project, rev = _revision(request, key, pk)
    f, files, board = _board_for(request, rev)
    if not files:
        messages.info(request, "Upload the Gerber files (the zip you send to the fab) to see the board.")
        return redirect("design:files", key=project.key, pk=rev.pk)
    zips = DesignFile.objects.filter(revision=rev, category=Category.GERBER, name__iendswith=".zip").order_by("-uploaded_at")
    return render(request, "design/pcb_viewer.html", {
        "project": project, "rev": rev, "board": board, "selected": f, "files": files, "masks": MASKS,
        "zips": zips, "query": f"?file={f.pk}" if f else "",
        "masks_json": {"masks": MASK_COLOURS, "finishes": FINISHES},
    })


def _svg_response(svg):
    resp = HttpResponse(svg, content_type="image/svg+xml; charset=utf-8")
    resp["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
def pcb_svg(request, key, pk):
    project, rev = _revision(request, key, pk)
    _f, files, board = _board_for(request, rev)
    if not board or not board.get("ok"):
        raise Http404
    return _svg_response(board["thumb_top"] if request.GET.get("view") == "thumb" else board["svg"])
