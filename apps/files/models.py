"""The file library.

Files are organised into *spaces* — one per project plus a company-wide
"Shared" space — and folders inside them. Every upload of a document is kept
as a numbered version. On disk everything lives under <data>/files, laid out
so a person browsing the folder can still find things:

    files/<PROJECT-KEY or shared>/<document id>/v<version>-<original name>
"""
import hashlib
import os
import re

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

SHARED = "shared"

PREVIEW_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
PREVIEW_PDF = {".pdf"}
PREVIEW_TEXT = {".txt", ".log", ".csv", ".tsv", ".md", ".json", ".yaml", ".yml", ".ini", ".cfg", ".c", ".h",
                ".cpp", ".hpp", ".py", ".s", ".ld", ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".net", ".xml"}

# Anything that could run in a browser is blocked. Everything else is allowed,
# because hardware work uses a long tail of file types (Gerbers, STEP, logs…).
BLOCKED_EXTENSIONS = {".html", ".htm", ".xhtml", ".js", ".mjs", ".exe", ".bat", ".cmd", ".com", ".msi", ".scr",
                      ".ps1", ".vbs", ".jar", ".app", ".dll", ".sh", ".php", ".asp", ".aspx", ".jsp", ".hta"}


def safe_name(name):
    name = os.path.basename(name or "file").strip().replace("\x00", "")
    name = re.sub(r"[^\w.\- ()+]", "_", name)[:180]
    return name or "file"


def unique_name(project, folder, name, exclude_pk=None, suffix=""):
    """`name`, or "stem (2).ext", "stem (3).ext"… so no live document in the folder has it.
    With `suffix` (e.g. "restored") the first alternative is "stem (restored).ext"."""
    stem, ext = os.path.splitext(name)
    taken = Document.objects.alive().filter(project=project, folder=folder)
    if exclude_pk:
        taken = taken.exclude(pk=exclude_pk)
    candidates = [name] + ([f"{stem} ({suffix}){ext}"] if suffix else [])
    i = 2
    while True:
        for c in candidates:
            if not taken.filter(name=c).exists():
                return c
        candidates = [f"{stem} ({suffix} {i}){ext}" if suffix else f"{stem} ({i}){ext}"]
        i += 1


def space_label(space):
    return "Shared" if space == SHARED else space


class FolderQuerySet(models.QuerySet):
    pass


class Folder(models.Model):
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="folders")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    name = models.CharField(max_length=120)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["project", "parent", "name"], name="unique_folder_name")]

    def __str__(self):
        return self.name

    @property
    def space(self):
        return self.project.key if self.project_id else SHARED

    def get_absolute_url(self):
        return reverse("files:folder", args=[self.space, self.pk])

    def ancestors(self):
        chain, node = [], self.parent
        while node is not None:
            chain.insert(0, node)
            node = node.parent
        return chain

    def descendant_ids(self):
        ids, frontier = [self.pk], [self.pk]
        while frontier:
            frontier = list(Folder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
            ids += frontier
        return ids

    @classmethod
    def get_or_create_path(cls, project, *names, user=None):
        parent = None
        for name in names:
            parent, _ = cls.objects.get_or_create(project=project, parent=parent, name=name, defaults={"created_by": user})
        return parent


class DocumentQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def trashed(self):
        return self.filter(deleted_at__isnull=False)

    def visible_to(self, user):
        from apps.projects.models import Project
        return self.filter(Q(project__isnull=True) | Q(project__in=Project.objects.visible_to(user)))


class Document(models.Model):
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    task = models.ForeignKey("projects.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=300, blank=True)
    size = models.BigIntegerField(default=0, help_text="Size of the latest version, in bytes.")
    version_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    objects = DocumentQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("files:document", args=[self.pk])

    @property
    def space(self):
        return self.project.key if self.project_id else SHARED

    @property
    def ext(self):
        return os.path.splitext(self.name)[1].lower()

    @property
    def preview_kind(self):
        if self.ext in PREVIEW_PDF:
            return "pdf"
        if self.ext in PREVIEW_IMAGE:
            return "image"
        if self.ext in PREVIEW_TEXT:
            return "text"
        return ""

    @property
    def latest(self):
        return self.versions.order_by("-number").first()

    def add_version(self, uploaded, user, note=""):
        from django.db import transaction
        with transaction.atomic():
            # Lock the document row so two uploads at once can't both take the same number.
            locked = Document.objects.select_for_update().get(pk=self.pk)
            return self._add_version(locked, uploaded, user, note)

    def _add_version(self, locked, uploaded, user, note):
        number = max(locked.version_count, self.versions.aggregate(m=models.Max("number"))["m"] or 0) + 1
        v = DocumentVersion(document=self, number=number, uploaded_by=user, note=note[:200],
                            original_name=safe_name(uploaded.name), size=uploaded.size)
        h = hashlib.sha256()
        for chunk in uploaded.chunks():
            h.update(chunk)
        v.sha256 = h.hexdigest()
        uploaded.seek(0)
        v.file.save(f"v{number}-{v.original_name}", uploaded, save=False)
        v.save()
        self.version_count = number
        self.size = v.size
        self.updated_by = user
        self.save()
        return v

    def can_view(self, user):
        return self.project is None or self.project.can_view(user)

    def can_edit(self, user):
        if user.is_read_only:
            return False
        return self.project is None or self.project.can_edit(user)

    @property
    def total_size(self):
        return self.versions.aggregate(s=Sum("size"))["s"] or 0

    def trash(self, user):
        self.deleted_at = timezone.now()
        self.deleted_by = user
        self.save(update_fields=["deleted_at", "deleted_by"])

    def restore(self):
        """Brings the document back; renames it to "name (restored).ext" if its folder
        already has a live file with the same name."""
        self.deleted_at = None
        self.deleted_by = None
        if self.folder_id and not Folder.objects.filter(pk=self.folder_id).exists():
            self.folder = None
        self.name = unique_name(self.project, self.folder, self.name, exclude_pk=self.pk, suffix="restored")
        self.save()

    def purge(self):
        for v in self.versions.all():
            v.file.delete(save=False)
        space, pk = self.space, self.pk
        self.delete()
        # Tidy the now-empty folder when files are stored locally.
        from apps.core.storage import local_root
        location = local_root()
        if location:
            try:
                os.rmdir(os.path.join(location, space, str(pk)))
            except OSError:
                pass


def version_path(instance, filename):
    doc = instance.document
    return f"{doc.space}/{doc.pk}/{filename}"


class DocumentVersion(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    file = models.FileField(upload_to=version_path, max_length=400)
    original_name = models.CharField(max_length=200)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    note = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        unique_together = [("document", "number")]

    def __str__(self):
        return f"{self.document.name} v{self.number}"
