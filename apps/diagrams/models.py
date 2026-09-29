"""Hardware block diagrams: system (whole product), high-level and detailed (per board).

The drawing is stored as JSON. Every save is a new numbered version, so a review
always refers to an exact drawing. Diagrams go Draft → In review → Approved; saving
a new version of an approved diagram starts a new draft (the approved version stays
on record).
"""
from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.projects.models import Board, Project


class Diagram(models.Model):
    class Level(models.TextChoices):
        SYSTEM = "system", "System (whole product)"
        HIGH = "high", "High-level"
        DETAILED = "detailed", "Detailed (low-level)"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEW = "review", "In review"
        APPROVED = "approved", "Approved"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="diagrams")
    board = models.ForeignKey(Board, null=True, blank=True, on_delete=models.CASCADE, related_name="diagrams",
                              help_text="Leave empty for a system diagram of the whole product.")
    name = models.CharField(max_length=120)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.HIGH)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    current_version = models.PositiveIntegerField(default=0)
    approved_version = models.PositiveIntegerField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "board__order", "level", "name"]

    def __str__(self):
        where = self.board.name if self.board_id else self.project.key
        return f"{where} · {self.name}"

    def get_absolute_url(self):
        return reverse("diagrams:detail", args=[self.project.key, self.pk])

    @property
    def latest(self):
        return self.versions.order_by("-number").first()

    @property
    def subject(self):
        """What the diagram describes, for title blocks."""
        return self.board.name if self.board_id else f"{self.project.name} (system)"


class DiagramVersion(models.Model):
    diagram = models.ForeignKey(Diagram, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    data = models.JSONField()
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["diagram", "-number"]
        unique_together = [("diagram", "number")]

    def __str__(self):
        return f"{self.diagram} v{self.number}"


class DiagramComment(models.Model):
    """A review comment, optionally pinned to one block of the drawing."""

    diagram = models.ForeignKey(Diagram, on_delete=models.CASCADE, related_name="comments")
    version = models.PositiveIntegerField()
    node_id = models.CharField(max_length=40, blank=True)
    node_label = models.CharField(max_length=120, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    text = models.TextField()
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
