import os
import posixpath
import tempfile
import zipfile

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.chat.formatting import render as render_text
from apps.core.storage import file_response
from apps.core.utils import audit
from apps.projects.models import Project, log_activity

from . import checkin, diff, highlight
from .models import Repository, Version

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
    text = vf.blob.text(limit=highlight.HIGHLIGHT_BYTES * 4)
    if text is not None:
        lines, language = highlight.render(path, text)
    lock = repo.locks.select_related("user").filter(path=path).first()
    return render(request, "sources/file.html", {
        "project": project, "repo": repo, "version": version, "path": path, "name": name, "blob": vf.blob,
        "lines": lines, "language": language, "is_image": ext in IMAGE_TYPES, "lock": lock,
        "lockable": checkin.needs_lock(path), "folder": posixpath.dirname(path) + "/" if "/" in path else "",
        "crumbs": breadcrumbs(posixpath.dirname(path)), "is_latest": version.number == repo.latest_number,
        "can_edit": project.can_edit(request.user), "can_manage": request.user.can_manage_projects, "tab": "sources",
    })


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
    with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for f in files:
            with f.blob.file.open("rb") as src, z.open(f.path, "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
    fh.seek(0)
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
