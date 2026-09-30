from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import AuditLog, Notification


def client_ip(request):
    # Only trust X-Forwarded-For when explicitly deployed behind a proxy.
    from django.conf import settings
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        fwd = request.META.get("HTTP_X_FORWARDED_FOR")
        if fwd:
            # The right-most address is the one added by our own proxy (Caddy); anything
            # to its left was sent by the client and can be forged.
            return fwd.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR")


def audit(request, action, obj=None, actor=None, **details):
    user = actor
    if user is None and request is not None and getattr(request, "user", None) and request.user.is_authenticated:
        user = request.user
    AuditLog.objects.create(
        actor=user,
        action=action,
        object_type=obj.__class__.__name__ if obj is not None else "",
        object_id=str(getattr(obj, "pk", "") or ""),
        object_repr=str(obj)[:255] if obj is not None else "",
        details={k: str(v) for k, v in details.items()},
        ip_address=client_ip(request) if request is not None else None,
    )


def notify(user, text, url=""):
    if user is None:
        return
    Notification.objects.create(user=user, text=text[:255], url=url)


def role_required(check, message="You don't have permission to do that."):
    """Decorator: `check` is a function(user) -> bool."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not check(request.user):
                raise PermissionDenied(message)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


manager_required = role_required(lambda u: u.can_manage_projects, "Only leads and administrators can do that.")
procurement_required = role_required(lambda u: u.can_manage_procurement, "Only procurement, leads and administrators can do that.")
admin_required = role_required(lambda u: u.is_admin_role, "Only administrators can do that.")
writer_required = role_required(lambda u: not u.is_read_only, "Your account is read-only.")
