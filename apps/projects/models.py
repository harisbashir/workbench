import re

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Max
from django.urls import reverse

key_validator = RegexValidator(
    r"^[A-Z][A-Z0-9]{1,9}$",
    "Use 2–10 capital letters or digits, starting with a letter (e.g. PWR, SENS2).",
)
repo_validator = RegexValidator(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    "Use the form owner/repository, e.g. acme/power-board.",
)

TASK_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")


class ProjectQuerySet(models.QuerySet):
    def visible_to(self, user):
        if user.sees_all_projects:
            return self
        return self.filter(models.Q(members=user) | models.Q(lead=user)).distinct()


class Project(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ON_HOLD = "on_hold", "On hold"
        ARCHIVED = "archived", "Archived"

    key = models.CharField(
        max_length=10, unique=True, validators=[key_validator],
        help_text="Short code used in task IDs (PWR-12) and to link GitHub pull requests.",
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="led_projects")
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="projects")
    github_repo = models.CharField(
        max_length=200, blank=True, validators=[repo_validator],
        help_text="Optional. The GitHub repository for this project, e.g. acme/power-board.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ["status", "name"]

    def __str__(self):
        return f"{self.key} · {self.name}"

    def get_absolute_url(self):
        return reverse("projects:detail", args=[self.key])

    @property
    def github_url(self):
        return f"https://github.com/{self.github_repo}" if self.github_repo else ""

    def is_member(self, user):
        return self.lead_id == user.pk or self.members.filter(pk=user.pk).exists()

    def can_view(self, user):
        return user.sees_all_projects or self.is_member(user)

    def can_edit(self, user):
        if user.is_read_only:
            return False
        return user.can_manage_projects or self.is_member(user)


class Revision(models.Model):
    """A hardware/firmware revision of the product, e.g. Rev A, Rev B."""

    class Status(models.TextChoices):
        DESIGN = "design", "In design"
        REVIEW = "review", "In review"
        RELEASED = "released", "Released for production"
        OBSOLETE = "obsolete", "Obsolete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="revisions")
    name = models.CharField(max_length=40, help_text="e.g. Rev A, v1.2, EVT")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DESIGN)
    target_date = models.DateField(null=True, blank=True)
    git_ref = models.CharField(max_length=100, blank=True, help_text="Optional git tag or commit this revision was released from.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "-created_at"]
        unique_together = [("project", "name")]

    def __str__(self):
        return f"{self.project.key} {self.name}"

    def get_absolute_url(self):
        return reverse("inventory:bom", args=[self.pk])


class Task(models.Model):
    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        REVIEW = "review", "In review"
        DONE = "done", "Done"

    class Kind(models.TextChoices):
        FIRMWARE = "firmware", "Firmware"
        SCHEMATIC = "schematic", "Schematic"
        PCB = "pcb", "PCB layout"
        MECHANICAL = "mechanical", "Mechanical"
        TEST = "test", "Test / validation"
        PRODUCTION = "production", "Production"
        OTHER = "other", "Other"

    class Priority(models.IntegerChoices):
        LOW = 1, "Low"
        NORMAL = 2, "Normal"
        HIGH = 3, "High"
        URGENT = 4, "Urgent"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.FIRMWARE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO, db_index=True)
    priority = models.IntegerField(choices=Priority.choices, default=Priority.NORMAL)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_tasks")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="review_tasks",
                                 help_text="Who should review the design or code for this task.")
    revision = models.ForeignKey(Revision, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "due_date", "number"]
        unique_together = [("project", "number")]

    def __str__(self):
        return f"{self.key} {self.title}"

    @property
    def key(self):
        return f"{self.project.key}-{self.number}"

    def get_absolute_url(self):
        return reverse("projects:task", args=[self.project.key, self.number])

    def save(self, *args, **kwargs):
        if not self.number:
            with transaction.atomic():
                last = Task.objects.select_for_update().filter(project=self.project).aggregate(m=Max("number"))["m"] or 0
                self.number = last + 1
                super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        from django.utils import timezone
        return bool(self.due_date and self.status != self.Status.DONE and self.due_date < timezone.localdate())


class TaskComment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


def attachment_path(instance, filename):
    return f"tasks/{instance.task.project.key}/{instance.task.number}/{filename}"


class TaskAttachment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path)
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]


class Activity(models.Model):
    """Timeline entries shown on projects and tasks (people and GitHub)."""

    class Source(models.TextChoices):
        USER = "user", "Workbench"
        GITHUB = "github", "GitHub"
        SYSTEM = "system", "System"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="activities")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_label = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.USER)
    text = models.CharField(max_length=500)
    url = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def who(self):
        if self.actor:
            return self.actor.display_name
        return self.actor_label or self.get_source_display()


def log_activity(project, text, actor=None, task=None, url="", source=Activity.Source.USER, actor_label=""):
    return Activity.objects.create(project=project, task=task, actor=actor, text=text[:500], url=url,
                                   source=source, actor_label=actor_label)
