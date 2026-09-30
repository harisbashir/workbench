from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import Part, StockMovement, Supplier


def _next_number(model, prefix):
    last = model.objects.filter(number__startswith=prefix).order_by("-id").first()
    n = int(last.number.split("-")[1]) + 1 if last else 1
    while model.objects.filter(number=f"{prefix}-{n:04d}").exists():
        n += 1
    return f"{prefix}-{n:04d}"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ORDERED = "ordered", "Ordered"
        PARTIAL = "partial", "Partly received"
        RECEIVED = "received", "Received"
        CLOSED = "closed", "Closed short"
        CANCELLED = "cancelled", "Cancelled"

    OPEN = ("draft", "ordered", "partial")

    number = models.CharField(max_length=20, unique=True, editable=False)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    reference = models.CharField(max_length=120, blank=True, help_text="Supplier's order or quote number.")
    expected_date = models.DateField(null=True, blank=True)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    ordered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} · {self.supplier}"

    def get_absolute_url(self):
        return reverse("production:po_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = _next_number(PurchaseOrder, "PO")
        super().save(*args, **kwargs)

    @property
    def subtotal(self):
        return sum((line.line_total for line in self.lines.all()), Decimal(0))

    @property
    def total(self):
        return self.subtotal + self.shipping_cost

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    @property
    def currency(self):
        return self.supplier.currency or "USD"

    def refresh_status(self):
        lines = list(self.lines.all())
        if not lines or self.status in (self.Status.DRAFT, self.Status.CANCELLED, self.Status.CLOSED):
            return
        if all(line.received_qty >= line.quantity for line in lines):
            self.status = self.Status.RECEIVED
        elif any(line.received_qty for line in lines):
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.ORDERED
        self.save(update_fields=["status"])


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4)
    received_qty = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["part__ipn"]

    @property
    def line_total(self):
        return self.unit_cost * self.quantity

    @property
    def outstanding(self):
        return max(self.quantity - self.received_qty, 0)

    def receive(self, qty, user):
        """Book qty into stock (capped at what's outstanding). Returns what was received.

        The line is re-read under a row lock, so two people receiving the same order at
        the same time can't both book the same parts."""
        with transaction.atomic():
            line = PurchaseOrderLine.objects.select_for_update().select_related("order__supplier").get(pk=self.pk)
            qty = min(qty, line.outstanding)
            if qty <= 0:
                self.received_qty = line.received_qty
                return 0
            line.received_qty += qty
            line.save(update_fields=["received_qty"])
            self.received_qty = line.received_qty
            self.part.adjust_stock(qty, StockMovement.Reason.RECEIVED, user=user, reference=line.order.number)
            # Part costs are kept in the base currency: a price in another currency isn't copied over.
            if line.unit_cost and line.order.supplier.in_base_currency:
                Part.objects.filter(pk=self.part_id).update(unit_cost=line.unit_cost)
        return qty


class BuildOrder(models.Model):
    """A production run of N boards of one revision."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Assembly(models.TextChoices):
        FAB = "fab", "Assembled by JLCPCB, Seeed or another assembly house, then finished in house"
        HOUSE = "house", "Assembled completely in house"

    class Stage(models.TextChoices):
        PLANNED = "planned", "Planned"
        AT_FAB = "at_fab", "At the assembly house"
        FINISHING = "finishing", "Finishing in house"
        TESTING = "testing", "Test & flash"
        DONE = "done", "Done"

    number = models.CharField(max_length=20, unique=True, editable=False)
    revision = models.ForeignKey("projects.Revision", on_delete=models.PROTECT, related_name="builds")
    assembly = models.CharField("How are the boards assembled?", max_length=8, choices=Assembly.choices, default=Assembly.FAB)
    stage = models.CharField(max_length=12, choices=Stage.choices, default=Stage.PLANNED)
    fab_order_ref = models.CharField("Assembly order number", max_length=80, blank=True,
                                     help_text="e.g. the JLCPCB order number (SO…), for tracking and re-orders.")
    fab_tracking = models.CharField("Shipping tracking", max_length=120, blank=True)
    fab_ordered_at = models.DateField(null=True, blank=True)
    received_qty = models.PositiveIntegerField(null=True, blank=True, help_text="Boards received from the assembly house.")
    received_at = models.DateTimeField(null=True, blank=True)
    quantity = models.PositiveIntegerField(help_text="Number of boards to build.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    manufacturer = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL,
                                     help_text="Assembly house, or leave blank if built in-house.")
    due_date = models.DateField(null=True, blank=True)
    completed_qty = models.PositiveIntegerField(default=0, help_text="Boards that passed test.")
    failed_qty = models.PositiveIntegerField(default=0, help_text="Boards that failed test.")
    serial_prefix = models.CharField(max_length=20, blank=True, help_text="Optional, e.g. PWRB-2026-")
    firmware_releases = models.ManyToManyField(
        "firmware.FirmwareRelease", blank=True, related_name="builds", verbose_name="Firmware to flash",
        help_text="Which firmware versions go on these boards. Recorded so every build is traceable.")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    stock_consumed = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} · {self.revision} × {self.quantity}"

    def get_absolute_url(self):
        return reverse("production:build_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = _next_number(BuildOrder, "BO")
        super().save(*args, **kwargs)

    @property
    def by_fab(self):
        return self.assembly == self.Assembly.FAB

    def stock_lines(self):
        """BOM lines whose parts come out of our own stock: all of them for an in-house
        build; only the ones fitted in house when an assembly house does the rest."""
        lines = self.revision.bom_lines.select_related("part").filter(dnp=False)
        return lines.filter(fitted_by="house") if self.by_fab else lines

    @property
    def board_qty(self):
        """Boards to finish: what came back from the fab if known, else what was ordered."""
        return self.received_qty if self.received_qty is not None else self.quantity

    def create_steps(self):
        """Finishing checklist from the BOM (in-house parts), unless it exists already."""
        if self.steps.exists():
            return
        for i, line in enumerate(self.stock_lines().order_by("part__category", "part__ipn")):
            if line.part.category == "pcb":
                continue
            desc = line.part.description
            if line.part.value and line.part.value.lower() not in desc.lower():
                desc = f"{line.part.value} — {desc}"
            BuildStep.objects.create(build=self, bom_line=line, order=i, per_board=line.quantity,
                                     title=f"Fit {line.references or line.part.ipn}", detail=desc[:200])

    @property
    def steps_done(self):
        steps = list(self.steps.all())
        return bool(steps) and all(s.done_qty >= self.board_qty for s in steps)

    def requirements(self):
        """Parts needed for this build vs. what's in stock."""
        rows = []
        for line in self.stock_lines():
            need = line.quantity * self.quantity
            have = line.part.stock
            rows.append({
                "line": line, "part": line.part, "need": need, "have": have,
                "short": max(need - have, 0) if not self.stock_consumed else 0,
            })
        return rows

    @property
    def shortage_count(self):
        return sum(1 for r in self.requirements() if r["short"])

    # --- State changes -------------------------------------------------------------
    # Each one is a conditional UPDATE ("claim"): if two people press the same button at
    # the same time (or someone double-clicks), exactly one request wins and the other
    # does nothing, so parts can't be taken from stock twice.

    ACTIVE = ("planned", "in_progress")

    def _claim(self, where, **changes):
        n = BuildOrder.objects.filter(pk=self.pk, **where).update(**changes)
        if n:
            for k, v in changes.items():
                setattr(self, k, v)
        return bool(n)

    def begin(self, stage, **extra):
        """Planned → in progress (at `stage`). Returns False if it had already started or was cancelled."""
        return self._claim({"status": self.Status.PLANNED}, status=self.Status.IN_PROGRESS, stage=stage,
                           started_at=timezone.now(), **extra)

    def start(self):
        return self.begin(self.Stage.AT_FAB if self.by_fab else self.Stage.FINISHING)

    def consume_stock(self, user):
        """Take the parts for this build out of stock, once. Returns True if it did."""
        with transaction.atomic():
            rows = self.requirements()
            if not self._claim({"stock_consumed": False}, stock_consumed=True):
                self.stock_consumed = True
                return False
            for row in rows:
                if row["need"]:
                    row["part"].adjust_stock(-row["need"], StockMovement.Reason.BUILD, user=user, reference=self.number)
        return True

    def complete(self, user, passed, failed):
        """Planned/in progress → completed. Returns False if it was already completed or cancelled."""
        with transaction.atomic():
            if not self._claim({"status__in": self.ACTIVE}, status=self.Status.COMPLETED, stage=self.Stage.DONE,
                               completed_qty=passed, failed_qty=failed, completed_at=timezone.now()):
                return False
            self.consume_stock(user)
        return True

    def consumed_parts(self):
        """{part id: quantity} taken from stock for this build and not yet returned."""
        rows = (StockMovement.objects.filter(reference=self.number,
                                             reason__in=[StockMovement.Reason.BUILD, StockMovement.Reason.RETURN])
                .values("part_id").annotate(net=Sum("delta")))
        return {r["part_id"]: -r["net"] for r in rows if r["net"] < 0}

    def return_parts(self, user, quantities=None, note=""):
        """Put parts taken for this build back into stock: `quantities` ({part id: qty}),
        or everything still out. Never returns more than was taken. Returns {part: qty}."""
        returned = {}
        with transaction.atomic():
            list(BuildOrder.objects.select_for_update().filter(pk=self.pk))  # one return at a time per build
            out = self.consumed_parts()
            wanted = out if quantities is None else quantities
            parts = Part.objects.in_bulk([pid for pid in wanted if pid in out])
            for pid, qty in wanted.items():
                qty = min(int(qty), out.get(pid, 0))
                if qty > 0 and pid in parts:
                    parts[pid].adjust_stock(qty, StockMovement.Reason.RETURN, user=user, reference=self.number)
                    returned[parts[pid]] = qty
        return returned

    def return_for_missing_boards(self, user, missing):
        """Fewer boards came back than were ordered: return the in-house parts for the missing ones."""
        if missing <= 0 or not self.stock_consumed:
            return {}
        per_part = {}
        for line in self.stock_lines():
            per_part[line.part_id] = per_part.get(line.part_id, 0) + line.quantity * missing
        return self.return_parts(user, per_part)

    def cancel(self, user, return_parts=True):
        """Planned/in progress → cancelled; optionally put the parts taken for it back.
        Returns (cancelled?, {part: qty returned})."""
        with transaction.atomic():
            if not self._claim({"status__in": self.ACTIVE}, status=self.Status.CANCELLED):
                return False, {}
            returned = self.return_parts(user) if (return_parts and self.stock_consumed) else {}
        return True, returned

    @property
    def yield_percent(self):
        tested = self.completed_qty + self.failed_qty
        return round(100 * self.completed_qty / tested) if tested else None


class BuildStep(models.Model):
    """One job in finishing a build: fit a part (from the BOM) or another task (wash, coat, label…).

    Progress is counted in boards, so a technician can tap +1 / +5 as they go.
    """

    build = models.ForeignKey(BuildOrder, on_delete=models.CASCADE, related_name="steps")
    bom_line = models.ForeignKey("inventory.BomLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    title = models.CharField(max_length=160)
    detail = models.CharField(max_length=200, blank=True)
    per_board = models.PositiveIntegerField(default=1, help_text="Parts per board")
    order = models.PositiveIntegerField(default=0)
    done_qty = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title

    @property
    def total(self):
        return self.build.board_qty

    @property
    def percent(self):
        return min(100, round(100 * self.done_qty / self.total)) if self.total else 0

    @property
    def complete(self):
        return self.done_qty >= self.total
