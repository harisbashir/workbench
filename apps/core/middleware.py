class SecurityHeadersMiddleware:
    """Adds a strict Content-Security-Policy and related headers.

    Workbench loads no third-party scripts, styles or fonts, so the policy only
    allows resources from this site.
    """

    CSP = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.user.is_authenticated if hasattr(request, "user") else False:
            response.setdefault("Cache-Control", "no-store")
        return response


class FirstRunSetupMiddleware:
    """Until the first administrator exists, every page leads to the setup page."""

    _done = False  # cached once setup is complete, so it costs nothing afterwards
    OPEN_PREFIXES = ("/setup/", "/static/", "/healthz", "/branding/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not FirstRunSetupMiddleware._done:
            from django.shortcuts import redirect

            from apps.accounts.models import User

            if User.objects.exists():
                FirstRunSetupMiddleware._done = True
            elif not request.path.startswith(self.OPEN_PREFIXES):
                return redirect("core:setup")
        return self.get_response(request)
