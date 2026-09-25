import zoneinfo

from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import User

TIME_ZONE_CHOICES = [(tz, tz) for tz in sorted(zoneinfo.available_timezones()) if "/" in tz and not tz.startswith("Etc/")]


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class CodeForm(forms.Form):
    code = forms.CharField(
        max_length=20, label="Code",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "one-time-code", "inputmode": "numeric", "placeholder": "123456"}),
    )


class ProfileForm(forms.ModelForm):
    time_zone = forms.ChoiceField(choices=TIME_ZONE_CHOICES)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "job_title", "time_zone", "github_username"]


class UserAdminForm(forms.ModelForm):
    time_zone = forms.ChoiceField(choices=TIME_ZONE_CHOICES, initial="Asia/Karachi")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "job_title", "role", "time_zone", "github_username", "is_active"]
        help_texts = {
            "username": "Short login name, e.g. ali.khan. Letters, digits and @/./+/-/_ only.",
            "is_active": "Untick to block this person from signing in (use this when someone leaves).",
        }


class SetPasswordForm(forms.Form):
    password1 = forms.CharField(label="New password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                help_text="At least 12 characters. Avoid common words and all-number passwords.")
    password2 = forms.CharField(label="Repeat new password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("The two passwords don't match.")
        if p1:
            validate_password(p1, self.user)
        return data


class ChangePasswordForm(SetPasswordForm):
    current = forms.CharField(label="Current password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    field_order = ["current", "password1", "password2"]

    def clean_current(self):
        if not self.user.check_password(self.cleaned_data["current"]):
            raise forms.ValidationError("That isn't your current password.")
        return self.cleaned_data["current"]
