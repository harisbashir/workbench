"""Design files of a board revision: Gerbers, drill files, schematic, layout, CPL…

Uploading a file with the same name again adds a new version; older versions
are kept. Once a revision is released for production its files are locked.
"""
import hashlib
import os
import re

from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.cad.models import MeshPreview
from apps.projects.models import Revision


class Category(models.TextChoices):
    GERBER = "gerber", "Gerbers"
    DRILL = "drill", "Drill files"
    SCHEMATIC = "schematic", "Schematic"
    PCB = "pcb", "PCB layout"
    PROJECT = "project", "Design project"
    BOM = "bom", "BOM"
    PNP = "pnp", "Pick and place (CPL)"
    ASSEMBLY = "assembly", "Assembly drawings"
    MODEL = "model", "3D model"
    FAB = "fab", "Fabrication notes"
    OTHER = "other", "Other"


CATEGORY_HELP = {
    "gerber": "Gerber zip (as sent to the fab) or individual Gerber files",
    "drill": "Excellon .drl files, if not inside the Gerber zip",
    "schematic": "PDF export, and/or .kicad_sch / .sch files",
    "pcb": ".kicad_pcb / .brd layout files",
    "pnp": "Component positions for assembly (KiCad .pos / CSV)",
    "bom": "BOM export as sent to the assembler",
    "model": "3D model of the assembled board from KiCad: STEP, VRML (.wrl) or glTF (.glb)",
}

GERBER_EXT = {"gbr", "ger", "pho", "art", "gtl", "gbl", "gts", "gbs", "gto", "gbo", "gtp", "gbp", "gko", "gm1", "gml",
              "cmp", "sol", "stc", "sts", "plc", "pls", "crc", "crs", "gbrjob", "g1", "g2", "g3", "g4", "gp1", "gp2", "mil"}
DRILL_EXT = {"drl", "xln", "exc", "drd", "nc"}


def guess_category(name, head=b""):
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    if ext == "zip":
        return Category.GERBER if re.search(rb"\.(gbr|gtl|gbl|drl|gko|gm1|cmp|sol|xln|g\d|gbrjob)", head, re.I) or \
            re.search(r"gerber|fab|jlc|pcbway|seeed", low) else Category.OTHER
    if ext in GERBER_EXT or re.fullmatch(r"g\d+l?", ext):
        return Category.GERBER
    if ext in DRILL_EXT:
        return Category.DRILL
    if ext in ("kicad_sch", "sch", "schdoc", "dsn"):
        return Category.SCHEMATIC
    if ext in ("kicad_pcb", "brd", "pcbdoc"):
        return Category.PCB
    if ext in ("kicad_pro", "kicad_prl", "pro", "prjpcb", "lbr", "kicad_sym", "kicad_mod", "kicad_dru"):
        return Category.PROJECT
    if ext in ("step", "stp", "wrl", "vrml", "igs", "iges", "stl", "glb", "gltf", "3mf", "obj"):
        return Category.MODEL
    if ext == "pos" or re.search(r"(^|[-_ .])(pos|cpl|pnp|pick|placement|centroid|xy)([-_ .]|$)", low.rsplit(".", 1)[0]):
        return Category.PNP
    if re.search(r"bom", low) and ext in ("csv", "xlsx", "xls", "txt", "tsv"):
        return Category.BOM
    if ext == "pdf":
        if re.search(r"sch", low):
            return Category.SCHEMATIC
        if re.search(r"assembl|asm|fab|placement", low):
            return Category.ASSEMBLY
        return Category.FAB
    if ext == "txt" and re.search(rb"(^|\n)\s*M48", head):
        return Category.DRILL
    return Category.OTHER


def design_path(instance, filename):
    rev = instance.revision
    return f"design/{rev.project.key}/rev{rev.pk}/v{instance.version}-{filename}"


class DesignFileQuerySet(models.QuerySet):
    def current(self):
        return self.filter(is_current=True)


class DesignFile(MeshPreview):
    MESH_KIND = "design"

    revision = models.ForeignKey(Revision, on_delete=models.CASCADE, related_name="design_files")
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    file = models.FileField(upload_to=design_path, max_length=400)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    note = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = DesignFileQuerySet.as_manager()

    class Meta:
        ordering = ["category", "name", "-version"]
        indexes = [models.Index(fields=["revision", "name", "is_current"])]

    def __str__(self):
        return f"{self.revision} {self.name} v{self.version}"

    @property
    def ext(self):
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def is_gerber_zip(self):
        return self.category == Category.GERBER and self.ext == "zip"

    @property
    def viewer(self):
        """How the file can be looked at in Workbench: 'pcb', '3d', 'pdf', 'image', 'text', 'csv' or ''."""
        if self.is_3d:
            return "3d"
        if self.category in (Category.GERBER, Category.DRILL) and (self.ext == "zip" or self.ext not in ("pdf", "txt")):
            return "pcb"
        if self.ext == "pdf":
            return "pdf"
        if self.ext in ("png", "jpg", "jpeg", "gif", "webp"):
            return "image"
        if self.ext in ("csv", "pos", "tsv"):
            return "csv"
        if self.ext in ("txt", "md", "kicad_sch", "kicad_pcb", "kicad_pro", "sch", "rpt", "net", "json", "kicad_dru"):
            return "text"
        return ""

    def get_absolute_url(self):
        return reverse("design:file", args=[self.revision.project.key, self.revision.pk, self.pk])

    @classmethod
    def store(cls, revision, uploaded, user, category=None, note="", background=True):
        from apps.files.models import safe_name
        name = safe_name(uploaded.name)
        h = hashlib.sha256()
        for chunk in uploaded.chunks():
            h.update(chunk)
        uploaded.seek(0)
        head = uploaded.read(65536)
        uploaded.seek(0)
        previous = cls.objects.filter(revision=revision, name=name).order_by("-version").first()
        if previous and previous.sha256 == h.hexdigest() and previous.is_current:
            return previous, False  # identical file uploaded again
        obj = cls(revision=revision, name=name, category=category or (previous.category if previous else guess_category(name, head)),
                  version=(previous.version + 1) if previous else 1, size=uploaded.size, sha256=h.hexdigest(),
                  uploaded_by=user, note=note[:200])
        obj.file.save(name, uploaded, save=False)
        obj.save()
        cls.objects.filter(revision=revision, name=name).exclude(pk=obj.pk).update(is_current=False)
        from apps.cad import jobs
        jobs.queue(obj, background=background)
        return obj, True


def split_name(name):
    base, ext = os.path.splitext(name)
    return base, ext.lower()
