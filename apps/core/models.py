from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record of security-relevant and business-relevant changes."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.object_repr}"


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    text = models.CharField(max_length=255)
    url = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    emailed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SiteSettings(models.Model):
    """Settings an administrator can change in the app (System → Settings)."""

    company_name = models.CharField(max_length=80, default="Workbench")
    site_url = models.URLField(
        blank=True, help_text="The address people use to open Workbench, e.g. https://workbench.example.com. "
                              "Used in sign-in links, emails and the GitHub webhook address.")
    time_zone = models.CharField(max_length=64, default="UTC", help_text="Used to schedule nightly backups.")

    # GitHub
    _github_secret = models.TextField(blank=True, db_column="github_secret")

    # Email (optional)
    email_enabled = models.BooleanField(default=False, help_text="Send notifications and sign-in links by email.")
    smtp_host = models.CharField("SMTP server", max_length=200, blank=True, help_text="e.g. smtp.gmail.com or smtp.office365.com")
    smtp_port = models.PositiveIntegerField("SMTP port", default=587)
    smtp_username = models.CharField("SMTP username", max_length=200, blank=True)
    _smtp_password = models.TextField(blank=True, db_column="smtp_password")
    smtp_use_tls = models.BooleanField("Use STARTTLS", default=True)
    email_from = models.EmailField("Send emails from", blank=True, help_text="e.g. workbench@yourcompany.com")

    # Backups & storage
    backup_enabled = models.BooleanField("Nightly backups", default=True)
    backup_hour = models.PositiveSmallIntegerField("Backup time (hour, 0–23)", default=2)
    backup_keep = models.PositiveSmallIntegerField("Backups to keep", default=14)
    trash_days = models.PositiveSmallIntegerField("Empty trash after (days)", default=30)
    max_upload_mb = models.PositiveIntegerField("Largest upload (MB)", default=200)

    # Branding
    logo = models.FileField(upload_to="branding/", blank=True, max_length=200,
                            help_text="Shown on light backgrounds (sign-in page, printouts).")
    logo_dark = models.FileField(upload_to="branding/", blank=True, max_length=200,
                                 help_text="Optional version for the dark menu bar, e.g. a white logo.")
    logo_updated_at = models.DateTimeField(null=True, blank=True)

    setup_completed_at = models.DateTimeField(null=True, blank=True)
    last_backup_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "site settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        if not obj._github_secret:
            import secrets
            obj.github_secret = secrets.token_hex(32)
            obj.save(update_fields=["_github_secret"])
        return obj

    # Encrypted fields --------------------------------------------------------
    @property
    def github_secret(self):
        from .crypto import decrypt
        return decrypt(self._github_secret) if self._github_secret else ""

    @github_secret.setter
    def github_secret(self, value):
        from .crypto import encrypt
        self._github_secret = encrypt(value) if value else ""

    @property
    def smtp_password(self):
        from .crypto import decrypt
        return decrypt(self._smtp_password) if self._smtp_password else ""

    @smtp_password.setter
    def smtp_password(self, value):
        from .crypto import encrypt
        self._smtp_password = encrypt(value) if value else ""

    @property
    def email_ready(self):
        return self.email_enabled and bool(self.smtp_host and self.email_from)

    @property
    def logo_version(self):
        return int(self.logo_updated_at.timestamp()) if self.logo_updated_at else 0

    def absolute_url(self, path, request=None):
        base = (self.site_url or "").rstrip("/")
        if not base and request is not None:
            base = f"{request.scheme}://{request.get_host()}"
        return base + path


class ExportJob(models.Model):
    """A request for an organised 'download everything' zip (built in the background)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        RUNNING = "running", "Preparing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    include_versions = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    file_name = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(default=0)
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class StorageSettings(models.Model):
    """Where uploaded files are kept (System → Storage). One row.

    Configs are dicts: {"kind": "local", "path": …} or {"kind": "s3", "provider": …,
    "bucket": …, "region": …, "endpoint": …, "prefix": …, "access_key": "enc:…",
    "secret_key": "enc:…"}. Secrets are encrypted with the installation key.
    """

    active = models.JSONField(default=dict, blank=True)     # empty = the default folder (data/files)
    draft = models.JSONField(default=dict, blank=True)      # being set up / tested
    previous = models.JSONField(default=dict, blank=True)   # fallback for reads after a move
    locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    draft_tested_at = models.DateTimeField(null=True, blank=True)
    draft_test_ok = models.BooleanField(default=False)
    draft_test_message = models.CharField(max_length=300, blank=True)
    rev = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "storage settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.rev += 1
        super().save(*args, **kwargs)
        from .storage import reset_cache
        reset_cache()


class StorageMove(models.Model):
    """Copying every file from one storage to another, then switching to it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to start"
        RUNNING = "running", "Copying files"
        DONE = "done", "Finished"
        FAILED = "failed", "Stopped"

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    source = models.JSONField(default=dict)
    target = models.JSONField(default=dict)
    lock_after = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    total = models.PositiveIntegerField(default=0)
    done_count = models.PositiveIntegerField(default=0)
    copied = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    bytes_copied = models.BigIntegerField(default=0)
    failures = models.JSONField(default=list, blank=True)
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def percent(self):
        return int(self.done_count * 100 / self.total) if self.total else (100 if self.status == "done" else 0)
