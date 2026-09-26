"""Firmware management.

  Firmware   one firmware image a product runs, e.g. "Main application" or
             "Bootloader". A project can have several.
  Release    one version of that firmware (semantic versioning, e.g. 1.4.2),
             with its binaries, release notes, the git tag/commit it was built
             from, and the board revisions it's compatible with.

A release moves Draft → Testing → Released, and later Deprecated (superseded)
or Recalled (must not be used). Once released, its binaries can't change —
a fix is always a new version. That's what makes firmware traceable.
"""
import hashlib
import os
import re

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
TASK_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")


def parse_version(text):
    """'v1.4.2' -> (1, 4, 2, prerelease or '') or None if it isn't semantic versioning."""
    text = (text or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    m = SEMVER_RE.match(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""


def sort_key(version):
    major, minor, patch, pre = parse_version(version)
    # Pre-releases (1.2.0-rc.1) sort before the final release (1.2.0).
    return f"{major:06d}.{minor:06d}.{patch:06d}.{pre or '~'}"


class Firmware(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="firmwares")
    name = models.CharField(max_length=80, help_text="e.g. Main application, Bootloader, Radio co-processor")
    slug = models.SlugField(max_length=80)
    target = models.CharField("Target / MCU", max_length=80, blank=True, help_text="e.g. STM32G031K8, ESP32-S3")
    description = models.TextField(blank=True)
    github_repo = models.CharField(
        "GitHub repository", max_length=200, blank=True,
        help_text="owner/name. Leave empty if it's the project's repository.")
    tag_prefix = models.CharField(
        max_length=40, default="v",
        help_text="Git tags for this firmware start with this, e.g. “v” for v1.4.2, or “boot-v” for boot-v1.0.0. "
                  "Published GitHub releases with a matching tag are added here automatically.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "name"]
        unique_together = [("project", "slug")]

    def __str__(self):
        return f"{self.project.key} · {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:70] or "firmware"
            slug, i = base, 2
            while Firmware.objects.filter(project=self.project, slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("firmware:detail", args=[self.project.key, self.slug])

    @property
    def repo(self):
        return self.github_repo or self.project.github_repo

    def releases_sorted(self):
        return self.releases.order_by("-sort_key")

    @property
    def latest_released(self):
        return self.releases.filter(status=FirmwareRelease.Status.RELEASED).order_by("-sort_key").first()

    def recommended_for(self, revision):
        """The newest released version compatible with a board revision."""
        return self.releases.filter(status=FirmwareRelease.Status.RELEASED, revisions=revision).order_by("-sort_key").first()

    def suggest_next_version(self):
        last = self.releases.order_by("-sort_key").first()
        if not last:
            return "0.1.0"
        major, minor, patch, pre = parse_version(last.version)
        if pre:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"


class FirmwareRelease(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        TESTING = "testing", "Testing"
        RELEASED = "released", "Released"
        DEPRECATED = "deprecated", "Deprecated"
        RECALLED = "recalled", "Recalled"

    STATUS_HELP = {
        "draft": "Being prepared. Binaries and notes can still change.",
        "testing": "Ready for testing on real hardware. Binaries can still change.",
        "released": "Approved for use in production and the field. Locked — fixes need a new version.",
        "deprecated": "Replaced by a newer version. Don't use it for new builds.",
        "recalled": "Must not be used — it has a serious problem.",
    }

    firmware = models.ForeignKey(Firmware, on_delete=models.CASCADE, related_name="releases")
    version = models.CharField(max_length=60, help_text="Semantic version: MAJOR.MINOR.PATCH, e.g. 1.4.2 or 2.0.0-rc.1")
    sort_key = models.CharField(max_length=120, editable=False, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    git_ref = models.CharField("Git tag or commit", max_length=100, blank=True,
                               help_text="The tag or commit this was built from, e.g. v1.4.2 or 3f9c2ab")
    source_url = models.URLField(blank=True, help_text="Link to the GitHub release or CI run, if any.")
    notes = models.TextField("Release notes", blank=True)
    revisions = models.ManyToManyField("projects.Revision", blank=True, related_name="firmware_releases",
                                       verbose_name="Compatible board revisions")
    status_note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["firmware", "-sort_key"]
        unique_together = [("firmware", "version")]

    def __str__(self):
        return f"{self.firmware.name} {self.version}"

    def save(self, *args, **kwargs):
        v = self.version.strip()
        if v[:1] in ("v", "V"):
            v = v[1:]
        self.version = v
        self.sort_key = sort_key(v)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("firmware:release", args=[self.firmware.project.key, self.firmware.slug, self.version])

    @property
    def is_locked(self):
        return self.status in (self.Status.RELEASED, self.Status.DEPRECATED, self.Status.RECALLED)

    @property
    def is_prerelease(self):
        return bool(parse_version(self.version)[3])

    @property
    def status_help(self):
        return self.STATUS_HELP.get(self.status, "")

    @property
    def git_url(self):
        repo = self.firmware.repo
        if not (repo and self.git_ref):
            return ""
        if re.fullmatch(r"[0-9a-f]{7,40}", self.git_ref):
            return f"https://github.com/{repo}/commit/{self.git_ref}"
        return f"https://github.com/{repo}/tree/{self.git_ref}"

    def mentioned_task_keys(self):
        return sorted(set(f"{k}-{n}" for k, n in TASK_KEY_RE.findall(self.notes or "")))

    def set_status(self, status, user, note=""):
        self.status = status
        self.status_note = note[:300]
        self.status_changed_at = timezone.now()
        if status == self.Status.RELEASED and not self.released_at:
            self.released_at = timezone.now()
            self.released_by = user
        self.save()


def artifact_path(instance, filename):
    r = instance.release
    return f"firmware/{r.firmware.project.key}/{r.firmware.slug}/{r.version}/{filename}"


class FirmwareArtifact(models.Model):
    """A file belonging to a release: the image to flash, debug symbols, etc."""

    class Kind(models.TextChoices):
        IMAGE = "image", "Image to flash"
        BOOTLOADER = "bootloader", "Bootloader"
        DEBUG = "debug", "Debug symbols / map"
        PACKAGE = "package", "Update package"
        OTHER = "other", "Other"

    release = models.ForeignKey(FirmwareRelease, on_delete=models.CASCADE, related_name="artifacts")
    file = models.FileField(upload_to=artifact_path, max_length=400)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.IMAGE)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name

    @staticmethod
    def guess_kind(name):
        n = name.lower()
        ext = os.path.splitext(n)[1]
        if "boot" in n:
            return FirmwareArtifact.Kind.BOOTLOADER
        if ext in (".elf", ".map", ".axf", ".out", ".lst", ".sym"):
            return FirmwareArtifact.Kind.DEBUG
        if ext in (".zip", ".tar", ".gz", ".dfu", ".ota", ".pkg"):
            return FirmwareArtifact.Kind.PACKAGE
        if ext in (".bin", ".hex", ".uf2", ".srec", ".s19", ".img", ".ihex"):
            return FirmwareArtifact.Kind.IMAGE
        return FirmwareArtifact.Kind.OTHER

    @classmethod
    def store(cls, release, uploaded, user):
        from apps.files.models import safe_name
        name = safe_name(uploaded.name)
        h = hashlib.sha256()
        for chunk in uploaded.chunks():
            h.update(chunk)
        uploaded.seek(0)
        art = cls(release=release, name=name, kind=cls.guess_kind(name), size=uploaded.size,
                  sha256=h.hexdigest(), uploaded_by=user)
        art.file.save(name, uploaded, save=False)
        art.save()
        return art
