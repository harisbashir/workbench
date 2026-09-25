from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
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
        CANCELLED = "cancelled", "Cancelled"

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

    def refresh_status(self):
        lines = list(self.lines.all())
        if not lines or self.status in (self.Status.DRAFT, self.Status.CANCELLED):
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
        qty = min(qty, self.outstanding)
        if qty <= 0:
            return 0
        with transaction.atomic():
            self.received_qty += qty
            self.save(update_fields=["received_qty"])
            self.part.adjust_stock(qty, StockMovement.Reason.RECEIVED, user=user, reference=self.order.number)
            if self.unit_cost:
                Part.objects.filter(pk=self.part_id).update(unit_cost=self.unit_cost)
        return qty


class BuildOrder(models.Model):
    """A production run of N boards of one revision."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=20, unique=True, editable=False)
    revision = models.ForeignKey("projects.Revision", on_delete=models.PROTECT, related_name="builds")
    quantity = models.PositiveIntegerField(help_text="Number of boards to build.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    manufacturer = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL,
                                     help_text="Assembly house, or leave blank if built in-house.")
    due_date = models.DateField(null=True, blank=True)
    completed_qty = models.PositiveIntegerField(default=0, help_text="Boards that passed test.")
    failed_qty = models.PositiveIntegerField(default=0, help_text="Boards that failed test.")
    serial_prefix = models.CharField(max_length=20, blank=True, help_text="Optional, e.g. PWRB-2026-")
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

    def requirements(self):
        """Parts needed for this build vs. what's in stock."""
        rows = []
        for line in self.revision.bom_lines.select_related("part").filter(dnp=False):
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

    def start(self):
        self.status = self.Status.IN_PROGRESS
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def consume_stock(self, user):
        if self.stock_consumed:
            return
        with transaction.atomic():
            for row in self.requirements():
                row["part"].adjust_stock(-row["need"], StockMovement.Reason.BUILD, user=user, reference=self.number)
            self.stock_consumed = True
            self.save(update_fields=["stock_consumed"])

    def complete(self, user, passed, failed):
        self.consume_stock(user)
        self.completed_qty = passed
        self.failed_qty = failed
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save()

    @property
    def yield_percent(self):
        tested = self.completed_qty + self.failed_qty
        return round(100 * self.completed_qty / tested) if tested else None
