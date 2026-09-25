import zoneinfo

from django import forms
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
