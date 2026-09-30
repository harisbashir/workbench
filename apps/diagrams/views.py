import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.chat.models import post_system_message
from apps.core.utils import audit, notify
from apps.projects.models import Board, Project, log_activity

from . import geometry, render as R, starters
from .models import Diagram, DiagramComment, DiagramVersion


def _project(request, key, edit=False):
    project = get_object_or_404(Project, key=key.upper())
    if not project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not project.can_edit(request.user):
        raise PermissionDenied("You can view this project but not change it.")
    return project


def _diagram(request, key, pk, edit=False):
    project = _project(request, key, edit=edit)
    return project, get_object_or_404(Diagram.objects.select_related("board", "approved_by"), pk=pk, project=project)


def can_approve(user, project):
    return not user.is_read_only and (user.can_manage_projects or project.lead_id == user.pk)


def project_approvers(project):
    """People on the project who can approve: its lead and members with a lead or administrator role."""
    people = set(project.members.filter(is_active=True))
    if project.lead and project.lead.is_active:
        people.add(project.lead)
    return {u for u in people if can_approve(u, project)}


def self_approval_blocked(user, project, version):
    """Four-eyes rule: you can't approve a version you saved yourself, unless nobody else on the project can approve."""
    if version is None or version.created_by_id != user.pk:
        return False
    return bool(project_approvers(project) - {user})


def _version(diagram, number=None):
    qs = diagram.versions.select_related("created_by")
    v = qs.filter(number=number).first() if number else qs.first()
    if v is None:
        raise Http404("That version doesn't exist.")
    return v


def title_block(diagram, version):
    p = diagram.project
    status = diagram.get_status_display()
    if diagram.approved_version == version.number and diagram.approved_by:
        status = f"Approved by {diagram.approved_by.display_name}"
    elif diagram.approved_version and diagram.approved_version != version.number:
        status = f"{'Draft' if diagram.status == 'draft' else status} (v{diagram.approved_version} approved)"
    board = diagram.board
    rev = board.latest_revision if board else None
    return {
        "Title": diagram.name,
        "Product": f"{p.key} · {p.name}",
        "Board": f"{board.name}{' · ' + rev.name if rev else ''}" if board else "Whole product (system)",
        "Level": diagram.get_level_display(),
        "Version": f"v{version.number}",
        "Status": status,
        "Author": version.created_by.display_name if version.created_by else "",
        "Date": timezone.localtime(version.created_at).strftime("%Y-%m-%d"),
    }


def _filename(diagram, version, ext):
    base = slugify(f"{diagram.project.key}-{diagram.board.name if diagram.board else 'system'}-{diagram.name}") or "diagram"
    return f"{base}-v{version.number}.{ext}"


# --- create -----------------------------------------------------------------------------------------

class DiagramForm(forms.Form):
    name = forms.CharField(max_length=120)
    level = forms.ChoiceField(choices=Diagram.Level.choices)
    board = forms.ModelChoiceField(queryset=Board.objects.none(), required=False, empty_label="— Whole product (system diagram) —")
    start = forms.ChoiceField(label="Start from", choices=[("starter", "A starter layout to edit"), ("blank", "An empty page")],
                              widget=forms.RadioSelect, initial="starter")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["board"].queryset = Board.objects.filter(project=project)
        if project is not None:
            copyable = Diagram.objects.filter(project=project).exclude(current_version=0)
            if copyable.exists():
                f = self.fields["start"]
                choices = list(f.choices) + [(f"copy:{d.pk}", f"A copy of “{d}”") for d in copyable]
                f.widget = forms.Select()
                f.choices = choices  # also sets the widget's options

    def clean(self):
        data = super().clean()
        if data.get("level") == Diagram.Level.SYSTEM:
            data["board"] = None
        elif not data.get("board"):
            self.add_error("board", "Choose the board this diagram describes (or make it a system diagram).")
        return data


@login_required
def create(request, key):
    project = _project(request, key, edit=True)
    initial = {"level": request.GET.get("level") or "high"}
    board = project.boards.filter(pk=request.GET.get("board")).first() if request.GET.get("board") else None
    if board:
        initial["board"] = board
        initial["name"] = f"{board.name} — {'detailed' if initial['level'] == 'detailed' else 'high-level'} block diagram"
    elif initial["level"] == "system":
        initial["name"] = f"{project.name} — system diagram"
    form = DiagramForm(request.POST or None, project=project, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        start = c["start"]
        if start.startswith("copy:"):
            src = get_object_or_404(Diagram, pk=start[5:], project=project)
            data = _version(src).data
        elif start == "blank":
            data = starters.blank()
        elif c["level"] == Diagram.Level.SYSTEM:
            data = starters.system(list(project.boards.all()), project.name)
        else:
            data = starters.board_high_level(c["board"].name)
        try:
            data = geometry.clean(data)
        except ValueError as exc:
            form.add_error("start", f"That drawing can't be copied: {exc}")
            return render(request, "diagrams/create.html", {"form": form, "project": project, "board": board})
        with transaction.atomic():
            d = Diagram.objects.create(project=project, board=c["board"], name=c["name"], level=c["level"],
                                       description=c["description"], created_by=request.user, updated_by=request.user,
                                       current_version=1)
            DiagramVersion.objects.create(diagram=d, number=1, data=data, note="Created", created_by=request.user)
        log_activity(project, f"started the block diagram “{d.name}”", actor=request.user, url=d.get_absolute_url())
        audit(request, "diagram.created", d)
        return redirect("diagrams:edit", key=project.key, pk=d.pk)
    return render(request, "diagrams/create.html", {"form": form, "project": project, "board": board})


# --- view & review ------------------------------------------------------------------------------------

@login_required
def detail(request, key, pk):
    project, d = _diagram(request, key, pk)
    try:
        number = int(request.GET.get("v") or d.current_version)
    except ValueError:
        raise Http404
    v = _version(d, number)
    comments = list(d.comments.select_related("author"))
    labels = {n["id"]: (n.get("label") or n.get("type")) for n in v.data.get("nodes", [])}
    return render(request, "diagrams/detail.html", {
        "project": project, "d": d, "v": v, "svg": R.to_svg(v.data, interactive=True),
        "versions": d.versions.select_related("created_by"), "comments": comments,
        "open_comments": sum(1 for c in comments if not c.resolved),
        "node_choices": sorted(((nid, lbl) for nid, lbl in labels.items() if lbl), key=lambda x: x[1].lower()),
        "can_edit": project.can_edit(request.user), "can_approve": can_approve(request.user, project),
        "own_version": self_approval_blocked(request.user, project, _version(d)) if d.current_version else False,
        "is_current": v.number == d.current_version,
    })


@login_required
@require_POST
def status(request, key, pk):
    project, d = _diagram(request, key, pk)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()[:1000]
    S = Diagram.Status
    people = set(project.members.all()) | ({project.lead} if project.lead else set())
    if action == "submit":
        if not project.can_edit(request.user):
            raise PermissionDenied
        d.status = S.REVIEW
        d.save(update_fields=["status", "updated_at"])
        for u in ({project.lead} if project.lead else people):
            if u != request.user:
                notify(u, f"{request.user.display_name} asked for a review of the block diagram “{d.name}” (v{d.current_version})",
                       d.get_absolute_url())
        post_system_message(project, f"{request.user.display_name} submitted the block diagram “{d.name}” v{d.current_version} for review.")
        msg = "Submitted for review."
    elif action in ("approve", "changes"):
        if not can_approve(request.user, project):
            raise PermissionDenied("Only the project lead or an administrator can approve diagrams.")
        try:
            reviewed = int(request.POST.get("version") or 0)
        except ValueError:
            reviewed = 0
        if d.status != S.REVIEW:
            messages.error(request, "This diagram isn't waiting for review.")
            return redirect(d)
        if reviewed != d.current_version:
            messages.error(request, f"The drawing changed while you were reviewing it: v{d.current_version} is now the newest. "
                                    "Look at it before you decide.")
            return redirect(d)
        self_approved = False
        if action == "approve":
            current = _version(d)
            if self_approval_blocked(request.user, project, current):
                messages.error(request, f"You saved v{d.current_version} yourself, so someone else on the project has to approve it.")
                return redirect(d)
            self_approved = current.created_by_id == request.user.pk
            d.status, d.approved_version, d.approved_by, d.approved_at = S.APPROVED, d.current_version, request.user, timezone.now()
            msg = f"v{d.current_version} approved."
            post_system_message(project, f"{request.user.display_name} approved the block diagram “{d.name}” v{d.current_version}.")
        else:
            if not note:
                messages.error(request, "Say what needs to change.")
                return redirect(d)
            d.status = S.DRAFT
            msg = "Sent back for changes."
        d.save()
        if note:
            DiagramComment.objects.create(diagram=d, version=d.current_version, author=request.user,
                                          text=("Approved: " if action == "approve" else "Changes requested: ") + note)
        for u in {d.created_by, d.updated_by} - {None, request.user}:
            notify(u, f"{request.user.display_name} {'approved' if action == 'approve' else 'asked for changes to'} “{d.name}”",
                   d.get_absolute_url())
    elif action == "reopen":
        if not can_approve(request.user, project):
            raise PermissionDenied
        d.status = S.DRAFT
        d.save(update_fields=["status", "updated_at"])
        msg = "Back to draft."
    else:
        raise Http404
    log_activity(project, f"{msg.lower().rstrip('.')} — block diagram “{d.name}”", actor=request.user, url=d.get_absolute_url())
    if action == "approve" and self_approved:
        audit(request, "diagram.approve", d, version=d.current_version,
              self_approved="yes — the author is the only lead or administrator on the project")
    else:
        audit(request, f"diagram.{action}", d, version=d.current_version)
    messages.success(request, msg)
    return redirect(d)


@login_required
@require_POST
def comment(request, key, pk):
    project, d = _diagram(request, key, pk)
    if request.user.is_read_only and not project.is_member(request.user):
        raise PermissionDenied
    if request.POST.get("resolve"):
        c = get_object_or_404(DiagramComment, pk=request.POST["resolve"], diagram=d)
        if not project.can_edit(request.user):
            raise PermissionDenied
        c.resolved = not c.resolved
        c.save(update_fields=["resolved"])
        return redirect(f"{d.get_absolute_url()}#comments")
    text = (request.POST.get("text") or "").strip()
    if text:
        node = request.POST.get("node") or ""
        v = _version(d)
        label = next((n.get("label") or n["type"] for n in v.data.get("nodes", []) if n["id"] == node), "")
        DiagramComment.objects.create(diagram=d, version=d.current_version, author=request.user, text=text[:4000],
                                      node_id=node if label else "", node_label=label[:120])
        for u in {d.created_by, d.updated_by, project.lead} - {None, request.user}:
            notify(u, f"{request.user.display_name} commented on “{d.name}”: {text[:80]}", d.get_absolute_url() + "#comments")
    return redirect(f"{d.get_absolute_url()}#comments")


@login_required
@require_POST
def settings_(request, key, pk):
    project, d = _diagram(request, key, pk, edit=True)
    if request.POST.get("action") == "delete":
        if not can_approve(request.user, project):
            raise PermissionDenied("Only the project lead or an administrator can delete diagrams.")
        name, board = d.name, d.board
        d.delete()
        log_activity(project, f"deleted the block diagram “{name}”", actor=request.user)
        messages.success(request, f"“{name}” deleted.")
        return redirect(board.get_absolute_url() if board else f"/projects/{project.key}/hardware/")
    name = (request.POST.get("name") or "").strip()[:120]
    if name:
        d.name = name
    d.description = (request.POST.get("description") or "").strip()
    d.save(update_fields=["name", "description", "updated_at"])
    messages.success(request, "Saved.")
    return redirect(d)


# --- editor -----------------------------------------------------------------------------------------------

@login_required
def edit(request, key, pk):
    project, d = _diagram(request, key, pk, edit=True)
    v = _version(d)
    meta = {
        "id": d.pk, "name": d.name, "level": d.level, "version": v.number,
        "saveUrl": f"/projects/{project.key}/diagrams/{d.pk}/save/",
        "pdfUrl": f"/projects/{project.key}/diagrams/{d.pk}/preview.pdf",
        "viewUrl": d.get_absolute_url(),
        "boards": [{"id": b.pk, "name": b.name, "url": b.get_absolute_url()} for b in project.boards.all()],
        "approved": d.approved_version, "status": d.status,
    }
    return render(request, "diagrams/editor.html", {
        "project": project, "d": d, "v": v, "style": geometry.STYLE, "data": v.data, "meta": meta,
        "shapes": geometry.SHAPES.items(), "edge_kinds": geometry.EDGE_KINDS.items(), "colors": geometry.COLORS.items(),
    })


class BadRequest(Exception):
    pass


def _json_body(request):
    """The request's JSON object, or BadRequest (answered with 400)."""
    if len(request.body or b"") > geometry.MAX_JSON_BYTES:
        raise BadRequest(f"The drawing is too large to save (more than {geometry.MAX_JSON_BYTES // 1024} KB).")
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("The request isn't valid JSON.")
    if not isinstance(body, dict):
        raise BadRequest("The request must be a JSON object.")
    return body


def _bad(message):
    return JsonResponse({"ok": False, "error": message}, status=400)


@login_required
@require_POST
def save(request, key, pk):
    project, d = _diagram(request, key, pk, edit=True)
    try:
        body = _json_body(request)
        data = geometry.clean(body.get("data"))
    except (BadRequest, ValueError) as exc:
        return _bad(str(exc))
    base = body.get("base")
    if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
        return _bad("The base version must be a number.")
    with transaction.atomic():
        d = Diagram.objects.select_for_update().get(pk=d.pk)
        if base is not None and base != d.current_version and body.get("force") is not True:
            last = d.versions.select_related("created_by").first()
            who = last.created_by.display_name if last and last.created_by else "someone"
            return JsonResponse({"ok": False, "conflict": True, "version": d.current_version,
                                 "error": f"{who} saved v{d.current_version} while you were editing."}, status=409)
        latest = d.versions.first()
        if latest and latest.data == data:
            return JsonResponse({"ok": True, "version": d.current_version, "unchanged": True})
        d.current_version += 1
        DiagramVersion.objects.create(diagram=d, number=d.current_version, data=data,
                                      note=geometry.clean_text(body.get("note"), 300), created_by=request.user)
        reopened = d.status == Diagram.Status.APPROVED
        if reopened:
            d.status = Diagram.Status.DRAFT
        d.updated_by = request.user
        d.save()
    if d.current_version % 5 == 1 or body.get("note") or reopened:
        log_activity(project, f"saved v{d.current_version} of the block diagram “{d.name}”", actor=request.user, url=d.get_absolute_url())
    return JsonResponse({"ok": True, "version": d.current_version, "status": d.status, "reopened": reopened})


# --- exports ------------------------------------------------------------------------------------------------

@login_required
def export(request, key, pk, number, fmt):
    project, d = _diagram(request, key, pk)
    v = _version(d, number)
    if fmt == "svg":
        body = R.to_svg(v.data, legend=request.GET.get("thumb") != "1")
        resp = HttpResponse(body, content_type="image/svg+xml")
        resp["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        if request.GET.get("download"):
            resp["Content-Disposition"] = f'attachment; filename="{_filename(d, v, "svg")}"'
        else:
            resp["Cache-Control"] = "private, max-age=3600"
        return resp
    if fmt == "pdf":
        page = request.GET.get("page") if request.GET.get("page") in R.PAGES else None
        resp = HttpResponse(R.to_pdf(v.data, title_block=title_block(d, v), page=page), content_type="application/pdf")
        disp = "inline" if request.GET.get("inline") else "attachment"
        resp["Content-Disposition"] = f'{disp}; filename="{_filename(d, v, "pdf")}"'
        audit(request, "diagram.exported", d, version=v.number, format="pdf")
        return resp
    if fmt == "json":
        resp = JsonResponse({"workbench_diagram": 1, "name": d.name, "level": d.level, "version": v.number, "data": v.data},
                            json_dumps_params={"indent": 1})
        resp["Content-Disposition"] = f'attachment; filename="{_filename(d, v, "json")}"'
        return resp
    raise Http404


@login_required
@require_POST
def preview_pdf(request, key, pk):
    """PDF of the unsaved drawing in the editor (only editors have one)."""
    project, d = _diagram(request, key, pk, edit=True)
    try:
        body = _json_body(request)
        data = geometry.clean(body.get("data"))
    except (BadRequest, ValueError) as exc:
        return _bad(str(exc))
    v = DiagramVersion(diagram=d, number=d.current_version, data=data, created_by=request.user, created_at=timezone.now())
    tb = title_block(d, v)
    tb["Version"] = f"v{d.current_version} + unsaved changes"
    page = body.get("page") if isinstance(body.get("page"), str) and body["page"] in R.PAGES else None
    resp = HttpResponse(R.to_pdf(data, title_block=tb, page=page), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{_filename(d, v, "pdf")}"'
    return resp
