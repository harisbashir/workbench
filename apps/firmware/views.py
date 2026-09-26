import os

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.chat.models import post_system_message
from apps.core.models import SiteSettings
from apps.core.utils import audit, notify
from apps.files.models import BLOCKED_EXTENSIONS
from apps.projects.models import Project, Revision, Task, log_activity

from .models import Firmware, FirmwareArtifact, FirmwareRelease, parse_version

ARTIFACT_ORDER = {"image": 0, "package": 1, "bootloader": 2, "debug": 3, "other": 4}

NOTES_TEMPLATE = """### Changes
-

### Fixes
-

### Known issues
- None
"""


# --- Permissions -------------------------------------------------------------------

def _project(request, key, edit=False):
    project = get_object_or_404(Project, key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view this firmware but not change it.")
    return project


def can_approve(user, project):
    """Releasing, deprecating and recalling firmware is a lead's decision."""
    return not user.is_read_only and (user.can_manage_projects or project.lead_id == user.pk)


# --- Forms ---------------------------------------------------------------------------

class FirmwareForm(forms.ModelForm):
    class Meta:
        model = Firmware
        fields = ["name", "target", "description", "github_repo", "tag_prefix"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class ReleaseForm(forms.ModelForm):
    class Meta:
        model = FirmwareRelease
        fields = ["version", "git_ref", "revisions", "notes", "source_url"]
        widgets = {"revisions": forms.CheckboxSelectMultiple, "notes": forms.Textarea(attrs={"rows": 10})}
        help_texts = {"revisions": "Which board revisions this firmware runs on. The newest released version for "
                                   "each revision is shown as its recommended firmware.",
                      "notes": "What changed. Mention task IDs (e.g. PWR-12) to link them."}

    def __init__(self, firmware, *args, **kwargs):
        self.firmware = firmware
        super().__init__(*args, **kwargs)
        self.fields["revisions"].queryset = firmware.project.revisions.exclude(status=Revision.Status.OBSOLETE) | \
            Revision.objects.filter(pk__in=self.instance.revisions.values("pk") if self.instance.pk else [])
        self.fields["revisions"].queryset = self.fields["revisions"].queryset.distinct()

    def clean_version(self):
        v = self.cleaned_data["version"].strip()
        if v[:1] in ("v", "V"):
            v = v[1:]
        if parse_version(v) is None:
            raise forms.ValidationError("Use semantic versioning: MAJOR.MINOR.PATCH, e.g. 1.4.2 (or 2.0.0-rc.1 for a pre-release).")
        clash = FirmwareRelease.objects.filter(firmware=self.firmware, version=v).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Version {v} already exists. Each release needs a new version number.")
        return v


def _check_upload(f):
    ext = os.path.splitext(f.name)[1].lower()
    limit = SiteSettings.load().max_upload_mb
    if ext in BLOCKED_EXTENSIONS:
        return f"{f.name}: files of type {ext} aren't allowed."
    if f.size > limit * 1024 * 1024:
        return f"{f.name}: larger than the {limit} MB limit."
    return None


def _add_artifacts(request, release, files):
    added = []
    for f in files:
        err = _check_upload(f)
        if err:
            messages.error(request, err)
            continue
        from apps.files.models import safe_name
        existing = release.artifacts.filter(name=safe_name(f.name)).first()
        if existing:  # replacing a file in a draft/testing release
            existing.file.delete(save=False)
            existing.delete()
        art = FirmwareArtifact.store(release, f, request.user)
        audit(request, "firmware.artifact_uploaded", release, file=art.name, sha256=art.sha256)
        added.append(art)
    return added


# --- Views ---------------------------------------------------------------------------

@login_required
def project_firmware(request, key):
    project = _project(request, key)
    firmwares = list(project.firmwares.annotate(n=Count("releases")))
    revisions = list(project.revisions.exclude(status=Revision.Status.OBSOLETE))
    matrix = []
    for rev in revisions:
        matrix.append({"rev": rev, "cells": [fw.recommended_for(rev) for fw in firmwares]})
    for fw in firmwares:
        fw.latest = fw.latest_released
        fw.in_testing = fw.releases.filter(status=FirmwareRelease.Status.TESTING).order_by("-sort_key").first()
    return render(request, "firmware/project.html", {
        "project": project, "firmwares": firmwares, "matrix": matrix,
        "can_edit": project.can_edit(request.user), "tab": "firmware",
    })


@login_required
def firmware_edit(request, key, slug=None):
    project = _project(request, key, edit=True)
    fw = get_object_or_404(Firmware, project=project, slug=slug) if slug else None
    form = FirmwareForm(request.POST or None, instance=fw)
    if request.method == "POST" and form.is_valid():
        creating = fw is None
        fw = form.save(commit=False)
        fw.project = project
        if creating:
            fw.created_by = request.user
        fw.save()
        if creating:
            log_activity(project, f"added firmware “{fw.name}”", actor=request.user, url=fw.get_absolute_url())
            messages.success(request, f"{fw.name} added. Next: create its first release.")
        audit(request, "firmware.saved", fw)
        return redirect(fw)
    return render(request, "firmware/firmware_form.html", {"form": form, "project": project, "fw": fw})


@login_required
def firmware_detail(request, key, slug):
    project = _project(request, key)
    fw = get_object_or_404(Firmware, project=project, slug=slug)
    status = request.GET.get("status", "")
    releases = fw.releases_sorted().prefetch_related("revisions").annotate(n_art=Count("artifacts"))
    if status in dict(FirmwareRelease.Status.choices):
        releases = releases.filter(status=status)
    return render(request, "firmware/firmware_detail.html", {
        "project": project, "fw": fw, "releases": releases, "status": status,
        "statuses": FirmwareRelease.Status.choices, "can_edit": project.can_edit(request.user),
        "revisions": project.revisions.exclude(status=Revision.Status.OBSOLETE),
    })


@login_required
def release_create(request, key, slug):
    project = _project(request, key, edit=True)
    fw = get_object_or_404(Firmware, project=project, slug=slug)
    previous = fw.releases.order_by("-sort_key").first()
    initial = {"version": fw.suggest_next_version(), "notes": NOTES_TEMPLATE,
               "revisions": list(previous.revisions.values_list("pk", flat=True)) if previous else
               list(project.revisions.exclude(status=Revision.Status.OBSOLETE).values_list("pk", flat=True)[:1])}
    initial["git_ref"] = f"{fw.tag_prefix}{initial['version']}"
    form = ReleaseForm(fw, request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        rel = form.save(commit=False)
        rel.firmware = fw
        rel.created_by = request.user
        rel.status = FirmwareRelease.Status.TESTING if request.POST.get("to_testing") else FirmwareRelease.Status.DRAFT
        rel.save()
        form.save_m2m()
        _add_artifacts(request, rel, request.FILES.getlist("artifacts"))
        log_activity(project, f"created {fw.name} {rel.version} ({rel.get_status_display()})", actor=request.user, url=rel.get_absolute_url())
        audit(request, "firmware.release_created", rel)
        if rel.status == FirmwareRelease.Status.TESTING:
            post_system_message(project, f"{request.user.display_name} published {fw.name} {rel.version} for testing.", rel.get_absolute_url())
        messages.success(request, f"{fw.name} {rel.version} created.")
        return redirect(rel)
    return render(request, "firmware/release_form.html", {"form": form, "project": project, "fw": fw, "previous": previous})


def _release(request, key, slug, version, edit=False):
    project = _project(request, key, edit=edit)
    fw = get_object_or_404(Firmware, project=project, slug=slug)
    rel = get_object_or_404(FirmwareRelease.objects.select_related("created_by", "released_by"), firmware=fw, version=version)
    return project, fw, rel


@login_required
def release_detail(request, key, slug, version):
    project, fw, rel = _release(request, key, slug, version)
    keys = rel.mentioned_task_keys()
    tasks = []
    for k in keys:
        pkey, _, num = k.partition("-")
        t = Task.objects.filter(project__key=pkey, number=int(num), project__in=Project.objects.visible_to(request.user)).first()
        if t:
            tasks.append(t)
    older = fw.releases.filter(sort_key__lt=rel.sort_key).order_by("-sort_key")
    compare_to = None
    if request.GET.get("since"):
        compare_to = fw.releases.filter(version=request.GET["since"]).first()
    changelog = []
    if compare_to:
        changelog = fw.releases.filter(sort_key__gt=compare_to.sort_key, sort_key__lte=rel.sort_key).order_by("-sort_key")
    from apps.production.models import BuildOrder
    return render(request, "firmware/release_detail.html", {
        "project": project, "fw": fw, "rel": rel,
        "artifacts": sorted(rel.artifacts.select_related("uploaded_by"), key=lambda a: (ARTIFACT_ORDER.get(a.kind, 9), a.name)),
        "revisions": rel.revisions.all(), "tasks": tasks, "older": older[:30], "compare_to": compare_to, "changelog": changelog,
        "builds": BuildOrder.objects.filter(firmware_releases=rel).select_related("revision"),
        "can_edit": project.can_edit(request.user) and not rel.is_locked,
        "can_approve": can_approve(request.user, project),
        "activity": project.activities.filter(url=rel.get_absolute_url()).select_related("actor")[:20],
        "newer_released": fw.releases.filter(status=FirmwareRelease.Status.RELEASED, sort_key__gt=rel.sort_key).order_by("-sort_key").first(),
    })


@login_required
def release_edit(request, key, slug, version):
    project, fw, rel = _release(request, key, slug, version, edit=True)
    if rel.is_locked:
        messages.error(request, "Released firmware can't be changed. Create a new version instead.")
        return redirect(rel)
    form = ReleaseForm(fw, request.POST or None, instance=rel)
    if request.method == "POST" and form.is_valid():
        rel = form.save()
        _add_artifacts(request, rel, request.FILES.getlist("artifacts"))
        audit(request, "firmware.release_edited", rel)
        messages.success(request, "Saved.")
        return redirect(rel)
    return render(request, "firmware/release_form.html", {"form": form, "project": project, "fw": fw, "rel": rel})


@login_required
@require_POST
def release_upload(request, key, slug, version):
    project, fw, rel = _release(request, key, slug, version, edit=True)
    if rel.is_locked:
        raise PermissionDenied("Released firmware can't be changed. Create a new version instead.")
    added = _add_artifacts(request, rel, request.FILES.getlist("artifacts"))
    if added:
        messages.success(request, "Uploaded " + ", ".join(a.name for a in added) + ".")
    return redirect(rel)


@login_required
@require_POST
def artifact_delete(request, key, slug, version, pk):
    project, fw, rel = _release(request, key, slug, version, edit=True)
    if rel.is_locked:
        raise PermissionDenied("Released firmware can't be changed.")
    art = get_object_or_404(FirmwareArtifact, pk=pk, release=rel)
    audit(request, "firmware.artifact_deleted", rel, file=art.name)
    art.file.delete(save=False)
    art.delete()
    messages.success(request, f"Removed {art.name}.")
    return redirect(rel)


@login_required
@require_POST
def release_status(request, key, slug, version):
    project, fw, rel = _release(request, key, slug, version)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()
    S = FirmwareRelease.Status
    editors_can = {"testing": (S.DRAFT,), "draft": (S.TESTING,)}
    leads_can = {"released": (S.DRAFT, S.TESTING), "deprecated": (S.RELEASED,), "recalled": (S.RELEASED, S.DEPRECATED, S.TESTING)}
    if action in editors_can:
        if not project.can_edit(request.user):
            raise PermissionDenied
        allowed_from = editors_can[action]
    elif action in leads_can:
        if not can_approve(request.user, project):
            raise PermissionDenied("Only the project lead or an administrator can do that.")
        allowed_from = leads_can[action]
    else:
        raise Http404
    if rel.status not in allowed_from:
        messages.error(request, f"A {rel.get_status_display().lower()} release can't be moved to {action}.")
        return redirect(rel)
    if action == "released":
        problems = []
        if not rel.artifacts.exists():
            problems.append("upload at least one file")
        if not rel.revisions.exists():
            problems.append("choose the compatible board revisions")
        if problems:
            messages.error(request, "Before releasing, " + " and ".join(problems) + ".")
            return redirect(rel)
    if action == "recalled" and not note:
        messages.error(request, "Say why it's being recalled, so the team knows what to do.")
        return redirect(rel)
    rel.set_status(action, request.user, note)
    label = rel.get_status_display()
    log_activity(project, f"marked {fw.name} {rel.version} as {label}" + (f": {note}" if note else ""), actor=request.user, url=rel.get_absolute_url())
    audit(request, f"firmware.{action}", rel, note=note)
    if action == "released":
        post_system_message(project, f"✓ {fw.name} {rel.version} released by {request.user.display_name}.", rel.get_absolute_url())
        # Older released versions for the same revisions are superseded.
        superseded = fw.releases.filter(status=S.RELEASED, sort_key__lt=rel.sort_key, revisions__in=rel.revisions.all()).distinct()
        n = 0
        for old in superseded:
            if set(old.revisions.values_list("pk", flat=True)) <= set(rel.revisions.values_list("pk", flat=True)):
                old.set_status(S.DEPRECATED, request.user, f"Superseded by {rel.version}")
                n += 1
        if n:
            messages.info(request, f"{n} older release{'s' if n != 1 else ''} marked as deprecated (superseded by {rel.version}).")
    if action == "recalled":
        post_system_message(project, f"⚠ {fw.name} {rel.version} has been RECALLED by {request.user.display_name}: {note}. Don't flash it.", rel.get_absolute_url())
        people = set(project.members.all()) | ({project.lead} if project.lead else set())
        for person in people - {request.user}:
            notify(person, f"Firmware recalled: {fw.name} {rel.version} — {note}", rel.get_absolute_url())
    messages.success(request, f"{fw.name} {rel.version} is now {label.lower()}.")
    return redirect(rel)


@login_required
def artifact_download(request, pk):
    from apps.core.storage import file_response
    art = get_object_or_404(FirmwareArtifact.objects.select_related("release__firmware__project"), pk=pk)
    if not art.release.firmware.project.can_view(request.user):
        raise Http404
    resp = file_response(art.file, filename=art.name, content_type="application/octet-stream")
    resp["X-Checksum-SHA256"] = art.sha256
    audit(request, "firmware.downloaded", art.release, file=art.name)
    return resp


@login_required
def overview(request):
    """All firmware across the projects you can see."""
    visible = Project.objects.visible_to(request.user)
    firmwares = Firmware.objects.filter(project__in=visible).select_related("project").annotate(
        n=Count("releases"), testing=Count("releases", filter=Q(releases__status="testing")))
    for fw in firmwares:
        fw.latest = fw.latest_released
    recent = FirmwareRelease.objects.filter(firmware__project__in=visible).select_related("firmware__project").order_by("-created_at")[:15]
    return render(request, "firmware/overview.html", {"firmwares": firmwares, "recent": recent})
