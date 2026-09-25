from django.conf import settings


def workbench(request):
    from .models import SiteSettings

    site = SiteSettings.load()
    ctx = {"COMPANY_NAME": site.company_name, "SITE": site, "https_on": settings.HTTPS, "debug_mode": settings.DEBUG}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and request.session.get("mfa_verified"):
        from apps.chat.views import unread_total

        ctx["unread_notifications"] = user.notifications.filter(is_read=False).count()
        ctx["unread_chat"] = unread_total(user)
        path = request.path
        for section in ("projects", "chat", "parts", "production", "integrations", "help", "accounts", "audit", "my-work",
                        "files", "time", "system"):
            if path.startswith(f"/{section}"):
                ctx["section"] = section
                break
        else:
            ctx["section"] = "home"
    return ctx
