"""Enclosures, casings and other mechanical parts of a product, with their CAD files.

Each part keeps every version of its files (STEP, STL, 3MF, SolidWorks, Fusion 360,
drawings…). Viewable 3D formats get a preview in the built-in 3D viewer. When a part
is released, its files are locked; change the revision letter to work on the next one.
"""
import hashlib

from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.cad import formats
from apps.cad.models import MeshPreview
from apps.projects.models import Board, Project


class MechanicalPart(models.Model):
    class Kind(models.TextChoices):
        ENCLOSURE = "enclosure", "Enclosure / housing"
        COVER = "cover", "Lid / cover"
        BRACKET = "bracket", "Bracket / mount"
        PANEL = "panel", "Front panel / bezel"
        BUTTON = "button", "Button / knob / keycap"
        LIGHTPIPE = "lightpipe", "Light pipe / window"
        SEAL = "seal", "Gasket / seal"
        HEATSINK = "heatsink", "Heatsink / thermal"
        STANDOFF = "standoff", "Standoff / spacer"
        OTHER = "other", "Other"

    class Process(models.TextChoices):
        FDM = "fdm", "3D print (FDM)"
        SLA = "sla", "3D print (resin / SLA)"
        SLS = "sls", "3D print (SLS / MJF)"
        MOULD = "mould", "Injection moulding"
        CNC = "cnc", "CNC machining"
        SHEET = "sheet", "Sheet metal"
        LASER = "laser", "Laser / waterjet cut"
        EXTRUSION = "extrusion", "Extrusion"
        CAST = "cast", "Casting"
        OFF_SHELF = "cots", "Bought in (off the shelf)"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        CONCEPT = "concept", "Concept"
        PROTOTYPE = "prototype", "Prototype"
        RELEASED = "released", "Released for production"
        OBSOLETE = "obsolete", "Obsolete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="mechanical_parts")
    name = models.CharField(max_length=120, help_text="e.g. Main enclosure, Top cover, Battery bracket")
    part_number = models.CharField(max_length=60, blank=True, help_text="Optional drawing or part number, e.g. MEC-1002")
    revision = models.CharField(max_length=12, default="A", help_text="Revision letter or number, e.g. A, B, 02")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ENCLOSURE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONCEPT)
    process = models.CharField("Manufacturing process", max_length=20, choices=Process.choices, blank=True)
    material = models.CharField(max_length=80, blank=True, help_text="e.g. PETG, ABS, PC-ABS, Aluminium 6061")
    finish = models.CharField("Colour / finish", max_length=80, blank=True, help_text="e.g. Matte black, anodised, bead-blasted")
    quantity = models.PositiveIntegerField("Quantity per product", default=1)
    supplier = models.CharField(max_length=120, blank=True, help_text="Who makes it, e.g. JLC3DP, PCBWay, in house")
    boards = models.ManyToManyField(Board, blank=True, related_name="housings",
                                    help_text="The boards this part holds or mounts.")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "kind", "name"]
        unique_together = [("project", "name")]

    def __str__(self):
        return f"{self.project.key} {self.name}"

    def get_absolute_url(self):
        return reverse("mechanical:detail", args=[self.project.key, self.pk])

    @property
    def is_locked(self):
        return self.status == self.Status.RELEASED

    def current_files(self):
        return self.files.filter(is_current=True)

    @property
    def preview_file(self):
        """The file shown in the 3D viewer: the newest current file that can be viewed."""
        for f in self.files.filter(is_current=True).exclude(mesh_status__in=["", "native"]).order_by("-uploaded_at"):
            if f.mesh_status != "failed":
                return f
        return None


class FileKind(models.TextChoices):
    MODEL = "model", "3D model"
    SOURCE = "source", "CAD source"
    DRAWING = "drawing", "Drawing"
    PRINT = "print", "Print / manufacturing file"
    OTHER = "other", "Other"


def guess_kind(name):
    e = formats.ext_of(name)
    if e in ("stl", "3mf", "obj", "step", "stp", "iges", "igs", "glb", "gltf", "wrl", "vrml"):
        return FileKind.MODEL
    if e in formats.NATIVE and e not in ("dxf", "dwg"):
        return FileKind.SOURCE
    if e in ("pdf", "dxf", "dwg", "svg", "png", "jpg", "jpeg"):
        return FileKind.DRAWING
    if e in ("gcode", "bgcode", "ctb", "sl1", "form", "nc", "tap"):
        return FileKind.PRINT
    return FileKind.OTHER


def mech_path(instance, filename):
    return f"mechanical/{instance.part.project.key}/part{instance.part_id}/v{instance.version}-{filename}"


class MechanicalFile(MeshPreview):
    MESH_KIND = "mech"

    part = models.ForeignKey(MechanicalPart, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=10, choices=FileKind.choices, default=FileKind.OTHER)
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    file = models.FileField(upload_to=mech_path, max_length=400)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    note = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name", "-version"]
        indexes = [models.Index(fields=["part", "name", "is_current"])]

    def __str__(self):
        return f"{self.part} {self.name} v{self.version}"

    @property
    def ext(self):
        return formats.ext_of(self.name)

    def get_absolute_url(self):
        return reverse("mechanical:file", args=[self.part.project.key, self.part_id, self.pk])

    @classmethod
    def store(cls, part, uploaded, user, note="", background=True):
        from apps.cad import jobs
        from apps.files.models import safe_name
        name = safe_name(uploaded.name)
        h = hashlib.sha256()
        for chunk in uploaded.chunks():
            h.update(chunk)
        uploaded.seek(0)
        previous = cls.objects.filter(part=part, name=name).order_by("-version").first()
        if previous and previous.is_current and previous.sha256 == h.hexdigest():
            return previous, False
        obj = cls(part=part, name=name, kind=previous.kind if previous else guess_kind(name),
                  version=(previous.version + 1) if previous else 1, size=uploaded.size, sha256=h.hexdigest(),
                  uploaded_by=user, note=note[:200])
        obj.file.save(name, uploaded, save=False)
        obj.save()
        cls.objects.filter(part=part, name=name).exclude(pk=obj.pk).update(is_current=False)
        jobs.queue(obj, background=background)
        return obj, True
