"""Optional email, configured in System → Email. Everything works without it."""
import logging
from collections import defaultdict

from django.core.mail import EmailMessage, get_connection

from .models import Notification, SiteSettings

log = logging.getLogger("workbench")


def connection_for(site):
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=site.smtp_host, port=site.smtp_port, username=site.smtp_username or None,
        password=site.smtp_password or None, use_tls=site.smtp_use_tls, timeout=15,
    )


def send(to, subject, body, site=None):
    site = site or SiteSettings.load()
    if not site.email_ready:
        return False
    msg = EmailMessage(subject=f"[{site.company_name}] {subject}", body=body, from_email=site.email_from,
                       to=[to] if isinstance(to, str) else to, connection=connection_for(site))
    msg.send(fail_silently=False)
    return True


def send_pending_notifications():
    """Emails unread notifications, one message per person. Called by the scheduler."""
    from django.utils import timezone

    site = SiteSettings.load()
    # Give up on anything older than a day (e.g. while the mail server was down).
    Notification.objects.filter(emailed=False, created_at__lt=timezone.now() - timezone.timedelta(days=1)).update(emailed=True)
    pending = Notification.objects.filter(emailed=False, is_read=False).select_related("user")
    if not site.email_ready:
        pending.update(emailed=True)  # don't pile up a backlog to send later
        return 0
    by_user = defaultdict(list)
    for n in pending:
        by_user[n.user].append(n)
    sent = 0
    for user, items in by_user.items():
        ids = [n.pk for n in items]
        if not (user.is_active and user.email and user.email_notifications):
            Notification.objects.filter(pk__in=ids).update(emailed=True)
            continue
        lines = [f"• {n.text}\n  {site.absolute_url(n.url) if n.url.startswith('/') else n.url}" for n in items]
        subject = items[0].text if len(items) == 1 else f"{len(items)} new notifications"
        body = f"Hi {user.first_name or user.username},\n\n" + "\n\n".join(lines) + \
               f"\n\nOpen Workbench: {site.absolute_url('/')}\nTurn these emails off in your profile."
        try:
            send(user.email, subject, body, site)
            sent += 1
        except Exception:
            log.exception("Couldn't email notifications to %s", user.username)
            continue
        Notification.objects.filter(pk__in=ids).update(emailed=True)
    return sent
