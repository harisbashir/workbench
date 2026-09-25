from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

# Paths a signed-in user may visit before finishing two-factor authentication.
ALLOWED_PREFIXES = ("/static/", "/integrations/github/webhook")


class MFARequiredMiddleware:
    """Forces two-factor enrolment and verification for every session.

    - Signed in, MFA not set up yet   -> sent to the setup page.
    - Signed in, MFA set up, not yet verified this session -> sent to the code page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            path = request.path
            allowed = {
                reverse("accounts:mfa_setup"),
                reverse("accounts:mfa_verify"),
                reverse("accounts:logout"),
            }
            if path not in allowed and not path.startswith(ALLOWED_PREFIXES):
                if user.mfa_enabled and not request.session.get("mfa_verified"):
                    return redirect(f"{reverse('accounts:mfa_verify')}?next={path}")
                if not user.mfa_enabled and settings.WORKBENCH_REQUIRE_MFA:
                    return redirect("accounts:mfa_setup")
        return self.get_response(request)


class UserTimezoneMiddleware:
    """Shows all dates and times in the signed-in user's own time zone."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import zoneinfo

        from django.utils import timezone

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.time_zone:
            try:
                timezone.activate(zoneinfo.ZoneInfo(user.time_zone))
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                timezone.deactivate()
        else:
            timezone.deactivate()
        return self.get_response(request)
