import os
import posixpath
import tempfile
import zipfile

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.chat.formatting import render as render_text
from apps.core.storage import file_response
from apps.core.utils import audit, notify
from apps.firmware.models import FirmwareArtifact, FirmwareRelease, parse_version
from apps.projects.models import Project, Revision, log_activity

from . import checkin, diff, highlight, intake
from .models import Blob, Draft, Repository, Version

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
README_NAMES = ("readme.md", "readme.txt", "readme")


# --- Lookups and permissions --------------------------------------------------------

def _project(request, key, edit=False):
    project = get_object_or_404(Project, key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view these files but not change them.")
    return project


def _repo(request, key, slug, edit=False):
    project = _project(request, key, edit=edit)
    return project, get_object_or_404(Repository.objects.select_related("board", "controller", "firmware"), project=project, slug=slug)


def _version(repo, number):
    return get_object_or_404(Version.objects.select_related("author"), repository=repo, number=number)


# --- Lists ----------------------------------------------------------------------------

@login_required
def overview(request):
    projects = (Project.objects.visible_to(request.user).filter(repositories__isnull=False).distinct().order_by("key")
                .prefetch_related(Prefetch("repositories", Repository.objects.select_related("board"))))
    return render(request, "sources/overview.html", {"projects": projects})


@login_required
def project_sources(request, key):
    project = _project(request, key)
    repos = list(project.repositories.select_related("board", "controller"))
    groups = [(None, [r for r in repos if r.board_id is None])]
    groups += [(b, [r for r in repos if r.board_id == b.pk]) for b in project.boards.all()]
    return render(request, "sources/project.html", {
        "project": project, "groups": [g for g in groups if g[1]], "has_repos": bool(repos),
        "can_edit": project.can_edit(request.user), "tab": "sources",
    })


# --- Create, edit, delete -------------------------------------------------------------

class RepoForm(forms.ModelForm):
    class Meta:
        model = Repository
        fields = ["name", "kind", "board", "controller", "firmware", "description"]

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.projects.models import Controller
        self.fields["board"].queryset = project.boards.all()
        self.fields["board"].empty_label = "Whole project (shared)"
        self.fields["controller"].queryset = Controller.objects.filter(board__project=project).select_related("board")
        self.fields["controller"].label_from_instance = lambda c: f"{c.board.name} · {c}"
        self.fields["firmware"].queryset = project.firmwares.all()
        self.fields["kind"].widget = forms.RadioSelect(choices=Repository.Kind.choices)

    def clean(self):
        data = super().clean()
        c, b = data.get("controller"), data.get("board")
        if c and b and c.board_id != b.pk:
            self.add_error("controller", "That controller is on a different board.")
        return data


@login_required
def repo_edit(request, key, slug=None):
    project = _project(request, key, edit=True)
    repo = get_object_or_404(Repository, project=project, slug=slug) if slug else None
    initial = {}
    if repo is None and str(request.GET.get("board", "")).isdigit():
        initial["board"] = project.boards.filter(pk=request.GET["board"]).first()
    form = RepoForm(request.POST or None, instance=repo, project=project, initial=initial)
    if request.method == "POST" and form.is_valid():
        creating = repo is None
        repo = form.save(commit=False)
        repo.project = project
        if creating:
            repo.created_by = request.user
        repo.save()
        audit(request, "sources.repo_created" if creating else "sources.repo_updated", repo)
        if creating:
            log_activity(project, f"added the repository “{repo.name}”", actor=request.user, url=repo.get_absolute_url())
            messages.success(request, f"{repo.name} added. Next: check in its files.")
        return redirect(repo)
    return render(request, "sources/repo_form.html", {"form": form, "project": project, "repo": repo, "tab": "sources"})


@login_required
@require_POST
def repo_delete(request, key, slug):
    project, repo = _repo(request, key, slug, edit=True)
    if not request.user.can_manage_projects:
        raise PermissionDenied("Only a lead or an administrator can delete a repository.")
    audit(request, "sources.repo_deleted", repo, versions=repo.latest_number)
    log_activity(project, f"deleted the repository “{repo.name}”", actor=request.user)
    messages.success(request, f"{repo.name} deleted.")
    repo.delete()
    return redirect("sources:project", project.key)


# --- Browsing ---------------------------------------------------------------------------

def listing(tree, folder):
    """Folders and files directly inside `folder` ("" or "src/")."""
    dirs, files = {}, []
    for path, blob in tree.items():
        if not path.startswith(folder):
            continue
        rest = path[len(folder):]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            dirs[name] = dirs.get(name, 0) + 1
        else:
            files.append({"name": rest, "path": path, "blob": blob})
    return ([{"name": n, "path": f"{folder}{n}/", "count": c} for n, c in sorted(dirs.items(), key=lambda x: x[0].lower())],
            sorted(files, key=lambda f: f["name"].lower()))


def breadcrumbs(folder):
    out, acc = [], ""
    for part in [p for p in folder.split("/") if p]:
        acc += part + "/"
        out.append({"name": part, "path": acc})
    return out


@login_required
def repo_detail(request, key, slug):
    project, repo = _repo(request, key, slug)
    v = request.GET.get("v", "")
    version = _version(repo, int(v)) if v.isdigit() else repo.latest()
    folder = request.GET.get("path", "")
    folder = "" if not folder else folder.strip("/") + "/"
    tree = checkin.tree(version)
    if folder and not any(p.startswith(folder) for p in tree):
        raise Http404("That folder isn't in this version.")
    dirs, files = listing(tree, folder)
    locks = {lk.path: lk for lk in repo.locks.select_related("user")}
    for f in files:
        f["lock"] = locks.get(f["path"])
        f["lockable"] = checkin.needs_lock(f["path"])
    readme = None
    for f in files:
        if f["name"].lower() in README_NAMES:
            text = f["blob"].text(limit=200_000)
            if text is not None:
                readme = render_text(text) if f["name"].lower().endswith(".md") else text
            break
    return render(request, "sources/repo.html", {
        "project": project, "repo": repo, "version": version, "is_latest": version is not None and version.number == repo.latest_number,
        "folder": folder, "crumbs": breadcrumbs(folder), "dirs": dirs, "files": files, "readme": readme,
        "readme_is_html": bool(readme) and hasattr(readme, "__html__"),
        "history": repo.versions.select_related("author")[:50], "locks": list(locks.values()),
        "promote_revisions": (repo.board.revisions.exclude(status=Revision.Status.OBSOLETE)
                              if repo.kind == Repository.Kind.KICAD and repo.board_id else None),
        "next_version": repo.firmware.suggest_next_version() if repo.firmware_id else "",
        "can_edit": project.can_edit(request.user), "can_manage": request.user.can_manage_projects, "tab": "sources",
    })


@login_required
def file_view(request, key, slug, v, path):
    project, repo = _repo(request, key, slug)
    version = _version(repo, v)
    vf = version.files.select_related("blob").filter(path=path).first()
    if vf is None:
        raise Http404("That file isn't in this version.")
    name = posixpath.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if request.GET.get("raw"):
        if request.GET.get("inline") and ext in IMAGE_TYPES:
            return file_response(vf.blob.file, filename=name, inline=True, content_type=IMAGE_TYPES[ext])
        return file_response(vf.blob.file, filename=name, content_type="application/octet-stream")
    lines = language = None
    too_large = False
    if vf.blob.is_text:
        text = vf.blob.text(limit=highlight.HIGHLIGHT_BYTES)
        if text is None or text.count("\n") > highlight.HIGHLIGHT_LINES:
            too_large = True
        else:
            lines, language = highlight.render(path, text)
    lock = repo.locks.select_related("user").filter(path=path).first()
    return render(request, "sources/file.html", {
        "project": project, "repo": repo, "version": version, "path": path, "name": name, "blob": vf.blob,
        "lines": lines, "language": language, "is_image": ext in IMAGE_TYPES, "lock": lock, "too_large": too_large,
        "lockable": checkin.needs_lock(path), "folder": posixpath.dirname(path) + "/" if "/" in path else "",
        "crumbs": breadcrumbs(posixpath.dirname(path)), "is_latest": version.number == repo.latest_number,
        "can_edit": project.can_edit(request.user), "can_manage": request.user.can_manage_projects, "tab": "sources",
    })


def write_zip(files, fh):
    """Write VersionFiles into a zip in the open binary file `fh`, and rewind it."""
    with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for f in files:
            with f.blob.file.open("rb") as src, z.open(f.path, "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
    fh.seek(0)


@login_required
def version_zip(request, key, slug, v):
    project, repo = _repo(request, key, slug)
    version = _version(repo, v)
    files = list(version.files.select_related("blob"))
    if sum(f.blob.size for f in files) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        messages.error(request, f"This version is larger than {settings.MAX_UPLOAD_MB} MB, so it can't be downloaded as one zip. "
                                "Download the files you need one by one.")
        return redirect(version)
    fh = tempfile.TemporaryFile(dir=settings.DATA_DIR)
    write_zip(files, fh)
    resp = FileResponse(fh, as_attachment=True, filename=f"{project.key}-{repo.slug}-v{version.number}.zip",
                        content_type="application/zip")
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


MAX_DIFF_FILES = 100


@login_required
def compare(request, key, slug):
    project, repo = _repo(request, key, slug)
    if not repo.latest_number:
        raise Http404("Nothing has been checked in yet.")
    b_raw, a_raw = request.GET.get("b", ""), request.GET.get("a", "")
    b = _version(repo, int(b_raw)) if b_raw.isdigit() else repo.latest()
    a_num = int(a_raw) if a_raw.isdigit() else b.number - 1
    a = _version(repo, a_num) if a_num else None
    files = diff.changes(checkin.tree(a), checkin.tree(b))
    for i, f in enumerate(files):
        f["diff"] = diff.file_diff(f["path"], f["old"], f["new"]) if i < MAX_DIFF_FILES else None
    split = request.GET.get("view") == "split"
    return render(request, "sources/compare.html", {
        "project": project, "repo": repo, "a": a, "b": b, "files": files, "split": split,
        "versions": repo.versions.only("number", "message")[:200], "too_many": len(files) > MAX_DIFF_FILES,
        "added": sum(f["diff"]["added"] for f in files if f["diff"]), "removed": sum(f["diff"]["removed"] for f in files if f["diff"]),
        "tab": "sources",
    })


# --- Check-in --------------------------------------------------------------------------

@login_required
def checkin_start(request, key, slug):
    project, repo = _repo(request, key, slug, edit=True)
    base_raw = request.POST.get("base") or request.GET.get("base") or ""
    base = int(base_raw) if base_raw.isdigit() else repo.latest_number
    if request.method == "POST":
        uploads = request.FILES.getlist("files")
        paths = request.POST.getlist("paths")
        if not uploads:
            messages.error(request, "Choose the files, a folder or a zip to check in.")
            return redirect("sources:checkin", project.key, repo.slug)
        into = request.POST.get("into", "").strip()
        if into and intake.normalise_path(into) is None:
            messages.error(request, "That folder name can't be used. Use a path inside the repository, like src/drivers.")
            return redirect("sources:checkin", project.key, repo.slug)
        names = [paths[i] if i < len(paths) and paths[i] else u.name for i, u in enumerate(uploads)]
        result = intake.take(list(zip(names, uploads)), into=into,
                             keep_zips=bool(request.POST.get("keep_zips")))
        mode = "replace" if request.POST.get("mode") == "replace" else "update"
        draft = Draft.objects.create(repository=repo, user=request.user, base_number=min(base, repo.latest_number), mode=mode,
                                     files={p: b.pk for p, b in result.files.items()}, refused=result.refused, ignored=result.ignored)
        url = reverse("sources:checkin_preview", args=[project.key, repo.slug, draft.pk])
        if request.headers.get("x-requested-with") == "fetch":
            return JsonResponse({"redirect": url})
        return redirect(url)
    return render(request, "sources/checkin.html", {"project": project, "repo": repo, "base": min(base, repo.latest_number),
                                                    "max_mb": settings.MAX_UPLOAD_MB, "tab": "sources"})


@login_required
def checkin_preview(request, key, slug, draft):
    project, repo = _repo(request, key, slug, edit=True)
    draft = get_object_or_404(Draft, pk=draft, repository=repo, user=request.user)
    blobs_by_id = Blob.objects.in_bulk(set(draft.files.values()))
    staged = {p: blobs_by_id[i] for p, i in draft.files.items() if i in blobs_by_id}
    gone = [(p, "The uploaded copy of this file is gone (uploads are kept for a day). Please upload it again.")
            for p, i in draft.files.items() if i not in blobs_by_id]
    message = request.POST.get("message", "").strip()
    error = None
    if request.method == "POST":
        if not message:
            error = "Write a short message saying what changed."
        else:
            version, p = checkin.commit(repo, staged, draft.mode, draft.base_number, message, request.user,
                                        allow_partial=bool(request.POST.get("allow_partial")),
                                        release_locks=bool(request.POST.get("release_locks")), refuse=gone)
            if version is not None:
                draft.delete()
                audit(request, "sources.checkin", repo, version=version.number, added=version.added,
                      changed=version.changed, deleted=version.deleted)
                log_activity(project, f"checked in {repo.name} v{version.number}: {message}"[:500], actor=request.user,
                             url=version.get_absolute_url())
                messages.success(request, f"Checked in v{version.number}.")
                return redirect(repo)
            error = ("Nothing changed compared with the latest version." if not p.has_changes else
                     "Some files can't be checked in (see below). Tick “Check in the other files” to continue without them.")
    plan = checkin.plan(repo, staged, draft.mode, draft.base_number, request.user, refuse=gone)
    return render(request, "sources/preview.html", {
        "project": project, "repo": repo, "draft": draft, "plan": plan, "message": message, "error": error,
        "intake_refused": draft.refused, "tab": "sources",
    })


@login_required
@require_POST
def lock_action(request, key, slug):
    project, repo = _repo(request, key, slug, edit=True)
    path, action = request.POST.get("path", ""), request.POST.get("action")
    back = request.POST.get("next") or repo.get_absolute_url()
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = repo.get_absolute_url()
    name = posixpath.basename(path)
    if action == "lock":
        err = checkin.lock(repo, path, request.user)
        if err:
            messages.error(request, err)
        else:
            audit(request, "sources.lock", repo, path=path)
            messages.success(request, f"{name} is checked out to you. Check in your changes to release it.")
    elif action == "unlock":
        if checkin.unlock(repo, path, request.user):
            audit(request, "sources.unlock", repo, path=path)
            messages.success(request, f"{name} released.")
    elif action == "break":
        if not request.user.can_manage_projects:
            raise PermissionDenied("Only a lead or an administrator can release someone else's check-out.")
        holder = repo.locks.select_related("user").filter(path=path).first()
        if holder and checkin.unlock(repo, path, request.user, force=True):
            audit(request, "sources.lock_broken", repo, path=path, holder=holder.user.username)
            if holder.user_id != request.user.pk:
                notify(holder.user, f"{request.user} released your check-out of {name} in {repo.name}.", repo.get_absolute_url())
            messages.success(request, f"{name} released.")
    return redirect(back)


# --- Promote to production ------------------------------------------------------------

@login_required
@require_POST
def promote(request, key, slug, v):
    project, repo = _repo(request, key, slug, edit=True)
    version = _version(repo, v)
    if repo.kind == Repository.Kind.KICAD and repo.board_id:
        rev = get_object_or_404(Revision, pk=request.POST.get("revision") or 0, board_id=repo.board_id, project=project)
        if rev.status == Revision.Status.RELEASED and rev.source_version_id and rev.source_version_id != version.pk:
            messages.error(request, f"{rev.title} is released for production, so the design it was built from can't change.")
            return redirect(version)
        rev.source_version = version
        rev.save(update_fields=["source_version"])
        audit(request, "sources.promote", repo, version=version.number, revision=rev.title)
        log_activity(project, f"marked {rev.title} as built from {repo.name} v{version.number}", actor=request.user, url=rev.get_absolute_url())
        messages.success(request, f"{rev.title} is now built from {repo.name} v{version.number}.")
        return redirect(rev)
    if not repo.firmware_id:
        messages.error(request, "Choose which firmware this repository releases (Edit) before making a release from it.")
        return redirect(version)
    fw = repo.firmware
    number = (request.POST.get("version") or "").strip() or fw.suggest_next_version()
    if parse_version(number) is None:
        messages.error(request, f"“{number}” isn't a version number like 1.4.2.")
        return redirect(version)
    number = number.lstrip("vV")
    if fw.releases.filter(version=number).exists():
        messages.error(request, f"{fw.name} {number} already exists. Choose a new version number.")
        return redirect(version)
    files = list(version.files.select_related("blob"))
    if sum(f.blob.size for f in files) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        messages.error(request, f"This version is larger than {settings.MAX_UPLOAD_MB} MB, so it can't be attached as source.")
        return redirect(version)
    with tempfile.TemporaryFile(dir=settings.DATA_DIR) as fh:
        write_zip(files, fh)
        try:
            with transaction.atomic():
                rel = FirmwareRelease.objects.create(firmware=fw, version=number, status=FirmwareRelease.Status.DRAFT,
                                                     git_ref=f"sources v{version.number}", notes=version.message,
                                                     created_by=request.user)
        except IntegrityError:  # two clicks at once
            messages.error(request, f"{fw.name} {number} already exists. Choose a new version number.")
            return redirect(version)
        art = FirmwareArtifact.store(rel, File(fh, name=f"{repo.slug}-v{version.number}-source.zip"), request.user)
    art.kind = FirmwareArtifact.Kind.OTHER
    art.save(update_fields=["kind"])
    audit(request, "sources.promote", repo, version=version.number, release=f"{fw.name} {rel.version}")
    log_activity(project, f"made {fw.name} {rel.version} (draft) from {repo.name} v{version.number}", actor=request.user, url=rel.get_absolute_url())
    messages.success(request, f"Draft release {fw.name} {rel.version} created with the source attached. Upload the built files next.")
    return redirect(rel)
