"""Sources: versioned code and design files for projects and boards.

  Repository  a set of files kept together, e.g. "Main MCU firmware" or "Power board KiCad".
  Version     one check-in (v1, v2, …): a message, an author and the full list of files.
  Blob        the contents of one file, stored once however many versions use it.
  Lock        a file someone has checked out, so nobody else changes it meanwhile.
  Draft       an upload waiting for its check-in to be confirmed.

File contents live in Django's storage (local disk or S3) under sources/blobs/, named
by their SHA-256, so unchanged files cost nothing in later versions.
"""
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.text import slugify


class Repository(models.Model):
    class Kind(models.TextChoices):
        FIRMWARE = "firmware", "Firmware code"
        KICAD = "kicad", "KiCad design"
        OTHER = "other", "Other"

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="repositories")
    board = models.ForeignKey("projects.Board", null=True, blank=True, on_delete=models.CASCADE, related_name="repositories",
                              help_text="Leave empty for code or files the whole project shares.")
    name = models.CharField(max_length=80, help_text="e.g. Main MCU firmware, Power board KiCad")
    slug = models.SlugField(max_length=80)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    controller = models.ForeignKey("projects.Controller", null=True, blank=True, on_delete=models.SET_NULL, related_name="repositories",
                                   help_text="The chip this code runs on, if any.")
    firmware = models.ForeignKey("firmware.Firmware", null=True, blank=True, on_delete=models.SET_NULL, related_name="repositories",
                                 help_text="Versions of this code can become releases of this firmware.")
    description = models.CharField(max_length=300, blank=True)
    latest_number = models.PositiveIntegerField(default=0, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "board__order", "name"]
        unique_together = [("project", "slug")]
        verbose_name_plural = "repositories"

    def __str__(self):
        return f"{self.project.key} · {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:70] or "repository"
            slug, i = base, 2
            while Repository.objects.filter(project=self.project, slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("sources:repo", args=[self.project.key, self.slug])

    def latest(self):
        return self.versions.order_by("-number").first()


def blob_path(instance, filename):
    sha = instance.sha256
    return f"sources/blobs/{sha[:2]}/{sha[2:4]}/{sha}"


class Blob(models.Model):
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.BigIntegerField(default=0)
    file = models.FileField(upload_to=blob_path, max_length=200)
    is_text = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.sha256[:12]

    def read(self, limit=None):
        with self.file.open("rb") as fh:
            return fh.read() if limit is None else fh.read(limit)

    def text(self, limit=None):
        """The contents as text, or None for binary files (or text longer than `limit` bytes)."""
        if not self.is_text or (limit is not None and self.size > limit):
            return None
        data = self.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")


class Version(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    message = models.CharField(max_length=300)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    deleted = models.PositiveIntegerField(default=0)
    lines_added = models.PositiveIntegerField(default=0)
    lines_removed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["repository", "-number"]
        unique_together = [("repository", "number")]

    def __str__(self):
        return f"v{self.number}"

    def get_absolute_url(self):
        return f"{self.repository.get_absolute_url()}?v={self.number}"


class VersionFile(models.Model):
    version = models.ForeignKey(Version, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=400)
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["path"]
        unique_together = [("version", "path")]

    def __str__(self):
        return self.path


class Lock(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="locks")
    path = models.CharField(max_length=400)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["path"]
        unique_together = [("repository", "path")]

    def __str__(self):
        return f"{self.path} ({self.user})"


class Draft(models.Model):
    """Uploaded files waiting for their check-in to be confirmed. Removed after a day."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="drafts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    base_number = models.PositiveIntegerField(default=0)
    mode = models.CharField(max_length=8, default="update")
    files = models.JSONField(default=dict)    # {path: blob id}
    refused = models.JSONField(default=list)  # [[path, reason]]
    ignored = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
