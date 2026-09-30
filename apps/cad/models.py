"""3D preview fields shared by every kind of stored CAD file (board models, mechanical parts)."""
from django.db import models
from django.urls import reverse

from . import formats


def mesh_path(instance, filename):
    ext = "png" if filename.endswith(".png") else "wbm.gz"
    return f"cad-previews/{instance.sha256[:2]}/{instance.sha256}.{ext}"


class MeshStatus(models.TextChoices):
    NONE = "", "Not a 3D file"
    PENDING = "pending", "Waiting to be prepared"
    CONVERTING = "converting", "Being prepared"
    READY = "ready", "Ready"
    FAILED = "failed", "Couldn't be shown"
    NATIVE = "native", "Native CAD file"


class MeshPreview(models.Model):
    """Adds a converted, viewer-ready copy of a 3D file. Subclasses set MESH_KIND and have `name`, `file`, `sha256`."""

    MESH_KIND = ""

    mesh = models.FileField(upload_to=mesh_path, blank=True, max_length=300)
    thumb = models.FileField(upload_to=mesh_path, blank=True, max_length=300)
    mesh_status = models.CharField(max_length=12, blank=True, default="", choices=MeshStatus.choices)
    mesh_message = models.CharField(max_length=300, blank=True)
    mesh_info = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True

    @property
    def is_3d(self):
        e = formats.ext_of(self.name)
        return e in formats.VIEWABLE or e in formats.NATIVE

    @property
    def mesh_url(self):
        return reverse("cad:mesh", args=[self.MESH_KIND, self.pk])

    @property
    def thumb_url(self):
        return reverse("cad:thumb", args=[self.MESH_KIND, self.pk]) if self.thumb else ""

    @property
    def mesh_status_url(self):
        return reverse("cad:status", args=[self.MESH_KIND, self.pk])

    @property
    def format_label(self):
        e = formats.ext_of(self.name)
        return formats.VIEWABLE.get(e) or formats.NATIVE.get(e) or e.upper()

    def initial_mesh_state(self):
        """Set the preview status for a newly stored file (without converting)."""
        e = formats.ext_of(self.name)
        if e in formats.NATIVE:
            self.mesh_status = MeshStatus.NATIVE
            self.mesh_message = (f"{formats.NATIVE[e]} files are stored and versioned here but open only in their own program. "
                                 "Upload a STEP or 3MF export next to it to view it in 3D.")
        elif e in ("step", "stp", "iges", "igs") and not formats.step_available():
            self.mesh_status = MeshStatus.FAILED
            self.mesh_message = "STEP and IGES preview isn't installed on this server. Upload an STL, 3MF, glTF or VRML export to view it."
        elif e in formats.VIEWABLE:
            self.mesh_status = MeshStatus.PENDING
        else:
            self.mesh_status = MeshStatus.NONE


class MeshJob(models.Model):
    """Bookkeeping for a conversion in progress: when it started and how often it was tried.

    A row exists while a file is being converted; it is removed when the conversion ends. A row
    that is older than the time limit means the process doing the work died (restart, crash), and
    `jobs.tick()` retries the file or gives up after a few attempts.
    """

    model = models.CharField(max_length=60)  # "design.DesignFile"
    object_id = models.PositiveBigIntegerField()
    attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("model", "object_id")]

    def __str__(self):
        return f"{self.model} {self.object_id} (attempt {self.attempts})"
