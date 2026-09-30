import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.core.crypto import decrypt, encrypt


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        LEAD = "lead", "Engineering lead"
        ENGINEER = "engineer", "Engineer"
        PROCUREMENT = "procurement", "Procurement / production"
        VIEWER = "viewer", "Viewer (read-only)"

    ROLE_HELP = {
        Role.ADMIN: "Full access, including users, security settings and the audit log.",
        Role.LEAD: "Manages all projects, revisions, tasks, BOMs and production.",
        Role.ENGINEER: "Works on tasks and BOMs in the projects they are a member of.",
        Role.PROCUREMENT: "Manages parts, suppliers, purchase orders and builds. Can view all projects.",
        Role.VIEWER: "Can view the projects they are a member of, but not change anything.",
    }

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ENGINEER)
    job_title = models.CharField(max_length=120, blank=True)
    time_zone = models.CharField(
        max_length=64, default="UTC",
        help_text="Used to show times in your local time, e.g. Asia/Karachi or America/Toronto.",
    )
    github_username = models.CharField(
        max_length=39, blank=True,
        help_text="Lets Workbench match GitHub activity to you.",
    )

    email_notifications = models.BooleanField(
        default=True, help_text="Also send my notifications by email (when the administrator has set up email).")

    # Two-factor authentication (TOTP). The secret is encrypted at rest.
    _mfa_secret = models.TextField(blank=True, db_column="mfa_secret")
    mfa_enabled = models.BooleanField(default=False)
    mfa_last_step = models.BigIntegerField(default=0, editable=False)  # stops a code being used twice

    # Brute-force protection.
    failed_logins = models.PositiveIntegerField(default=0)
    last_failed_login = models.DateTimeField(null=True, blank=True, editable=False)
    mfa_failures = models.PositiveIntegerField(default=0, editable=False)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def initials(self):
        name = self.display_name.split()
        return "".join(p[0] for p in name[:2]).upper() or "?"

    # --- MFA ---------------------------------------------------------------
    @property
    def mfa_secret(self):
        return decrypt(self._mfa_secret) if self._mfa_secret else ""

    @mfa_secret.setter
    def mfa_secret(self, value):
        self._mfa_secret = encrypt(value) if value else ""

    def verify_totp(self, code, secret=None):
        """Checks a 6-digit code (±30 s for clock drift). Each code works only once."""
        import time

        import pyotp
        code = (code or "").replace(" ", "")
        secret = secret or self.mfa_secret
        if not secret or not code.isdigit() or len(code) != 6:
            # An empty secret (e.g. the encryption key changed) must never accept codes.
            return False
        totp = pyotp.TOTP(secret)
        now_step = int(time.time()) // 30
        for step in (now_step - 1, now_step, now_step + 1):
            if step > self.mfa_last_step and pyotp.utils.strings_equal(totp.at(step * 30), code):
                # Claim the step atomically so two simultaneous requests can't both use one code.
                claimed = type(self).objects.filter(pk=self.pk, mfa_last_step__lt=step).update(mfa_last_step=step) if self.pk else 1
                if not claimed:
                    return False
                self.mfa_last_step = step
                return True
        return False

    # --- Lockout -----------------------------------------------------------
    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    # --- Role helpers ------------------------------------------------------
    @property
    def is_admin_role(self):
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def can_manage_projects(self):
        return self.is_admin_role or self.role == self.Role.LEAD

    @property
    def can_manage_procurement(self):
        return self.is_admin_role or self.role in (self.Role.LEAD, self.Role.PROCUREMENT)

    @property
    def sees_all_projects(self):
        return self.is_admin_role or self.role in (self.Role.LEAD, self.Role.PROCUREMENT)

    @property
    def is_read_only(self):
        return self.role == self.Role.VIEWER and not self.is_superuser

    @property
    def role_help(self):
        return self.ROLE_HELP.get(self.role, "")


class RecoveryCode(models.Model):
    """Single-use backup codes for when a user loses their authenticator."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=256)
    used_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def generate_for(cls, user, count=8):
        cls.objects.filter(user=user).delete()
        codes = []
        for _ in range(count):
            raw = "-".join(secrets.token_hex(3) for _ in range(2))
            codes.append(raw)
            cls.objects.create(user=user, code_hash=make_password(raw))
        return codes

    @classmethod
    def use(cls, user, raw):
        raw = raw.strip().lower()
        for rc in cls.objects.filter(user=user, used_at__isnull=True):
            if check_password(raw, rc.code_hash):
                # Conditional update: a code can only be used once, even by simultaneous requests.
                return bool(cls.objects.filter(pk=rc.pk, used_at__isnull=True).update(used_at=timezone.now()))
        return False
