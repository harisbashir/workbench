from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.chat.formatting import render as render_text

register = template.Library()

# Minimal outline icons (24x24, stroke based) drawn in-house.
ICONS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "check": '<path d="M5 12l5 5L20 7"/>',
    "folder": '<path d="M3 6a2 2 0 012-2h4l2 2h8a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
    "chat": '<path d="M4 5h16v11H8l-4 4z"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "factory": '<path d="M3 21V10l6 4V10l6 4V6l6 3v12z"/><path d="M7 17h2M12 17h2M17 17h2"/>',
    "git": '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 8v8M18 10c0 4-6 3-10 6"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 015 .5c0 1.5-2.5 2-2.5 3.5M12 17h.01"/>',
    "users": '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3 3-5 6-5s6 2 6 5"/><circle cx="17" cy="9" r="2"/><path d="M17 14c2 0 4 1.5 4 4"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/>',
    "bell": '<path d="M6 16V11a6 6 0 1112 0v5l2 2H4z"/><path d="M10 20a2 2 0 004 0"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    "board": '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="10" rx="1"/><rect x="17" y="4" width="4" height="13" rx="1"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
    "download": '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
    "file": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 18h.01"/>',
    "box": '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
    "truck": '<path d="M2 6h12v10H2zM14 10h4l3 3v3h-7"/><circle cx="6" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>',
    "log": '<path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
    "logout": '<path d="M15 4h4v16h-4M10 17l5-5-5-5M15 12H3"/>',
    "compare": '<path d="M8 4v16M16 4v16M4 8h4M16 16h4"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "pr": '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/><path d="M6 8v8M18 16V9a3 3 0 00-3-3h-4M13 4l-2 2 2 2"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="1"/><path d="M8 11V7a4 4 0 018 0v4"/>',
    "send": '<path d="M4 12l16-8-6 16-2-6z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    "cpu": '<rect x="7" y="7" width="10" height="10" rx="1"/><path d="M10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-8 8"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "folder-plus": '<path d="M3 6a2 2 0 012-2h4l2 2h8a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><path d="M12 10v6M9 13h6"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "restore": '<path d="M4 12a8 8 0 108-8H8M8 4L5 7l3 3"/>',
    "paperclip": '<path d="M20 11l-8 8a5 5 0 01-7-7l8-8a3 3 0 014 4l-8 8a1 1 0 01-2-2l7-7"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
    "hash": '<path d="M5 9h14M5 15h14M10 4L8 20M16 4l-2 16"/>',
    "cloud": '<path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.4 1.5A3.3 3.3 0 0 0 7 18z"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "tool": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.1L3 17.7 6.3 21l6.3-6.3a4 4 0 0 0 5.1-5.4l-2.6 2.6-2.4-.6-.6-2.4 2.6-2.6z"/>',
    "cube": '<path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M3 7l9 5 9-5M12 12v10"/>',
    "diagram": '<rect x="3" y="4" width="7" height="5" rx="1"/><rect x="14" y="4" width="7" height="5" rx="1"/><rect x="8.5" y="15" width="7" height="5" rx="1"/><path d="M10 6.5h4M6.5 9v3.5h11V9M12 12.5V15"/>',
    "pcb": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><circle cx="16" cy="16" r="1.5"/><path d="M8 9.5V14h6.5M16 14.5V8h-4"/>',
    "lock-open": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
}


@register.simple_tag
def icon(name, size=18, cls=""):
    path = ICONS.get(name, ICONS["help"])
    return mark_safe(
        f'<svg class="icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


@register.filter
def chat_format(text):
    return render_text(text or "")


@register.filter
def filesize(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@register.simple_tag
def status_badge(value, label):
    return format_html('<span class="badge badge-{}">{}</span>', value or "none", label)


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


SYMBOLS = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥", "PKR": "Rs ", "INR": "₹", "AUD": "A$"}


@register.filter
def money(v, currency=None):
    """Amount in the given currency (default: the base currency, WORKBENCH_CURRENCY)."""
    from django.conf import settings
    code = (currency or getattr(settings, "WORKBENCH_CURRENCY", "") or "USD").upper()
    try:
        amount = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"
    sym = SYMBOLS.get(code)
    return f"{sym}{amount}" if sym else f"{amount} {code}"


@register.filter
def percent_step(v):
    """Round to nearest 5 so the value can be used in a CSS class (no inline styles under our CSP)."""
    try:
        v = max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return 0
    return int(round(v / 5.0) * 5)


@register.filter
def channel_label(channel, user):
    return channel.label_for(user)


@register.filter
def recommended(firmware, revision):
    """Newest released firmware version compatible with a board revision."""
    return firmware.recommended_for(revision)


@register.filter
def storage_where(cfg):
    from apps.core.storage import describe
    return describe(cfg)["where"] if cfg else ""


@register.simple_tag(takes_context=True)
def admin_link(context, url_name, label, *args):
    """A link for administrators; plain bold text for everyone else (who'd get "not allowed")."""
    from django.urls import reverse
    user = context.get("user")
    if user is not None and getattr(user, "is_admin_role", False):
        return format_html('<a href="{}">{}</a>', reverse(url_name, args=args), label)
    return format_html("<strong>{}</strong>", label)
