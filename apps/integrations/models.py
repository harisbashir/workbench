from django.db import models


class PullRequest(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        DRAFT = "draft", "Draft"
        MERGED = "merged", "Merged"
        CLOSED = "closed", "Closed"

    class Checks(models.TextChoices):
        UNKNOWN = "", "—"
        PENDING = "pending", "Running"
        SUCCESS = "success", "Passed"
        FAILURE = "failure", "Failed"

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="pull_requests")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=300)
    url = models.URLField()
    author_login = models.CharField(max_length=39, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    branch = models.CharField(max_length=255, blank=True)
    head_sha = models.CharField(max_length=40, blank=True)
    checks = models.CharField(max_length=10, choices=Checks.choices, blank=True, default="")
    review_state = models.CharField(max_length=30, blank=True)
    tasks = models.ManyToManyField("projects.Task", blank=True, related_name="pull_requests")
    opened_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        unique_together = [("project", "number")]

    def __str__(self):
        return f"#{self.number} {self.title}"


class WebhookDelivery(models.Model):
    """Every GitHub delivery we receive, for troubleshooting and replay protection."""

    delivery_id = models.CharField(max_length=64, unique=True)
    # SHA-256 of the signed body. GitHub doesn't sign the delivery-ID header, so replay
    # protection keys on the body: a captured payload re-sent under a new ID is refused.
    body_sha256 = models.CharField(max_length=64, null=True, blank=True, unique=True)
    event = models.CharField(max_length=40)
    repository = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20)  # processed / ignored / rejected / error
    message = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
