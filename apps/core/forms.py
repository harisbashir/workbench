import zoneinfo

from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils.crypto import constant_time_compare

from apps.accounts.models import User

from .models import SiteSettings

TIME_ZONES = [(tz, tz) for tz in sorted(zoneinfo.available_timezones()) if "/" in tz and not tz.startswith("Etc/")] + [("UTC", "UTC")]


class SetupForm(forms.Form):
    setup_code = forms.CharField(max_length=20, help_text="Shown in the container log when Workbench starts "
                                 "(run <code>docker compose logs workbench</code>). It proves you're the person who installed it.")
    company_name = forms.CharField(max_length=80, initial="Workbench", help_text="Shown in the menu and in emails.")
    site_url = forms.URLField(label="Workbench address", help_text="The address your team will use, as it appears in the browser.")
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    username = forms.CharField(max_length=150, help_text="What you'll sign in with, e.g. haris.")
    email = forms.EmailField(required=False)
    time_zone = forms.ChoiceField(choices=TIME_ZONES, initial="America/Toronto")
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                help_text="At least 12 characters.")
    password2 = forms.CharField(label="Repeat password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, *args, expected_code="", **kwargs):
        self.expected_code = expected_code
        super().__init__(*args, **kwargs)

    def clean_setup_code(self):
        code = self.cleaned_data["setup_code"].strip().upper()
        if not constant_time_compare(code, self.expected_code):
            raise forms.ValidationError("That setup code isn't right. Copy it from the container log.")
        return code

    def clean_username(self):
        u = self.cleaned_data["username"].strip()
        User.username_validator(u)
        return u

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords don't match.")
        elif p1:
            try:
                validate_password(p1, User(username=data.get("username", ""), first_name=data.get("first_name", "")))
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return data


class GeneralSettingsForm(forms.ModelForm):
    time_zone = forms.ChoiceField(choices=TIME_ZONES, help_text="Used for scheduling nightly backups.")

    class Meta:
        model = SiteSettings
        fields = ["company_name", "site_url", "time_zone"]


class EmailSettingsForm(forms.ModelForm):
    smtp_password = forms.CharField(label="SMTP password", required=False, widget=forms.PasswordInput(render_value=False),
                                    help_text="Leave empty to keep the current password. For Gmail/Microsoft 365 use an app password.")

    class Meta:
        model = SiteSettings
        fields = ["email_enabled", "smtp_host", "smtp_port", "smtp_username", "smtp_use_tls", "email_from"]

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("smtp_password"):
            obj.smtp_password = self.cleaned_data["smtp_password"]
        if commit:
            obj.save()
        return obj


class StorageSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ["backup_enabled", "backup_hour", "backup_keep", "trash_days", "max_upload_mb"]

    def clean_backup_hour(self):
        h = self.cleaned_data["backup_hour"]
        if not 0 <= h <= 23:
            raise forms.ValidationError("Use an hour from 0 to 23.")
        return h


class LogoForm(forms.Form):
    logo = forms.FileField(required=False, label="Logo (for light backgrounds)",
                           help_text="SVG, or PNG/WebP with a transparent background. Wide logos work best, e.g. 400×100 px. Max 1 MB.")
    logo_dark = forms.FileField(required=False, label="Logo for the dark menu bar",
                                help_text="A light/white version of your logo. If you don't add one, the main logo is shown on a white badge.")

    def clean(self):
        from .branding import validate_logo
        data = super().clean()
        for field in ("logo", "logo_dark"):
            f = data.get(field)
            if f:
                try:
                    data[field] = validate_logo(f)
                except forms.ValidationError as e:
                    self.add_error(field, e)
        return data


class StorageLocationForm(forms.Form):
    """Where uploaded files should be kept (System → Storage)."""

    kind = forms.ChoiceField(label="Keep files in", widget=forms.RadioSelect, choices=[
        ("local", "A folder on this server"), ("s3", "Cloud storage (Amazon S3 and compatible services)")])
    path = forms.CharField(label="Folder", required=False, max_length=300,
                           help_text="The default is <code>/data/files</code> (the data folder). For another disk or a NAS share, "
                                     "mount it into the container first — see <em>Help → Installation → Storage</em>.")
    provider = forms.ChoiceField(label="Service", required=False, choices=[])
    bucket = forms.CharField(label="Bucket name", required=False, max_length=63)
    region = forms.CharField(required=False, max_length=40)
    endpoint = forms.CharField(label="Endpoint address", required=False, max_length=200,
                               help_text="Filled in for you for most services.")
    prefix = forms.CharField(label="Folder inside the bucket", required=False, max_length=100, initial="workbench",
                             help_text="Lets several installations share one bucket.")
    access_key = forms.CharField(label="Access key ID", required=False, max_length=200)
    secret_key = forms.CharField(label="Secret access key", required=False, max_length=200,
                                 widget=forms.PasswordInput(render_value=False))

    def __init__(self, *args, saved=None, **kwargs):
        from .storage import PROVIDERS
        self.saved = saved or {}
        super().__init__(*args, **kwargs)
        self.fields["provider"].choices = [(k, v["label"]) for k, v in PROVIDERS.items()]
        self.fields["provider"].widget.attrs["data-provider-select"] = "1"
        for name in ("bucket", "region", "endpoint", "prefix", "access_key"):
            self.fields[name].widget.attrs.update(autocomplete="off", spellcheck="false")
        if self.saved.get("secret_key"):
            self.fields["secret_key"].help_text = "Saved. Leave blank to keep it."
        self.fields["access_key"].help_text = "Leave both keys blank on AWS if the server has an IAM role."
        # Needed for cloud storage (checked in clean()), so don't label them optional.
        for name in ("provider", "bucket", "region", "endpoint", "access_key", "secret_key"):
            self.fields[name].conditionally_required = True

    def clean(self):
        import re

        from .storage import PROVIDERS
        d = super().clean()
        if d.get("kind") == "local":
            path = (d.get("path") or "").strip() or str(settings.FILES_DIR)
            if not path.startswith("/"):
                self.add_error("path", "Use a full path starting with /, e.g. /mnt/storage/workbench.")
            elif any(path.rstrip("/") == str(p).rstrip("/") or path.startswith(str(p).rstrip("/") + "/")
                     for p in (settings.BACKUP_DIR, settings.DB_DIR)):
                self.add_error("path", "Pick a folder outside the database and backup folders.")
            d["path"] = path.rstrip("/") or "/"
            return d
        provider = d.get("provider") or "aws"
        info = PROVIDERS.get(provider)
        if not info:
            self.add_error("provider", "Choose a service.")
            return d
        bucket = (d.get("bucket") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket):
            self.add_error("bucket", "Bucket names are 3–63 lowercase letters, numbers, dots and hyphens.")
        d["bucket"] = bucket
        d["region"] = (d.get("region") or "").strip() or info.get("region", "")
        tmpl = info.get("endpoint", "")
        if (provider == "aws" or "{region}" in tmpl) and not d["region"]:
            self.add_error("region", "Enter the bucket's region.")
        endpoint = (d.get("endpoint") or "").strip().rstrip("/")
        if endpoint:
            if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?(/[\w.\-/]*)?", endpoint):
                self.add_error("endpoint", "Enter an address like https://s3.example.com.")
            elif endpoint.startswith("http://") and provider != "minio":
                self.add_error("endpoint", "Use https:// for internet services.")
            elif "ACCOUNT_ID" in endpoint:
                self.add_error("endpoint", "Replace ACCOUNT_ID with your Cloudflare account ID.")
        elif provider in ("r2", "minio"):
            self.add_error("endpoint", "Enter the service's endpoint address.")
        d["endpoint"] = endpoint
        d["prefix"] = re.sub(r"[^A-Za-z0-9._\-/]", "", (d.get("prefix") or "").strip().strip("/"))
        d["access_key"] = (d.get("access_key") or "").strip()
        secret = d.get("secret_key") or ""
        if not secret and d["access_key"] and d["access_key"] == self.saved.get("access_key"):
            secret = self.saved.get("secret_key", "")  # keep the saved secret
        d["secret_key"] = secret
        if d["access_key"] and not secret:
            self.add_error("secret_key", "Enter the secret access key.")
        if not d["access_key"] and provider != "aws":
            self.add_error("access_key", "This service needs an access key.")
        return d

    def config(self):
        d = self.cleaned_data
        if d["kind"] == "local":
            return {"kind": "local", "path": d["path"]}
        return {"kind": "s3", **{k: d[k] for k in ("provider", "bucket", "region", "endpoint", "prefix", "access_key", "secret_key")}}
