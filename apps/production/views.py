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
from apps.inventory.models import Part, Supplier
from apps.projects.models import Project, Revision, log_activity

from .models import BuildOrder, PurchaseOrder, PurchaseOrderLine


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
        help_texts = {"unit_cost": "Leave as 0 to use the part's current cost."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit_cost"].required = False


class BuildForm(forms.ModelForm):
    class Meta:
        model = BuildOrder
        fields = ["revision", "quantity", "assembly", "manufacturer", "due_date", "serial_prefix", "firmware_releases", "notes"]
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
        from apps.firmware.models import FirmwareRelease
        self.fields["firmware_releases"].queryset = FirmwareRelease.objects.filter(
            firmware__project__in=Project.objects.visible_to(user), status__in=["released", "testing"]
        ).select_related("firmware__project").order_by("firmware__project__key", "firmware__name", "-sort_key")
        self.fields["firmware_releases"].label_from_instance = lambda r: f"{r.firmware.project.key} · {r.firmware.name} {r.version}" + (" (testing)" if r.status == "testing" else "")
        self.fields["firmware_releases"].widget = forms.CheckboxSelectMultiple(choices=self.fields["firmware_releases"].choices)

    def clean(self):
        data = super().clean()
        rev = data.get("revision")
        for r in data.get("firmware_releases") or []:
            if rev and r.firmware.project_id != rev.project_id:
                self.add_error("firmware_releases", f"{r} belongs to a different project.")
        return data


class CompleteForm(forms.Form):
    passed = forms.IntegerField(min_value=0, label="Boards that passed test")
    failed = forms.IntegerField(min_value=0, initial=0, label="Boards that failed test")


# --- Purchase orders ---------------------------------------------------------

@login_required
def po_list(request):
    status = request.GET.get("status", "open")
    qs = PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines")
    if status == "open":
        qs = qs.filter(status__in=["draft", "ordered", "partial"])
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
    line_form = POLineForm(request.POST or None) if po.is_editable else None
    if request.method == "POST" and line_form is not None:
        if not can_edit:
            raise PermissionDenied
        if line_form.is_valid():
            line = line_form.save(commit=False)
            line.order = po
            if not line.unit_cost:
                line.unit_cost = line.part.unit_cost
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
    })


@procurement_required
@require_POST
def po_action(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    action = request.POST.get("action")
    if request.POST.get("remove_line") and po.is_editable:
        action = "remove_line"
        po.lines.filter(pk=request.POST["remove_line"]).delete()
    elif action == "order" and po.is_editable:
        if not po.lines.exists():
            messages.error(request, "Add at least one part before marking the order as sent.")
            return redirect(po)
        po.status = PurchaseOrder.Status.ORDERED
        po.ordered_at = timezone.now()
        po.save()
        audit(request, "po.ordered", po, total=po.total)
        messages.success(request, f"{po.number} marked as ordered. Record deliveries here when they arrive.")
    elif action == "receive" and po.status in (PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIAL):
        received = 0
        with transaction.atomic():
            for line in po.lines.select_related("part"):
                raw = request.POST.get(f"recv_{line.pk}", "").strip()
                if raw.isdigit() and int(raw) > 0:
                    received += line.receive(int(raw), request.user)
            po.refresh_status()
        audit(request, "po.received", po, items=received)
        messages.success(request, f"Received {received} items into stock." if received else "Nothing received — enter quantities first.")
    elif action == "receive_all" and po.status in (PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIAL):
        with transaction.atomic():
            n = sum(line.receive(line.outstanding, request.user) for line in po.lines.select_related("part"))
            po.refresh_status()
        audit(request, "po.received", po, items=n)
        messages.success(request, f"Received everything outstanding ({n} items) into stock.")
    elif action == "cancel" and po.status in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.ORDERED):
        po.status = PurchaseOrder.Status.CANCELLED
        po.save(update_fields=["status"])
        audit(request, "po.cancelled", po)
        messages.info(request, f"{po.number} cancelled.")
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
        b.save()
        log_activity(b.revision.project, f"planned build {b.number}: {b.quantity} × {b.revision.title}", actor=request.user, url=b.get_absolute_url())
        audit(request, "build.created", b)
        if not b.revision.bom_lines.exists():
            messages.warning(request, f"{b.revision} has no BOM yet, so Workbench can't check parts. Import the BOM first.")
        return redirect(b)
    return render(request, "production/build_form.html", {"form": form})


@login_required
def build_detail(request, pk):
    b = get_object_or_404(BuildOrder.objects.select_related("revision__project", "manufacturer"), pk=pk)
    if not b.revision.project.can_view(request.user):
        raise PermissionDenied
    reqs = b.requirements()
    firmware = []
    for r in b.firmware_releases.select_related("firmware").prefetch_related("artifacts", "revisions"):
        issues = []
        if r.status == "recalled":
            issues.append(("bad", "RECALLED — do not flash"))
        elif r.status != "released":
            issues.append(("warn", f"{r.get_status_display()} — not released yet"))
        if not r.revisions.filter(pk=b.revision_id).exists():
            issues.append(("warn", f"not marked compatible with {b.revision.title}"))
        newer = r.firmware.recommended_for(b.revision)
        if newer and newer.sort_key > r.sort_key:
            issues.append(("warn", f"newer release {newer.version} is available"))
        firmware.append({"rel": r, "issues": issues})
    from apps.firmware.models import FirmwareRelease
    fw_choices = list(FirmwareRelease.objects.filter(firmware__project=b.revision.project, status__in=["released", "testing"])
                      .select_related("firmware").order_by("firmware__name", "-sort_key"))
    recommended = {fw.pk: fw.recommended_for(b.revision) for fw in {r.firmware for r in fw_choices}}
    for r in fw_choices:
        r.recommended = recommended.get(r.firmware_id) == r
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
        "fab_form": FabOrderForm(initial={"fab_ordered_at": timezone.localdate()}),
        "receive_form": ReceiveForm(initial={"received_qty": b.quantity}),
        "can_edit": request.user.can_manage_procurement,
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
    if action == "start" and b.status == BuildOrder.Status.PLANNED and b.firmware_releases.filter(status="recalled").exists():
        messages.error(request, "This build uses recalled firmware. Choose a different firmware version before starting.")
        return redirect(b)
    if action == "start" and b.status == BuildOrder.Status.PLANNED:
        if b.shortage_count and "force" not in request.POST:
            messages.error(request, "Some parts are short. Order them first, or tick “start anyway”.")
            return redirect(b)
        b.start()
        b.consume_stock(request.user)
        b.create_steps()
        b.stage = BuildOrder.Stage.AT_FAB if b.by_fab else BuildOrder.Stage.FINISHING
        b.save(update_fields=["stage"])
        log_activity(project, f"started build {b.number}", actor=request.user, url=b.get_absolute_url())
        post_system_message(project, f"Build {b.number} ({b.quantity} × {b.revision.title}) has started. Parts have been taken from stock.", b.get_absolute_url())
        messages.success(request, f"{b.number} started. Parts for {b.quantity} boards were taken out of stock.")
    elif action == "complete" and b.status in (BuildOrder.Status.PLANNED, BuildOrder.Status.IN_PROGRESS):
        form = CompleteForm(request.POST)
        if form.is_valid():
            b.complete(request.user, form.cleaned_data["passed"], form.cleaned_data["failed"])
            b.stage = BuildOrder.Stage.DONE
            b.save(update_fields=["stage"])
            text = f"Build {b.number} completed: {b.completed_qty} passed, {b.failed_qty} failed"
            if b.yield_percent is not None:
                text += f" ({b.yield_percent}% yield)"
            log_activity(project, text[0].lower() + text[1:], actor=request.user, url=b.get_absolute_url())
            post_system_message(project, text + ".", b.get_absolute_url())
            if project.lead:
                notify(project.lead, text, b.get_absolute_url())
            messages.success(request, text + ".")
    elif action == "cancel" and b.status == BuildOrder.Status.PLANNED:
        b.status = BuildOrder.Status.CANCELLED
        b.save(update_fields=["status"])
        messages.info(request, f"{b.number} cancelled.")
    elif action == "set_firmware" and b.status in (BuildOrder.Status.PLANNED, BuildOrder.Status.IN_PROGRESS):
        from apps.firmware.models import FirmwareRelease
        chosen = FirmwareRelease.objects.filter(pk__in=request.POST.getlist("firmware"), firmware__project=project)
        b.firmware_releases.set(chosen)
        log_activity(project, f"set firmware for build {b.number}: " + (", ".join(str(r) for r in chosen) or "none"), actor=request.user, url=b.get_absolute_url())
        messages.success(request, "Firmware for this build saved.")
    elif action == "order_shortages":
        created = _order_shortages(request, b)
        if created:
            messages.success(request, "Draft purchase orders created: " + ", ".join(po.number for po in created) + ". Review and send them.")
            return redirect("production:po_list")
        messages.info(request, "Nothing is short — no orders needed.")
    audit(request, f"build.{action}", b)
    return redirect(b)


def _order_shortages(request, build):
    by_supplier = defaultdict(list)
    missing_supplier = []
    for r in build.requirements():
        if r["short"]:
            if r["part"].supplier_id:
                by_supplier[r["part"].supplier].append(r)
            else:
                missing_supplier.append(r["part"].ipn)
    created = []
    with transaction.atomic():
        for supplier, rows in by_supplier.items():
            po = PurchaseOrder.objects.filter(supplier=supplier, status=PurchaseOrder.Status.DRAFT).first()
            if po is None:
                po = PurchaseOrder.objects.create(supplier=supplier, created_by=request.user,
                                                  notes=f"Shortages for build {build.number}")
            for r in rows:
                line, made = po.lines.get_or_create(part=r["part"], defaults={"quantity": r["short"], "unit_cost": r["part"].unit_cost})
                if not made:
                    line.quantity = max(line.quantity, r["short"])
                    line.save()
            created.append(po)
    if missing_supplier:
        messages.warning(request, "These parts have no supplier set, so they weren't added to an order: " + ", ".join(missing_supplier))
    return created
