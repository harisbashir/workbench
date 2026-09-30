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


class Board(models.Model):
    """One PCB (assembly) of a product, e.g. the power board, control board or an I/O board.

    A product with a single PCB simply has one board. Each board has its own
    revisions, and each revision its own BOM, design files and builds.
    """

    class Kind(models.TextChoices):
        MAIN = "main", "Main / controller board"
        POWER = "power", "Power board"
        CONTROL = "control", "Control board"
        PERIPHERAL = "peripheral", "Peripheral / I/O board"
        INTERFACE = "interface", "Interface / connector board"
        DISPLAY = "display", "Display / user interface board"
        SENSOR = "sensor", "Sensor board"
        RF = "rf", "RF / wireless module"
        OTHER = "other", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="boards")
    name = models.CharField(max_length=80, help_text="e.g. Power board, Control board, Front panel")
    code = models.CharField(max_length=12, blank=True,
                            help_text="Optional short code or part number printed on the PCB, e.g. PB or PCB-1001.")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MAIN)
    description = models.TextField(blank=True, help_text="What this board does and how it connects to the others.")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "order", "id"]
        unique_together = [("project", "name")]

    def __str__(self):
        return f"{self.project.key} {self.name}"

    def get_absolute_url(self):
        return reverse("projects:hw_board", args=[self.project.key, self.pk])

    @property
    def latest_revision(self):
        return self.revisions.order_by("-created_at").first()


def default_board(project):
    """The project's board, creating a 'Main board' for projects that have none yet."""
    board = project.boards.order_by("order", "id").first()
    if board is None:
        board = Board.objects.create(project=project, name="Main board", kind=Board.Kind.MAIN)
    return board


class Revision(models.Model):
    """One revision of a board, e.g. Power board Rev A, Rev B."""

    class Status(models.TextChoices):
        DESIGN = "design", "In design"
        REVIEW = "review", "In review"
        RELEASED = "released", "Released for production"
        OBSOLETE = "obsolete", "Obsolete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="revisions")
    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="revisions")
    name = models.CharField(max_length=40, help_text="e.g. Rev A, v1.2, EVT")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DESIGN)
    target_date = models.DateField(null=True, blank=True)
    git_ref = models.CharField(max_length=100, blank=True, help_text="Optional git tag or commit this revision was released from.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "board__order", "board_id", "-created_at"]
        unique_together = [("board", "name")]

    def __str__(self):
        if self.single_board:
            return f"{self.project.key} {self.name}"
        return f"{self.project.key} {self.board.name} {self.name}"

    @property
    def single_board(self):
        """True when the product has only one board, so the board's name adds nothing to labels."""
        project = self.project
        if not hasattr(project, "_board_count"):
            project._board_count = project.boards.count()
        return project._board_count <= 1

    def save(self, *args, **kwargs):
        if self.board_id and not self.project_id:
            self.project = self.board.project
        elif self.project_id and not self.board_id:
            self.board = default_board(self.project)
        super().save(*args, **kwargs)

    @property
    def title(self):
        """'Rev B' for single-board products, 'Power board Rev B' otherwise."""
        return self.name if self.single_board else f"{self.board.name} {self.name}"

    def get_absolute_url(self):
        return reverse("projects:revision", args=[self.project.key, self.pk])

    @property
    def checklist_done(self):
        items = list(self.checks.all())
        return bool(items) and all(c.done_at for c in items)

    def add_default_checks(self):
        for i, text in enumerate(DEFAULT_REVIEW_CHECKS):
            RevisionCheck.objects.get_or_create(revision=self, text=text, defaults={"order": i})


DEFAULT_REVIEW_CHECKS = [
    "ERC passes with no unexplained errors",
    "DRC passes with no unexplained errors",
    "Every part has a manufacturer part number and is in the parts library",
    "No obsolete or not-recommended parts on the BOM",
    "Footprints and pinouts checked against datasheets",
    "Power, decoupling and ESD protection reviewed",
    "Test points and programming/debug header accessible",
    "Fabrication notes, stack-up and board outline reviewed",
    "Firmware builds and runs on this revision",
    "BOM compared with the previous revision and changes explained",
]


class RevisionCheck(models.Model):
    """One item of a revision's release checklist (design review sign-off)."""

    revision = models.ForeignKey(Revision, on_delete=models.CASCADE, related_name="checks")
    text = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]


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
    blocked_reason = models.CharField(
        "Blocked by", max_length=200, blank=True,
        help_text="If this task can't move forward, say why (e.g. waiting for parts, waiting on PWR-3). Leave empty when not blocked.")

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
                # Lock the project row: select_for_update() is dropped by aggregate(), so it
                # can't protect the MAX() itself. Concurrent creators queue here instead of
                # both taking the same number.
                list(Project.objects.select_for_update().filter(pk=self.project_id).values_list("pk", flat=True))
                last = Task.objects.filter(project=self.project).aggregate(m=Max("number"))["m"] or 0
                self.number = last + 1
                super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)

    @property
    def is_blocked(self):
        return bool(self.blocked_reason)

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


def attachment_path(instance, filename):  # kept for migration 0001
    return f"tasks/{instance.task.project.key}/{instance.task.number}/{filename}"


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
