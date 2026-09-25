from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import F
from django.urls import reverse


class Supplier(models.Model):
    name = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=20, default="distributor", choices=[
        ("distributor", "Component distributor"),
        ("pcb", "PCB fabricator"),
        ("assembly", "Assembly house / contract manufacturer"),
        ("other", "Other"),
    ])
    website = models.URLField(blank=True)
    contact_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    country = models.CharField(max_length=60, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    lead_time_days = models.PositiveIntegerField(default=14, help_text="Typical days from order to delivery.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("inventory:supplier_edit", args=[self.pk])


class Part(models.Model):
    class Category(models.TextChoices):
        RESISTOR = "resistor", "Resistor"
        CAPACITOR = "capacitor", "Capacitor"
        INDUCTOR = "inductor", "Inductor"
        DIODE = "diode", "Diode / LED"
        TRANSISTOR = "transistor", "Transistor"
        IC = "ic", "IC / microcontroller"
        CONNECTOR = "connector", "Connector"
        MODULE = "module", "Module"
        PCB = "pcb", "Bare PCB"
        MECHANICAL = "mechanical", "Mechanical / hardware"
        OTHER = "other", "Other"

    class Lifecycle(models.TextChoices):
        ACTIVE = "active", "Active"
        NRND = "nrnd", "Not recommended for new designs"
        OBSOLETE = "obsolete", "Obsolete"

    ipn = models.CharField("Part number", max_length=40, unique=True,
                           help_text="Your internal part number, e.g. RES-0001. Leave blank to generate one.",
                           blank=True)
    description = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    value = models.CharField(max_length=60, blank=True, help_text="e.g. 10k, 100nF, 3.3V")
    footprint = models.CharField(max_length=120, blank=True, help_text="KiCad footprint, e.g. Resistor_SMD:R_0603_1608Metric")
    manufacturer = models.CharField(max_length=120, blank=True)
    mpn = models.CharField("Manufacturer part number", max_length=120, blank=True, db_index=True)
    datasheet_url = models.URLField(blank=True)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="parts")
    supplier_sku = models.CharField("Supplier SKU", max_length=120, blank=True)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    stock = models.IntegerField(default=0)
    min_stock = models.PositiveIntegerField("Reorder level", default=0,
                                            help_text="You'll see a warning when stock falls below this.")
    location = models.CharField(max_length=80, blank=True, help_text="Where it's stored, e.g. Shelf B / Bin 12")
    lifecycle = models.CharField(max_length=20, choices=Lifecycle.choices, default=Lifecycle.ACTIVE)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    PREFIX = {
        "resistor": "RES", "capacitor": "CAP", "inductor": "IND", "diode": "DIO", "transistor": "TRN",
        "ic": "IC", "connector": "CON", "module": "MOD", "pcb": "PCB", "mechanical": "MEC", "other": "OTH",
    }

    class Meta:
        ordering = ["ipn"]

    def __str__(self):
        return f"{self.ipn} · {self.description}"

    def get_absolute_url(self):
        return reverse("inventory:part_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.ipn:
            prefix = self.PREFIX.get(self.category, "OTH")
            n = Part.objects.filter(ipn__startswith=prefix + "-").count() + 1
            while Part.objects.filter(ipn=f"{prefix}-{n:04d}").exists():
                n += 1
            self.ipn = f"{prefix}-{n:04d}"
        super().save(*args, **kwargs)

    @property
    def is_low(self):
        return self.stock < self.min_stock

    @property
    def stock_value(self):
        return self.unit_cost * max(self.stock, 0)

    def adjust_stock(self, delta, reason, user=None, reference=""):
        with transaction.atomic():
            Part.objects.filter(pk=self.pk).update(stock=F("stock") + delta)
            self.refresh_from_db(fields=["stock"])
            StockMovement.objects.create(part=self, delta=delta, reason=reason, reference=reference,
                                         user=user, balance_after=self.stock)


class StockMovement(models.Model):
    class Reason(models.TextChoices):
        RECEIVED = "received", "Received from purchase order"
        BUILD = "build", "Used in a build"
        ADJUST = "adjust", "Manual adjustment / stock count"
        RETURN = "return", "Returned"

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="movements")
    delta = models.IntegerField()
    balance_after = models.IntegerField(default=0)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    reference = models.CharField(max_length=120, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class BomLine(models.Model):
    """One line of a revision's bill of materials."""

    revision = models.ForeignKey("projects.Revision", on_delete=models.CASCADE, related_name="bom_lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="bom_lines")
    references = models.TextField(blank=True, help_text="Schematic reference designators, e.g. R1, R2, R5")
    quantity = models.PositiveIntegerField(default=1, help_text="How many are used on one board.")
    dnp = models.BooleanField("Do not populate", default=False)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["part__ipn"]
        unique_together = [("revision", "part")]

    @property
    def line_cost(self):
        return Decimal(0) if self.dnp else self.part.unit_cost * self.quantity
