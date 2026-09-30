import csv
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.chat.models import post_system_message
from apps.core.utils import audit, procurement_required
from apps.projects.models import Project, Revision, log_activity

from . import bom_import
from .forms import BomLineForm, BomUploadForm, PartForm, StockAdjustForm, SupplierForm
from .models import BomLine, Part, StockMovement, Supplier


# --- Parts ---------------------------------------------------------------------

@login_required
def part_list(request):
    qs = Part.objects.select_related("supplier")
    q = request.GET.get("q", "").strip()
    cat = request.GET.get("category", "")
    stock = request.GET.get("stock", "")
    if q:
        qs = qs.filter(Q(ipn__icontains=q) | Q(description__icontains=q) | Q(mpn__icontains=q) |
                       Q(value__icontains=q) | Q(manufacturer__icontains=q) | Q(footprint__icontains=q))
    if cat:
        qs = qs.filter(category=cat)
    if stock == "low":
        qs = qs.filter(stock__lt=F("min_stock"))
    elif stock == "out":
        qs = qs.filter(stock__lte=0)
    totals = Part.objects.filter(stock__gt=0).aggregate(
        n=Sum(ExpressionWrapper(F("stock") * F("unit_cost"), output_field=DecimalField(max_digits=18, decimal_places=4))))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "inventory/part_list.html", {
        "page": page, "q": q, "category": cat, "stock": stock, "categories": Part.Category.choices,
        "low_count": Part.objects.filter(stock__lt=F("min_stock")).count(),
        "stock_value": totals["n"] or 0, "part_count": Part.objects.count(),
        "can_edit": request.user.can_manage_procurement or not request.user.is_read_only,
    })


@login_required
def part_detail(request, pk):
    part = get_object_or_404(Part.objects.select_related("supplier"), pk=pk)
    visible = Project.objects.visible_to(request.user)
    return render(request, "inventory/part_detail.html", {
        "part": part,
        "movements": part.movements.select_related("user")[:30],
        "used_in": part.bom_lines.filter(revision__project__in=visible).select_related("revision__project"),
        "open_pos": part.purchaseorderline_set.filter(order__status__in=["ordered", "partial"]).select_related("order"),
        "adjust_form": StockAdjustForm(),
    })


def _can_edit_parts(user):
    return not user.is_read_only


@login_required
def part_edit(request, pk=None):
    if not _can_edit_parts(request.user):
        raise PermissionDenied("Your account is read-only.")
    part = get_object_or_404(Part, pk=pk) if pk else None
    form = PartForm(request.POST or None, instance=part)
    if request.method == "POST" and form.is_valid():
        part = form.save()
        audit(request, "part.saved", part)
        messages.success(request, f"Saved {part.ipn}.")
        return redirect(part)
    return render(request, "inventory/part_form.html", {"form": form, "part": part})


@procurement_required
@require_POST
def part_adjust(request, pk):
    part = get_object_or_404(Part, pk=pk)
    form = StockAdjustForm(request.POST)
    if form.is_valid():
        qty, mode = form.cleaned_data["quantity"], form.cleaned_data["mode"]
        with transaction.atomic():
            # "Set exact count" is computed from the stock as it is now, not as the page showed it.
            part = Part.objects.select_for_update().get(pk=part.pk)
            delta = qty if mode == "add" else -qty if mode == "remove" else qty - part.stock
            if delta:
                part.adjust_stock(delta, StockMovement.Reason.ADJUST, user=request.user, reference=form.cleaned_data["note"])
        if delta:
            audit(request, "stock.adjusted", part, delta=delta, note=form.cleaned_data["note"])
        messages.success(request, f"Stock for {part.ipn} is now {part.stock}.")
    else:
        messages.error(request, "Enter a quantity.")
    return redirect(part)


@login_required
def part_export(request):
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="parts.csv"'
    w = csv.writer(resp)
    w.writerow(["Part number", "Description", "Category", "Value", "Footprint", "Manufacturer", "MPN",
                "Supplier", "Supplier SKU", "Unit cost", "Stock", "Reorder level", "Location", "Lifecycle"])
    for p in Part.objects.select_related("supplier"):
        w.writerow([p.ipn, p.description, p.get_category_display(), p.value, p.footprint, p.manufacturer, p.mpn,
                    p.supplier or "", p.supplier_sku, p.unit_cost, p.stock, p.min_stock, p.location, p.get_lifecycle_display()])
    return resp


# --- Suppliers ---------------------------------------------------------------

@login_required
def supplier_list(request):
    return render(request, "inventory/supplier_list.html", {"suppliers": Supplier.objects.all()})


@procurement_required
def supplier_edit(request, pk=None):
    supplier = get_object_or_404(Supplier, pk=pk) if pk else None
    form = SupplierForm(request.POST or None, instance=supplier)
    if request.method == "POST" and form.is_valid():
        s = form.save()
        audit(request, "supplier.saved", s)
        messages.success(request, f"Saved {s.name}.")
        return redirect("inventory:supplier_list")
    return render(request, "inventory/supplier_form.html", {"form": form, "supplier": supplier})


# --- BOMs -------------------------------------------------------------------------

def _revision_for(request, pk, edit=False):
    rev = get_object_or_404(Revision.objects.select_related("project"), pk=pk)
    if not rev.project.can_view(request.user):
        raise PermissionDenied("You're not a member of this project.")
    if edit and not rev.project.can_edit(request.user):
        raise PermissionDenied("You can view this BOM but not change it.")
    if edit and rev.status == Revision.Status.RELEASED and not request.user.can_manage_projects:
        raise PermissionDenied("This revision is released. Only a lead can change its BOM.")
    return rev


def _can_edit_bom(user, rev):
    """Mirrors _revision_for(edit=True), so the page only offers what the user may do."""
    if not rev.project.can_edit(user):
        return False
    return rev.status != Revision.Status.RELEASED or user.can_manage_projects


@login_required
def bom_index(request):
    projects = list(Project.objects.visible_to(request.user).exclude(status=Project.Status.ARCHIVED).prefetch_related("revisions"))
    for p in projects:
        p.user_can_edit = p.can_edit(request.user)
    return render(request, "inventory/bom_index.html", {"projects": projects})


@login_required
def bom(request, pk):
    rev = _revision_for(request, pk)
    lines = list(rev.bom_lines.select_related("part", "part__supplier"))
    can_edit = _can_edit_bom(request.user, rev)
    fitted = [l for l in lines if not l.dnp]
    cost = sum((l.line_cost for l in fitted), Decimal(0))
    buildable = min((l.part.stock // l.quantity for l in fitted if l.quantity), default=0) if fitted else 0
    for l in lines:
        l.can_build = l.part.stock // l.quantity if l.quantity else 0
    others = rev.project.revisions.exclude(pk=rev.pk)
    return render(request, "inventory/bom.html", {
        "rev": rev, "project": rev.project, "lines": lines, "cost": cost, "buildable": max(buildable, 0),
        "placements": sum(l.quantity for l in fitted), "unique_parts": len(fitted),
        "can_edit": can_edit, "others": others,
        "limiting": sorted(fitted, key=lambda l: l.can_build)[:3] if fitted else [],
        "house_lines": [l for l in fitted if l.fitted_by == "house"],
        "house_parts": sum(l.quantity for l in fitted if l.fitted_by == "house"),
        "fab_parts": sum(l.quantity for l in fitted if l.fitted_by != "house"),
        "can_set_fitted": can_edit,
    })


@login_required
def bom_line_edit(request, pk, line_pk=None):
    rev = _revision_for(request, pk, edit=True)
    line = get_object_or_404(BomLine, pk=line_pk, revision=rev) if line_pk else None
    form = BomLineForm(request.POST or None, instance=line)
    if request.method == "POST":
        if "delete" in request.POST and line:
            line.delete()
            messages.success(request, "Line removed.")
            return redirect("inventory:bom", pk=rev.pk)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.revision = rev
            if not line and BomLine.objects.filter(revision=rev, part=obj.part).exists():
                form.add_error("part", "That part is already on this BOM — edit the existing line instead.")
            else:
                obj.save()
                messages.success(request, "BOM updated.")
                return redirect("inventory:bom", pk=rev.pk)
    return render(request, "inventory/bom_line_form.html", {"form": form, "rev": rev, "line": line})


@login_required
def bom_import_view(request, pk):
    rev = _revision_for(request, pk, edit=True)
    session_key = f"bom_import_{rev.pk}"
    if request.method == "POST" and "confirm" in request.POST:
        data = request.session.pop(session_key, None)
        if not data:
            messages.error(request, "The upload expired. Please upload the file again.")
            return redirect("inventory:bom_import", pk=rev.pk)
        created, linked, notes = _apply_import(rev, data["rows"], data["replace"], request.user)
        for n in notes:
            messages.warning(request, n)
        log_activity(rev.project, f"imported a BOM for {rev.title} ({len(data['rows'])} lines, {created} new parts)", actor=request.user,
                     url=rev.get_absolute_url())
        post_system_message(rev.project, f"{request.user.display_name} imported a new BOM for {rev.title}: {len(data['rows'])} lines, {created} new parts added to the library.", rev.get_absolute_url())
        audit(request, "bom.imported", rev, lines=len(data["rows"]), new_parts=created)
        messages.success(request, f"BOM imported: {len(data['rows'])} lines, {linked} matched to existing parts, {created} new parts created.")
        return redirect("inventory:bom", pk=rev.pk)

    form = BomUploadForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        rows, warnings = bom_import.parse(form.cleaned_data["file"].read())
        for w in warnings:
            messages.warning(request, w)
        if rows:
            for r in rows:
                part, how = bom_import.match_part(r)
                r["match_id"] = part.pk if part else None
                r["match_label"] = str(part) if part else ""
                r["match_how"] = how
            request.session[session_key] = {"rows": rows, "replace": form.cleaned_data["replace"]}
            preview = {"rows": rows, "new": sum(1 for r in rows if not r["match_id"]), "replace": form.cleaned_data["replace"],
                       "mixed_dnp": _mixed_dnp(rows)}
    return render(request, "inventory/bom_import.html", {"form": form, "rev": rev, "preview": preview})


def _mixed_dnp(rows):
    """Parts that appear both fitted and DNP in an import (they end up on one BOM line)."""
    groups = {}
    for r in rows:
        key = ("id", r["match_id"]) if r.get("match_id") else (
            ("mpn", r["mpn"].lower()) if r["mpn"] else ("vf", r["value"].lower(), r["footprint"].lower()))
        groups.setdefault(key, []).append(r)
    out = []
    for rs in groups.values():
        if len({r["dnp"] for r in rs}) > 1:
            out.append(" / ".join(r["references"] or r["value"] for r in rs))
    return out


def _dnp_note(line, refs):
    refs = [r for r in refs if r]
    if not refs:
        return
    note = "Not fitted (DNP): " + ", ".join(refs)
    line.notes = (f"{line.notes}; {note}" if line.notes else note)[:200]


def _apply_import(rev, rows, replace, user):
    """Returns (parts created, rows linked to existing parts, warnings).

    A BOM line is either fitted or DNP. When the same part shows up in a fitted row
    and a DNP row (KiCad/KiBot group them separately), the fitted references stay on
    the line and the DNP ones are recorded in the line's notes, so they are neither
    placed by the assembler nor taken from stock.
    """
    created = linked = 0
    notes = []
    with transaction.atomic():
        if replace:
            rev.bom_lines.all().delete()
        for r in rows:
            part = Part.objects.filter(pk=r.get("match_id")).first() if r.get("match_id") else None
            if part is None:
                part, how = bom_import.match_part(r)  # may have been created by an earlier row
            if part is None:
                part = Part.objects.create(
                    description=r["description"][:200] or r["value"] or "Imported part",
                    category=r["category"], value=r["value"][:60], footprint=r["footprint"][:120],
                    manufacturer=r["manufacturer"][:120], mpn=r["mpn"][:120], datasheet_url=r["datasheet"][:200],
                )
                created += 1
            else:
                linked += 1
            line, made = BomLine.objects.get_or_create(revision=rev, part=part, defaults={
                "quantity": r["quantity"], "references": r["references"], "dnp": r["dnp"]})
            if not made:
                if line.dnp == r["dnp"]:
                    line.quantity += r["quantity"]
                    line.references = ", ".join(x for x in [line.references, r["references"]] if x)
                elif r["dnp"]:  # fitted line, DNP row: leave the DNP parts off
                    _dnp_note(line, [r["references"]])
                    notes.append(f"{part.ipn}: {r['references'] or 'some references'} marked DNP but the same part is fitted "
                                 f"as {line.references or 'other references'} — the DNP ones were left off the line (see its notes).")
                else:  # DNP line, fitted row: the line becomes fitted
                    _dnp_note(line, [line.references])
                    notes.append(f"{part.ipn}: {line.references or 'some references'} marked DNP but the same part is fitted "
                                 f"as {r['references'] or 'other references'} — the line is fitted; the DNP ones are in its notes.")
                    line.dnp = False
                    line.quantity = r["quantity"]
                    line.references = r["references"]
                line.save()
    return created, linked, notes


@login_required
def bom_export(request, pk):
    rev = _revision_for(request, pk)
    resp = HttpResponse(content_type="text/csv")
    fname = f"{rev.project.key}-{rev.title}-BOM.csv".replace(" ", "_")
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    w = csv.writer(resp)
    w.writerow(["Part number", "Qty per board", "References", "Value", "Footprint", "Manufacturer", "MPN",
                "Supplier", "Supplier SKU", "Unit cost", "Line cost", "DNP"])
    for l in rev.bom_lines.select_related("part", "part__supplier"):
        p = l.part
        w.writerow([p.ipn, l.quantity, l.references, p.value, p.footprint, p.manufacturer, p.mpn,
                    p.supplier or "", p.supplier_sku, p.unit_cost, l.line_cost, "yes" if l.dnp else ""])
    return resp


@login_required
def bom_compare(request, pk, other_pk):
    a = _revision_for(request, other_pk)
    b = _revision_for(request, pk)
    la = {l.part_id: l for l in a.bom_lines.select_related("part")}
    lb = {l.part_id: l for l in b.bom_lines.select_related("part")}
    rows = []
    for pid in sorted(set(la) | set(lb), key=lambda i: (la.get(i) or lb.get(i)).part.ipn):
        x, y = la.get(pid), lb.get(pid)
        if x and not y:
            change = "removed"
        elif y and not x:
            change = "added"
        elif x.quantity != y.quantity or x.dnp != y.dnp or x.references != y.references:
            change = "changed"
        else:
            change = "same"
        rows.append({"part": (x or y).part, "old": x, "new": y, "change": change,
                     "references": (y.references if y else "") or (x.references if x else "")})
    summary = {k: sum(1 for r in rows if r["change"] == k) for k in ("added", "removed", "changed", "same")}
    return render(request, "inventory/bom_compare.html", {"a": a, "b": b, "rows": rows, "summary": summary,
                                                          "hide_same": request.GET.get("all") != "1"})
