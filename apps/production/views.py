from collections import defaultdict

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.chat.models import post_system_message
from apps.core.utils import audit, notify, procurement_required
from apps.inventory.models import BASE_CURRENCY, Part, Supplier
from apps.projects.models import Project, Revision, log_activity

from .models import BuildFirmware, BuildOrder, PurchaseOrder, PurchaseOrderLine


class DateInput(forms.DateInput):
    input_type = "date"


class POForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "reference", "expected_date", "shipping_cost", "notes"]
        widgets = {"expected_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class POLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderLine
        fields = ["part", "quantity", "unit_cost"]

    def __init__(self, *args, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.fields["unit_cost"].required = False
        self.fields["quantity"].min_value = 1
        self.fields["quantity"].widget.attrs["min"] = 1
        if order is not None and not order.supplier.in_base_currency:
            self.fields["unit_cost"].label = f"Unit cost ({order.currency})"
            self.fields["unit_cost"].help_text = (f"{order.supplier} quotes in {order.currency}; part costs are kept in "
                                                  f"{BASE_CURRENCY}, so enter the {order.currency} price.")
        else:
            self.fields["unit_cost"].help_text = "Leave as 0 to use the part's current cost."

    def clean_quantity(self):
        q = self.cleaned_data["quantity"]
        if q is None or q < 1:
            raise forms.ValidationError("Order at least one.")
        return q

    def clean(self):
        data = super().clean()
        if self.order is not None and not self.order.supplier.in_base_currency and not data.get("unit_cost"):
            self.add_error("unit_cost", f"Enter the price in {self.order.currency}: the part's cost is in {BASE_CURRENCY}.")
        return data


class BuildForm(forms.ModelForm):
    class Meta:
        model = BuildOrder
        fields = ["revision", "quantity", "assembly", "manufacturer", "due_date", "serial_prefix", "notes"]
        widgets = {"due_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["revision"].queryset = Revision.objects.filter(
            project__in=Project.objects.visible_to(user)).exclude(status=Revision.Status.OBSOLETE).select_related("project")
        self.fields["manufacturer"].queryset = Supplier.objects.filter(kind__in=["assembly", "pcb", "other"])
        self.fields["manufacturer"].empty_label = "—"
        self.fields["manufacturer"].label = "Assembly house"
        self.fields["manufacturer"].help_text = "e.g. JLCPCB or Seeed Fusion. Add them under Parts → Suppliers."
        self.fields["assembly"].widget = forms.RadioSelect(choices=BuildOrder.Assembly.choices)

class CompleteForm(forms.Form):
    passed = forms.IntegerField(min_value=0, label="Boards that passed test")
    failed = forms.IntegerField(min_value=0, initial=0, label="Boards that failed test")

    def __init__(self, *args, boards=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.boards = boards

    def clean(self):
        data = super().clean()
        passed, failed = data.get("passed"), data.get("failed")
        if self.boards is not None and passed is not None and failed is not None and passed + failed > self.boards:
            raise forms.ValidationError(f"Passed plus failed ({passed + failed}) is more than the {self.boards} boards in this build.")
        return data


# --- Purchase orders ---------------------------------------------------------

@login_required
def po_list(request):
    status = request.GET.get("status", "open")
    qs = PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines")
    if status == "open":
        qs = qs.filter(status__in=PurchaseOrder.OPEN)
    elif status in dict(PurchaseOrder.Status.choices):
        qs = qs.filter(status=status)
    return render(request, "production/po_list.html", {"orders": qs, "status": status, "statuses": PurchaseOrder.Status.choices})


@procurement_required
def po_create(request):
    initial = {}
    if request.GET.get("supplier", "").isdigit():
        initial["supplier"] = int(request.GET["supplier"])
    form = POForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        po = form.save(commit=False)
        po.created_by = request.user
        po.save()
        audit(request, "po.created", po)
        messages.success(request, f"{po.number} created as a draft. Add the parts you want to order.")
        return redirect(po)
    return render(request, "production/po_form.html", {"form": form})


@login_required
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related("supplier"), pk=pk)
    can_edit = request.user.can_manage_procurement
    if request.method == "POST" and not can_edit:
        raise PermissionDenied
    line_form = POLineForm(request.POST or None, order=po) if po.is_editable else None
    if request.method == "POST" and line_form is not None:
        if line_form.is_valid():
            line = line_form.save(commit=False)
            line.order = po
            if not line.unit_cost:
                line.unit_cost = line.part.unit_cost
            with transaction.atomic():
                if not PurchaseOrder.objects.select_for_update().filter(pk=po.pk, status=PurchaseOrder.Status.DRAFT).exists():
                    messages.error(request, f"{po.number} has been sent, so its lines can't change any more.")
                    return redirect(po)
                existing = po.lines.filter(part=line.part).first()
                if existing:
                    existing.quantity += line.quantity
                    existing.save()
                else:
                    line.save()
            return redirect(po)
    if line_form is not None:
        line_form.fields["part"].queryset = Part.objects.order_by("supplier_id", "ipn")
    return render(request, "production/po_detail.html", {
        "po": po, "lines": po.lines.select_related("part"), "line_form": line_form, "can_edit": can_edit,
        "receiving": can_edit and po.status in (PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIAL),
        "base_currency": BASE_CURRENCY, "foreign_currency": not po.supplier.in_base_currency,
    })


@procurement_required
@require_POST
def po_action(request, pk):
    get_object_or_404(PurchaseOrder, pk=pk)
    action = request.POST.get("action")
    remove = request.POST.get("remove_line", "")
    S = PurchaseOrder.Status
    with transaction.atomic():
        # Lock the order: every check below sees its current state, and two people
        # pressing buttons on the same order are handled one after the other.
        po = PurchaseOrder.objects.select_for_update().select_related("supplier").get(pk=pk)
        if remove:
            if po.is_editable and remove.isdigit():
                po.lines.filter(pk=int(remove)).delete()
            return redirect(po)
        if action == "order" and po.is_editable:
            if not po.lines.exists():
                messages.error(request, "Add at least one part before marking the order as sent.")
                return redirect(po)
            po.status = S.ORDERED
            po.ordered_at = timezone.now()
            po.save()
            audit(request, "po.ordered", po, total=po.total)
            messages.success(request, f"{po.number} marked as ordered. Record deliveries here when they arrive.")
        elif action in ("receive", "receive_all") and po.status in (S.ORDERED, S.PARTIAL):
            received = 0
            for line in po.lines.select_related("part"):
                if action == "receive_all":
                    qty = line.outstanding
                else:
                    raw = request.POST.get(f"recv_{line.pk}", "").strip()
                    qty = int(raw) if raw.isdigit() else 0
                if qty > 0:
                    received += line.receive(qty, request.user)
            po.refresh_status()
            audit(request, "po.received", po, items=received)
            if action == "receive_all":
                messages.success(request, f"Received everything outstanding ({received} items) into stock.")
            else:
                messages.success(request, f"Received {received} items into stock." if received else "Nothing received — enter quantities first.")
            if received and not po.supplier.in_base_currency:
                messages.info(request, f"This order is in {po.currency}, so part costs ({BASE_CURRENCY}) weren't updated from it.")
        elif action in ("receive", "receive_all"):
            messages.info(request, f"{po.number} is {po.get_status_display().lower()}, so nothing was received.")
        elif action == "cancel" and po.status in (S.DRAFT, S.ORDERED):
            po.status = S.CANCELLED
            po.save(update_fields=["status"])
            audit(request, "po.cancelled", po)
            messages.info(request, f"{po.number} cancelled.")
        elif action == "close" and po.status == S.PARTIAL:
            po.status = S.CLOSED
            po.save(update_fields=["status"])
            short = sum(line.outstanding for line in po.lines.all())
            audit(request, "po.closed", po, outstanding=short)
            messages.info(request, f"{po.number} closed: {short} item{'s' if short != 1 else ''} won't be delivered.")
    return redirect(po)


# --- Builds ------------------------------------------------------------------------

@login_required
def build_list(request):
    visible = Project.objects.visible_to(request.user)
    builds = BuildOrder.objects.filter(revision__project__in=visible).select_related("revision__project", "manufacturer")
    return render(request, "production/build_list.html", {"builds": builds})


@procurement_required
def build_create(request):
    initial = {}
    if request.GET.get("revision", "").isdigit():
        initial["revision"] = int(request.GET["revision"])
    form = BuildForm(request.user, request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        b = form.save(commit=False)
        b.created_by = request.user
        with transaction.atomic():
            b.save()
            b.copy_firmware_from_revision()
        log_activity(b.revision.project, f"planned build {b.number}: {b.quantity} × {b.revision.title}", actor=request.user, url=b.get_absolute_url())
        audit(request, "build.created", b)
        if not b.revision.bom_lines.exists():
            messages.warning(request, f"{b.revision} has no BOM yet, so Workbench can't check parts. Import the BOM first.")
        return redirect(b)
    return render(request, "production/build_form.html", {"form": form})


def fw_versions(b, firmware_id):
    """The versions a build may use for one firmware: released or testing."""
    from apps.firmware.models import FirmwareRelease
    return FirmwareRelease.objects.filter(firmware_id=firmware_id, firmware__project=b.revision.project,
                                          status__in=["released", "testing"]).order_by("-sort_key")


def _set_build_firmware(request, b):
    """Apply the build page's firmware form: change versions, remove rows, add one. Returns how many choices were
    refused, or None if the build was finished or cancelled meanwhile (nothing is changed)."""
    from apps.firmware.models import FirmwareRelease
    skipped = 0
    with transaction.atomic():
        if not BuildOrder.objects.select_for_update().filter(pk=b.pk, status__in=BuildOrder.ACTIVE).exists():
            return None
        for row in b.build_firmware.select_related("release"):
            if request.POST.get(f"remove_{row.pk}"):
                row.delete()
                continue
            raw = request.POST.get(f"row_{row.pk}", "")
            if raw.isdigit() and int(raw) != row.release_id:
                rel = fw_versions(b, row.release.firmware_id).filter(pk=int(raw)).first()
                if rel is None or (row.slot_id is None and b.build_firmware.filter(slot=None, release=rel).exists()):
                    skipped += 1
                else:
                    row.release = rel
                    row.save(update_fields=["release"])
        raw = request.POST.get("add", "")
        if raw.isdigit():
            rel = FirmwareRelease.objects.filter(pk=int(raw), firmware__project=b.revision.project,
                                                 status__in=["released", "testing"]).first()
            if rel is None:
                skipped += 1
            else:
                BuildFirmware.objects.get_or_create(build=b, release=rel, slot=None)
    chosen = ", ".join(str(r.release) for r in b.build_firmware.select_related("release__firmware")) or "none"
    audit(request, "build.firmware", b, firmware=chosen)
    log_activity(b.revision.project, f"set firmware for build {b.number}: {chosen}", actor=request.user, url=b.get_absolute_url())
    return skipped


@login_required
def build_detail(request, pk):
    b = get_object_or_404(BuildOrder.objects.select_related("revision__project", "manufacturer"), pk=pk)
    if not b.revision.project.can_view(request.user):
        raise PermissionDenied
    reqs = b.requirements()
    from apps.firmware.models import FirmwareRelease
    from apps.firmware.sets import Releases, issues_for
    rows = list(b.firmware_rows())
    known = Releases([row.release.firmware_id for row in rows], b.revision)
    firmware = [{"row": row, "rel": row.release, "issues": issues_for(row.release, b.revision, known),
                 "versions": [r for r in known.by_firmware[row.release.firmware_id] if r.status in ("released", "testing")]}
                for row in rows]
    fw_choices = list(FirmwareRelease.objects.filter(firmware__project=b.revision.project, status__in=["released", "testing"])
                      .exclude(build_firmware__build=b).select_related("firmware").order_by("firmware__name", "-sort_key"))
    from .fab_views import FabOrderForm, ReceiveForm, build_context
    ctx = build_context(b, request.user)
    stages = [("planned", "Planned")]
    if b.by_fab:
        stages += [("at_fab", "At the assembly house"), ("finishing", "Finishing in house")]
    else:
        stages += [("finishing", "Assembly in house")]
    stages += [("testing", "Test & flash"), ("done", "Done")]
    keys = [k for k, _ in stages]
    current = keys.index(b.stage) if b.stage in keys else 0
    ctx.update({
        "reqs": reqs, "short": [r for r in reqs if r["short"]], "firmware": firmware, "fw_choices": fw_choices,
        "complete_form": CompleteForm(initial={"passed": b.board_qty, "failed": 0}),
        "recalled": [f["rel"] for f in firmware if f["rel"].status == "recalled"],
        "consumed": _consumed_rows(b) if b.stock_consumed else [],
        "fab_form": FabOrderForm(initial={"fab_ordered_at": timezone.localdate()}),
        "receive_form": ReceiveForm(initial={"received_qty": b.quantity}),
        "can_edit": request.user.can_manage_procurement,
        "fw_editable": request.user.can_manage_procurement and b.status in BuildOrder.ACTIVE,
        "stages": [{"key": k, "label": label, "state": "done" if i < current else ("now" if i == current else "todo")}
                   for i, (k, label) in enumerate(stages)] if b.status != "cancelled" else [],
        "steps_total": len(ctx["steps"]), "steps_complete": sum(1 for s in ctx["steps"] if s.complete),
    })
    return render(request, "production/build_detail.html", ctx)


@procurement_required
@require_POST
def build_action(request, pk):
    b = get_object_or_404(BuildOrder.objects.select_related("revision__project"), pk=pk)
    action = request.POST.get("action")
    project = b.revision.project
    S = BuildOrder.Status
    from apps.firmware.models import FirmwareRelease
    recalled = FirmwareRelease.objects.filter(build_firmware__build=b, status="recalled")
    if action == "start":
        if b.status != S.PLANNED:
            messages.info(request, f"{b.number} is {b.get_status_display().lower()}, so it can't be started.")
            return redirect(b)
        if recalled.exists():
            messages.error(request, "This build uses recalled firmware. Choose a different firmware version before starting.")
            return redirect(b)
        if b.shortage_count and "force" not in request.POST:
            messages.error(request, "Some parts are short. Order them first, or tick “start anyway”.")
            return redirect(b)
        with transaction.atomic():
            started = b.begin(BuildOrder.Stage.AT_FAB if b.by_fab else BuildOrder.Stage.FINISHING)
            if started:
                b.consume_stock(request.user)
                b.create_steps()
        if not started:
            messages.info(request, f"{b.number} had already been started.")
            return redirect(b)
        log_activity(project, f"started build {b.number}", actor=request.user, url=b.get_absolute_url())
        post_system_message(project, f"Build {b.number} ({b.quantity} × {b.revision.title}) has started. Parts have been taken from stock.", b.get_absolute_url())
        messages.success(request, f"{b.number} started. Parts for {b.quantity} boards were taken out of stock.")
    elif action == "complete" and b.status in BuildOrder.ACTIVE:
        if recalled.exists() and "force" not in request.POST:
            messages.error(request, "This build uses recalled firmware (" + ", ".join(str(r) for r in recalled) + "). "
                           "Re-flash the boards with a good version and change the build's firmware, or tick “complete anyway”.")
            return redirect(b)
        form = CompleteForm(request.POST, boards=b.board_qty)
        if not form.is_valid():
            for err in form.errors.get("__all__", []) + [e for f, es in form.errors.items() if f != "__all__" for e in es]:
                messages.error(request, err)
            return redirect(b)
        if not b.complete(request.user, form.cleaned_data["passed"], form.cleaned_data["failed"]):
            messages.info(request, f"{b.number} had already been completed or cancelled.")
            return redirect(b)
        text = f"Build {b.number} completed: {b.completed_qty} passed, {b.failed_qty} failed"
        if b.yield_percent is not None:
            text += f" ({b.yield_percent}% yield)"
        if recalled.exists():
            text += " — completed although its firmware is recalled"
        log_activity(project, text[0].lower() + text[1:], actor=request.user, url=b.get_absolute_url())
        post_system_message(project, text + ".", b.get_absolute_url())
        if project.lead:
            notify(project.lead, text, b.get_absolute_url())
        messages.success(request, text + ".")
    elif action == "cancel" and b.status in BuildOrder.ACTIVE:
        cancelled, returned = b.cancel(request.user, return_parts=bool(request.POST.get("return_parts")))
        if not cancelled:
            messages.info(request, f"{b.number} had already been completed or cancelled.")
            return redirect(b)
        log_activity(project, f"cancelled build {b.number}", actor=request.user, url=b.get_absolute_url())
        msg = f"{b.number} cancelled."
        if returned:
            msg += " Put back into stock: " + ", ".join(f"{n} × {p.ipn}" for p, n in returned.items()) + "."
        messages.info(request, msg)
    elif action == "return_parts" and b.stock_consumed:
        quantities = {}
        for key, raw in request.POST.items():
            if key.startswith("return_") and key[7:].isdigit() and raw.strip().isdigit() and int(raw) > 0:
                quantities[int(key[7:])] = int(raw)
        returned = b.return_parts(request.user, quantities) if quantities else {}
        if returned:
            text = ", ".join(f"{n} × {p.ipn}" for p, n in returned.items())
            log_activity(project, f"returned unused parts of {b.number} to stock: {text}", actor=request.user, url=b.get_absolute_url())
            messages.success(request, f"Put back into stock: {text}.")
        else:
            messages.info(request, "Nothing returned — enter how many of each part are left over.")
    elif action == "set_firmware" and b.status in BuildOrder.ACTIVE:
        skipped = _set_build_firmware(request, b)
        if skipped is None:
            messages.info(request, f"{b.number} was finished or cancelled meanwhile, so its firmware wasn't changed.")
        else:
            if skipped:
                messages.warning(request, "Only released or testing firmware of this project can be used; the rest was left out.")
            messages.success(request, "Firmware for this build saved.")
    elif action == "reset_firmware" and b.status in BuildOrder.ACTIVE:
        n = b.copy_firmware_from_revision()
        if n is None:
            messages.info(request, f"{b.number} was finished or cancelled meanwhile, so its firmware wasn't changed.")
            return redirect(b)
        audit(request, "build.firmware", b, reset="from revision")
        log_activity(project, f"reset the firmware for build {b.number} from {b.revision.title}", actor=request.user, url=b.get_absolute_url())
        messages.success(request, f"Firmware copied from {b.revision.title}: {n} version{'s' if n != 1 else ''}.")
    elif action == "order_shortages" and b.status == S.PLANNED:
        created = _order_shortages(request, b)
        if created:
            messages.success(request, "Draft purchase orders updated: " + ", ".join(po.number for po in created) + ". Review and send them.")
            return redirect("production:po_list")
        messages.info(request, "Nothing more to order — what's short is already in stock or on order.")
    else:
        messages.info(request, "That can't be done at this stage of the build.")
        return redirect(b)
    audit(request, f"build.{action}", b)
    return redirect(b)


def _consumed_rows(b):
    out = b.consumed_parts()
    parts = Part.objects.in_bulk(list(out))
    return [{"part": parts[pid], "qty": n} for pid, n in sorted(out.items(), key=lambda t: parts[t[0]].ipn) if pid in parts]


def _demand(part_ids):
    """Parts still needed by every build that hasn't taken its parts yet: {part id: qty}."""
    need = defaultdict(int)
    for build in BuildOrder.objects.filter(status=BuildOrder.Status.PLANNED, stock_consumed=False).select_related("revision"):
        for line in build.stock_lines().filter(part_id__in=part_ids):
            need[line.part_id] += line.quantity * build.quantity
    return need


def _on_order(part_ids):
    """Parts on purchase orders not yet delivered (drafts included): {part id: qty}."""
    out = defaultdict(int)
    for line in PurchaseOrderLine.objects.filter(part_id__in=part_ids, order__status__in=PurchaseOrder.OPEN):
        out[line.part_id] += line.outstanding
    return out


def _order_shortages(request, build):
    """Add what's missing to draft orders, one per supplier.

    What's missing is counted across all planned builds (they all draw on the same
    stock) minus what's in stock and already on order — so two builds short of the
    same part order enough for both, and pressing the button twice orders nothing more."""
    short_rows = [r for r in build.requirements() if r["short"]]
    ids = [r["part"].pk for r in short_rows]
    demand, on_order = _demand(ids), _on_order(ids)
    by_supplier = defaultdict(list)
    missing_supplier = []
    for r in short_rows:
        part = r["part"]
        qty = max(demand.get(part.pk, r["need"]) - max(part.stock, 0) - on_order.get(part.pk, 0), 0)
        if not qty:
            continue
        if part.supplier_id:
            by_supplier[part.supplier].append((part, qty))
        else:
            missing_supplier.append(part.ipn)
    created = []
    with transaction.atomic():
        for supplier, rows in by_supplier.items():
            po = PurchaseOrder.objects.select_for_update().filter(supplier=supplier, status=PurchaseOrder.Status.DRAFT).first()
            if po is None:
                po = PurchaseOrder.objects.create(supplier=supplier, created_by=request.user,
                                                  notes=f"Shortages for build {build.number}")
            elif build.number not in po.notes:
                po.notes = (po.notes + "\n" if po.notes else "") + f"Shortages for build {build.number}"
                po.save(update_fields=["notes"])
            # Part costs are in the base currency; for a supplier quoting in another one the price is left for the buyer.
            same_currency = supplier.in_base_currency
            if not same_currency:
                messages.warning(request, f"{po.number}: {supplier} quotes in {supplier.currency} — enter the prices before sending it.")
            for part, qty in rows:
                line, made = po.lines.get_or_create(part=part, defaults={"quantity": qty,
                                                                         "unit_cost": part.unit_cost if same_currency else 0})
                if not made:
                    line.quantity += qty
                    line.save(update_fields=["quantity"])
            created.append(po)
    if missing_supplier:
        messages.warning(request, "These parts have no supplier set, so they weren't added to an order: " + ", ".join(missing_supplier))
    return created
