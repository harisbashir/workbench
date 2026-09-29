"""Assembly-house package for a revision, and the in-house finishing of builds."""
import io
import zipfile

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.chat.models import post_system_message
from apps.core.utils import audit, notify
from apps.projects.models import Revision, log_activity

from . import fab
from .models import BuildOrder, BuildStep


def _revision(request, pk):
    rev = get_object_or_404(Revision.objects.select_related("project"), pk=pk)
    if not rev.project.can_view(request.user):
        raise PermissionDenied
    return rev


def _design_files(rev):
    from apps.design.models import Category, DesignFile
    current = DesignFile.objects.filter(revision=rev, is_current=True).order_by("-uploaded_at")
    gerber = next((f for f in current if f.category == Category.GERBER and f.ext == "zip"), None)
    pnp = next((f for f in current if f.category == Category.PNP), None)
    return gerber, pnp


def _placements(pnp):
    if not pnp:
        return None, []
    with pnp.file.open("rb") as fh:
        return fab.parse_placements(fh.read())


@login_required
def fab_package(request, pk):
    rev = _revision(request, pk)
    assembler = request.GET.get("for", "jlc")
    if assembler not in fab.ASSEMBLERS:
        assembler = "jlc"
    gerber, pnp = _design_files(rev)
    placements, pnp_warnings = _placements(pnp)
    p = fab.plan(rev, placements)
    base = f"{rev.project.key}-{rev.title}".replace(" ", "")
    get = request.GET.get("get")
    if get:
        who = fab.ASSEMBLERS[assembler]["name"].replace(" ", "")
        if get == "bom":
            data, name = fab.bom_csv(p, assembler), f"{base}-BOM-{who}.csv"
        elif get == "cpl":
            if placements is None:
                raise Http404
            data, name = fab.cpl_csv(p, placements), f"{base}-CPL-{who}.csv"
        elif get == "house":
            data, name = fab.house_csv(p), f"{base}-in-house-parts.csv"
        elif get == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                if gerber:
                    with gerber.file.open("rb") as fh:
                        z.writestr(f"{base}-gerbers.zip", fh.read())
                z.writestr(f"{base}-BOM-{who}.csv", fab.bom_csv(p, assembler))
                if placements is not None:
                    z.writestr(f"{base}-CPL-{who}.csv", fab.cpl_csv(p, placements))
                z.writestr(f"not for {who} - {base}-in-house-parts.csv", fab.house_csv(p))
                z.writestr("README.txt", (
                    f"{rev.project.name} {rev.title} — files for {fab.ASSEMBLERS[assembler]['name']}\n\n"
                    f"{fab.ASSEMBLERS[assembler]['note']}\n\n"
                    f"The assembler places {sum(len(l.refs) or l.quantity for l in p['fab'])} parts per board. "
                    f"{sum(l.quantity for l in p['house'])} parts are fitted in house afterwards "
                    f"(see the in-house parts list; don't upload it).\n").encode())
            data, name = buf.getvalue(), f"{base}-{who}-order-files.zip"
        else:
            raise Http404
        audit(request, "fab.package_downloaded", rev, file=name)
        resp = HttpResponse(data, content_type="application/zip" if name.endswith(".zip") else "text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp
    return render(request, "production/fab_package.html", {
        "rev": rev, "project": rev.project, "assembler": assembler, "assemblers": fab.ASSEMBLERS, "info": fab.ASSEMBLERS[assembler],
        "gerber": gerber, "pnp": pnp, "placements": placements, "pnp_warnings": pnp_warnings, "p": p,
        "fab_parts": sum(len(l.refs) or l.quantity for l in p["fab"]), "house_parts": sum(l.quantity for l in p["house"]),
        "can_edit": rev.project.can_edit(request.user),
    })


@login_required
@require_POST
def bom_fitted(request, pk):
    """Switch BOM lines between 'assembly house' and 'in house'."""
    from apps.inventory.models import BomLine, suggest_house
    rev = _revision(request, pk)
    if not rev.project.can_edit(request.user):
        raise PermissionDenied
    if rev.status == Revision.Status.RELEASED and not request.user.can_manage_projects:
        raise PermissionDenied("Only leads can change a released revision's BOM.")
    action = request.POST.get("action")
    if action == "suggest":
        changed = 0
        for line in rev.bom_lines.select_related("part"):
            want = "house" if suggest_house(line) else "fab"
            if line.fitted_by != want and want == "house":
                line.fitted_by = want
                line.save(update_fields=["fitted_by"])
                changed += 1
        messages.success(request, f"Marked {changed} through-hole part{'s' if changed != 1 else ''} as fitted in house." if changed
                         else "No through-hole parts found to mark. Switch lines one by one with the buttons.")
    else:
        line = get_object_or_404(BomLine, pk=request.POST.get("line"), revision=rev)
        line.fitted_by = "house" if request.POST.get("to") == "house" else "fab"
        line.save(update_fields=["fitted_by"])
        if request.headers.get("x-requested-with") == "fetch":
            return JsonResponse({"ok": True, "fitted_by": line.fitted_by})
    return redirect(request.POST.get("next") or f"/parts/boms/{rev.pk}/")


# --- Build workflow -----------------------------------------------------------------------

class FabOrderForm(forms.Form):
    fab_order_ref = forms.CharField(label="Order number", max_length=80, required=False,
                                    widget=forms.TextInput(attrs={"placeholder": "e.g. SO2609221234"}))
    fab_ordered_at = forms.DateField(label="Ordered on", required=False, widget=forms.DateInput(attrs={"type": "date"}))


class ReceiveForm(forms.Form):
    received_qty = forms.IntegerField(label="Boards received", min_value=0)
    fab_tracking = forms.CharField(label="Tracking number", max_length=120, required=False)


def _build(request, pk, edit=True):
    b = get_object_or_404(BuildOrder.objects.select_related("revision__project"), pk=pk)
    if not b.revision.project.can_view(request.user):
        raise PermissionDenied
    return b


def can_finish(user, b):
    """Technicians (engineers on the project) can record finishing progress."""
    return user.can_manage_procurement or b.revision.project.can_edit(user)


@login_required
@require_POST
def build_stage(request, pk):
    b = _build(request, pk)
    action = request.POST.get("action")
    project = b.revision.project
    if action in ("fab_ordered", "received", "start_house") and not request.user.can_manage_procurement:
        raise PermissionDenied
    if action in ("finished", "add_step", "delete_step") and not can_finish(request.user, b):
        raise PermissionDenied
    if b.status == BuildOrder.Status.PLANNED and b.firmware_releases.filter(status="recalled").exists() \
            and action in ("fab_ordered", "start_house"):
        messages.error(request, "This build uses recalled firmware. Choose a different firmware version first.")
        return redirect(b)
    if action == "fab_ordered" and b.stage == BuildOrder.Stage.PLANNED:
        form = FabOrderForm(request.POST)
        if form.is_valid():
            if b.shortage_count and "force" not in request.POST:
                messages.error(request, "Some in-house parts are short. Order them first, or tick “continue anyway”.")
                return redirect(b)
            b.fab_order_ref = form.cleaned_data["fab_order_ref"]
            b.fab_ordered_at = form.cleaned_data["fab_ordered_at"] or timezone.localdate()
            b.stage = BuildOrder.Stage.AT_FAB
            b.save(update_fields=["fab_order_ref", "fab_ordered_at", "stage"])
            b.start()
            b.consume_stock(request.user)
            b.create_steps()
            who = b.manufacturer or "the assembly house"
            log_activity(project, f"ordered assembly of {b.number} from {who}" + (f" ({b.fab_order_ref})" if b.fab_order_ref else ""),
                         actor=request.user, url=b.get_absolute_url())
            post_system_message(project, f"Build {b.number}: {b.quantity} × {b.revision.title} ordered from {who}. "
                                         "In-house parts were taken from stock for finishing.", b.get_absolute_url())
            messages.success(request, f"{b.number} is now at {who}. In-house parts for {b.quantity} boards were reserved from stock.")
    elif action == "start_house" and b.stage == BuildOrder.Stage.PLANNED:
        if b.shortage_count and "force" not in request.POST:
            messages.error(request, "Some parts are short. Order them first, or tick “start anyway”.")
            return redirect(b)
        b.start()
        b.consume_stock(request.user)
        b.create_steps()
        b.stage = BuildOrder.Stage.FINISHING
        b.save(update_fields=["stage"])
        log_activity(project, f"started build {b.number}", actor=request.user, url=b.get_absolute_url())
        post_system_message(project, f"Build {b.number} ({b.quantity} × {b.revision.title}) has started. Parts have been taken from stock.", b.get_absolute_url())
        messages.success(request, f"{b.number} started. Parts for {b.quantity} boards were taken out of stock.")
    elif action == "received" and b.stage == BuildOrder.Stage.AT_FAB:
        form = ReceiveForm(request.POST)
        if form.is_valid():
            b.received_qty = form.cleaned_data["received_qty"]
            b.fab_tracking = form.cleaned_data["fab_tracking"] or b.fab_tracking
            b.received_at = timezone.now()
            b.stage = BuildOrder.Stage.FINISHING if b.steps.exists() else BuildOrder.Stage.TESTING
            b.save(update_fields=["received_qty", "fab_tracking", "received_at", "stage"])
            short = b.quantity - b.received_qty
            text = f"Build {b.number}: {b.received_qty} assembled boards received" + (f" ({short} fewer than ordered)" if short > 0 else "")
            log_activity(project, text[0].lower() + text[1:], actor=request.user, url=b.get_absolute_url())
            post_system_message(project, text + (". Ready for in-house finishing." if b.steps.exists() else ". Ready for test."), b.get_absolute_url())
            messages.success(request, text + ".")
    elif action == "finished" and b.stage == BuildOrder.Stage.FINISHING:
        b.stage = BuildOrder.Stage.TESTING
        b.save(update_fields=["stage"])
        log_activity(project, f"finished in-house assembly of {b.number}", actor=request.user, url=b.get_absolute_url())
        if not b.steps_done:
            messages.warning(request, "Moved to testing, although not every finishing step is ticked off for every board.")
        else:
            messages.success(request, "Finishing done. Next: flash and test.")
    elif action == "reopen" and b.stage == BuildOrder.Stage.TESTING and b.steps.exists():
        b.stage = BuildOrder.Stage.FINISHING
        b.save(update_fields=["stage"])
    elif action == "add_step":
        title = (request.POST.get("title") or "").strip()[:160]
        if title:
            last = b.steps.order_by("-order").first()
            BuildStep.objects.create(build=b, title=title, detail=(request.POST.get("detail") or "")[:200],
                                     per_board=0, order=(last.order + 1) if last else 0)
        return redirect(f"{b.get_absolute_url()}#finishing")
    elif action == "delete_step":
        get_object_or_404(BuildStep, pk=request.POST.get("step"), build=b).delete()
        return redirect(f"{b.get_absolute_url()}#finishing")
    audit(request, f"build.{action}", b)
    return redirect(b)


@login_required
@require_POST
def build_step(request, pk, step_pk):
    """+1 / +5 / all / -1 on a finishing step. Works with and without JavaScript."""
    b = _build(request, pk)
    if not can_finish(request.user, b):
        raise PermissionDenied
    step = get_object_or_404(BuildStep, pk=step_pk, build=b)
    total = b.board_qty
    was_done = b.steps_done
    how = request.POST.get("how", "1")
    if how == "all":
        step.done_qty = total
    elif how == "none":
        step.done_qty = 0
    else:
        try:
            delta = int(how)
        except ValueError:
            delta = 0
        step.done_qty = max(0, min(total, step.done_qty + delta))
    step.updated_by, step.updated_at = request.user, timezone.now()
    step.save(update_fields=["done_qty", "updated_by", "updated_at"])
    all_done = b.steps_done
    if all_done and not was_done and project_lead_should_hear(b):
        notify(b.revision.project.lead, f"{b.number}: in-house finishing is done on all {total} boards", b.get_absolute_url())
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"done": step.done_qty, "total": total, "percent": step.percent, "complete": step.complete,
                             "all_done": all_done})
    return redirect(f"{b.get_absolute_url()}#finishing")


def project_lead_should_hear(b):
    lead = b.revision.project.lead
    return bool(lead) and b.stage == BuildOrder.Stage.FINISHING


@login_required
def traveler(request, pk):
    """A printable sheet that goes with the boards: what to fit, where, how many."""
    b = _build(request, pk)
    return render(request, "production/traveler.html", build_context(b, request.user))


def build_context(b, user):
    """Finishing data shared by the build page and the traveler."""
    from apps.design.render import gerber_files, render_files
    steps = list(b.steps.select_related("bom_line__part", "updated_by"))
    board = None
    markers, off_board = [], 0
    gerber, pnp = _design_files(b.revision)
    files = gerber_files(b.revision)
    if files:
        board = render_files(files)
    if board and board.get("ok") and pnp:
        placements, _w = _placements(pnp)
        placed = {p["ref"]: p for p in placements or []}
        vb = [float(v) for v in board.get("viewbox", "0 0 0 0").split()]
        by_step = {}
        for s in steps:
            if s.bom_line:
                for ref in s.bom_line.refs:
                    if ref in placed:
                        by_step.setdefault(s.pk, []).append(placed[ref])
        n = 0
        for s in steps:
            for p in by_step.get(s.pk, []):
                x, y = p["x"], -p["y"]
                inside = vb[2] and vb[0] <= x <= vb[0] + vb[2] and vb[1] <= y <= vb[1] + vb[3]
                if not inside:
                    off_board += 1
                    continue
                n += 1
                r = max(vb[2], vb[3]) / 45
                fs = max(vb[2], vb[3]) / 36
                markers.append({"x": round(x, 3), "y": round(y, 3), "ref": p["ref"], "bottom": p["side"] == "bottom",
                                "step": s.pk, "r": round(r, 3), "fs": round(fs, 3), "ty": round(y + r + fs * 0.95, 3)})
        if off_board and not markers:
            markers = []
    step_index = {s.pk: i + 1 for i, s in enumerate(steps)}
    for m in markers:
        m["n"] = step_index.get(m["step"])
    return {"b": b, "steps": steps, "board": board if board and board.get("ok") else None, "markers": markers,
            "off_board": off_board, "can_finish": can_finish(user, b), "viewbox": board.get("viewbox") if board else "",
            "firmware": b.firmware_releases.select_related("firmware").prefetch_related("artifacts")}
