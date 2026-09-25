import base64
import io
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings
from apps.core.utils import admin_required, audit

from .forms import ChangePasswordForm, CodeForm, LoginForm, ProfileForm, SetPasswordForm, UserAdminForm
from .models import RecoveryCode, User


def _safe_next(request, default="core:dashboard"):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return reverse(default)


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"].strip()
        candidate = User.objects.filter(username__iexact=username).first()
        if candidate and candidate.is_locked:
            audit(request, "login.blocked_locked", candidate, actor=candidate)
            form.add_error(None, "This account is temporarily locked after too many failed attempts. Try again later or ask an administrator.")
        else:
            user = authenticate(request, username=candidate.username if candidate else username,
                                password=form.cleaned_data["password"])
            if user is not None:
                user.failed_logins = 0
                user.locked_until = None
                user.save(update_fields=["failed_logins", "locked_until"])
                login(request, user)
                request.session["mfa_verified"] = False
                audit(request, "login.password_ok", user)
                if user.mfa_enabled:
                    return redirect(f"{reverse('accounts:mfa_verify')}?next={_safe_next(request)}")
                return redirect(_safe_next(request))
            if candidate:
                candidate.failed_logins += 1
                if candidate.failed_logins >= settings.LOGIN_MAX_ATTEMPTS:
                    candidate.locked_until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
                    candidate.failed_logins = 0
                    audit(request, "login.locked", candidate, actor=candidate)
                candidate.save(update_fields=["failed_logins", "locked_until"])
            audit(request, "login.failed", candidate, username=username)
            form.add_error(None, "Wrong username or password.")
    return render(request, "accounts/login.html", {"form": form, "next": request.GET.get("next", "")})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit(request, "logout", request.user)
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect("accounts:login")


def _qr_svg(uri):
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@login_required
def mfa_setup(request):
    user = request.user
    if user.mfa_enabled and request.session.get("mfa_verified"):
        messages.info(request, "Two-factor authentication is already on for your account.")
        return redirect("accounts:security")
    if user.mfa_enabled:
        return redirect("accounts:mfa_verify")

    secret = request.session.get("mfa_pending_secret")
    if not secret:
        secret = pyotp.random_base32()
        request.session["mfa_pending_secret"] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user.username, issuer_name=SiteSettings.load().company_name)

    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if user.verify_totp(form.cleaned_data["code"], secret=secret):
            user.mfa_secret = secret
            user.mfa_enabled = True
            user.save()
            del request.session["mfa_pending_secret"]
            request.session["mfa_verified"] = True
            codes = RecoveryCode.generate_for(user)
            request.session["show_recovery_codes"] = codes
            audit(request, "mfa.enabled", user)
            return redirect("accounts:recovery_codes")
        form.add_error("code", "That code didn't match. Check your phone's time is set automatically and try the newest code.")
    return render(request, "accounts/mfa_setup.html", {
        "form": form, "qr": _qr_svg(uri), "secret": secret,
    })


@login_required
def mfa_verify(request):
    user = request.user
    if not user.mfa_enabled:
        return redirect("accounts:mfa_setup")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = form.cleaned_data["code"].replace(" ", "")
        ok = user.verify_totp(code)
        used_recovery = False
        if not ok and "-" in code:
            ok = used_recovery = RecoveryCode.use(user, code)
        if ok:
            request.session.cycle_key()
            request.session["mfa_verified"] = True
            audit(request, "login.mfa_ok", user, recovery_code=used_recovery)
            if used_recovery:
                left = user.recovery_codes.filter(used_at__isnull=True).count()
                messages.warning(request, f"You signed in with a recovery code. {left} left. Generate new ones from Security settings.")
            return redirect(_safe_next(request))
        audit(request, "login.mfa_failed", user)
        form.add_error("code", "That code didn't work. Try the newest code from your authenticator app, or a recovery code.")
    return render(request, "accounts/mfa_verify.html", {"form": form, "next": request.GET.get("next", "")})


@login_required
def recovery_codes(request):
    codes = request.session.pop("show_recovery_codes", None)
    if not codes:
        return redirect("accounts:security")
    return render(request, "accounts/recovery_codes.html", {"codes": codes})


@login_required
@require_POST
def regenerate_recovery_codes(request):
    request.session["show_recovery_codes"] = RecoveryCode.generate_for(request.user)
    audit(request, "mfa.recovery_regenerated", request.user)
    return redirect("accounts:recovery_codes")


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile saved.")
        return redirect("accounts:profile")
    return render(request, "accounts/profile.html", {"form": form})


@login_required
def security(request):
    form = ChangePasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        request.user.set_password(form.cleaned_data["password1"])
        request.user.save()
        update_session_auth_hash(request, request.user)
        audit(request, "password.changed", request.user)
        messages.success(request, "Password changed.")
        return redirect("accounts:security")
    remaining = request.user.recovery_codes.filter(used_at__isnull=True).count()
    return render(request, "accounts/security.html", {"form": form, "remaining": remaining})


# --- Administration ---------------------------------------------------------

def _set_password_link(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    # Built from the configured site address (not the request's Host header),
    # so a forged Host header can't redirect password links elsewhere.
    return SiteSettings.load().absolute_url(reverse("accounts:set_password", args=[uid, token]), request)


@admin_required
def user_list(request):
    users = User.objects.all().order_by("-is_active", "first_name", "username")
    return render(request, "accounts/user_list.html", {"users": users, "roles": User.ROLE_HELP})


@admin_required
def user_edit(request, pk=None):
    obj = get_object_or_404(User, pk=pk) if pk else None
    form = UserAdminForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        creating = obj is None
        user = form.save(commit=False)
        if creating:
            user.set_unusable_password()
        user.save()
        audit(request, "user.created" if creating else "user.updated", user, role=user.role, active=user.is_active)
        if creating:
            request.session["invite_link"] = {"user": user.display_name, "url": _set_password_link(request, user)}
            return redirect("accounts:user_invite", pk=user.pk)
        messages.success(request, f"Saved {user.display_name}.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "obj": obj, "roles": User.ROLE_HELP})


@admin_required
def user_invite(request, pk):
    user = get_object_or_404(User, pk=pk)
    site = SiteSettings.load()
    if request.method == "POST" and request.POST.get("action") == "email":
        from apps.core import email as mail
        url = _set_password_link(request, user)
        try:
            mail.send(user.email, "Your Workbench account",
                      f"Hi {user.first_name or user.username},\n\n{request.user.display_name} has set up a Workbench account for you.\n"
                      f"Your username is: {user.username}\n\nChoose your password here (the link works once and expires in 3 days):\n{url}\n\n"
                      "After that you'll set up two-factor sign-in with an authenticator app on your phone.", site)
            audit(request, "user.invite_emailed", user)
            messages.success(request, f"Sign-in link emailed to {user.email}.")
        except Exception as e:
            messages.error(request, f"Couldn't send the email: {e}")
        return redirect("accounts:user_list")
    link = request.session.pop("invite_link", None)
    if not link:
        link = {"user": user.display_name, "url": _set_password_link(request, user)}
        audit(request, "user.password_link", user)
    return render(request, "accounts/user_invite.html", {"link": link, "target": user, "can_email": site.email_ready and bool(user.email)})


@admin_required
@require_POST
def user_reset_mfa(request, pk):
    user = get_object_or_404(User, pk=pk)
    user.mfa_enabled = False
    user.mfa_secret = ""
    user.save()
    user.recovery_codes.all().delete()
    audit(request, "mfa.reset_by_admin", user)
    messages.success(request, f"Two-factor authentication reset for {user.display_name}. They will set it up again at next sign-in.")
    return redirect("accounts:user_list")


@admin_required
@require_POST
def user_unlock(request, pk):
    user = get_object_or_404(User, pk=pk)
    user.locked_until = None
    user.failed_logins = 0
    user.save(update_fields=["locked_until", "failed_logins"])
    audit(request, "user.unlocked", user)
    messages.success(request, f"{user.display_name} is unlocked.")
    return redirect("accounts:user_list")


def set_password(request, uidb64, token):
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "accounts/link_invalid.html", status=400)
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user.set_password(form.cleaned_data["password1"])
        user.locked_until = None
        user.failed_logins = 0
        user.save()
        audit(request, "password.set_from_link", user, actor=user)
        messages.success(request, "Password set. Sign in now — you'll be asked to set up two-factor authentication.")
        return redirect("accounts:login")
    return render(request, "accounts/set_password.html", {"form": form, "target": user})
