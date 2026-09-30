from datetime import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import date as date_filter
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.core.utils import notify

from .formatting import MENTION_RE, clean_text, render as render_text

MAX_BODY = 8000
from .models import Channel, ChannelRead, Message

PAGE = 60


def unread_total(user):
    return sum(unread_counts(user).values())


def unread_counts(user):
    """{channel id: unread messages} for the conversations a person follows — one query."""
    followed = Channel.objects.visible_to(user).filter(
        Q(members=user) | Q(kind=Channel.Kind.PROJECT) | Q(kind=Channel.Kind.DIRECT))
    last_read = ChannelRead.objects.filter(user=user, channel_id=OuterRef("channel_id")).values("last_read_id")[:1]
    rows = (Message.objects.filter(channel_id__in=followed.values("pk"), is_deleted=False)
            .exclude(author=user)
            .annotate(last_read=Coalesce(Subquery(last_read), Value(0)))
            .filter(id__gt=F("last_read"))
            .order_by().values("channel_id").annotate(n=Count("id")))
    return {r["channel_id"]: r["n"] for r in rows if r["n"]}


def _int_param(request, name, default=0):
    try:
        return max(0, int(request.GET.get(name) or default))
    except (TypeError, ValueError):
        return default


def _serialize(msg, user):
    return {
        "id": msg.id,
        "author": msg.author_name,
        "initials": msg.author.initials if msg.author else ("GH" if msg.kind == "github" else "WB"),
        "kind": msg.kind,
        "mine": msg.author_id == user.id,
        # Raw text of your own messages, so the edit box shows **bold**/`code` markers, not rendered text.
        "body": msg.body if msg.author_id == user.id and not msg.is_deleted else "",
        "html": str(render_text(msg.body)) if not msg.is_deleted else "<em>message deleted</em>",
        "url": msg.url,
        "time": date_filter(timezone.localtime(msg.created_at), "H:i"),
        "day": date_filter(timezone.localtime(msg.created_at), "l j F"),
        "edited": bool(msg.edited_at),
        "deleted": msg.is_deleted,
    }


def _mark_read(user, channel):
    last = channel.messages.aggregate(m=Max("id"))["m"] or 0
    ChannelRead.objects.update_or_create(user=user, channel=channel, defaults={"last_read_id": last})


def _sidebar(user):
    visible = Channel.objects.visible_to(user).select_related("project").prefetch_related("members")
    counts = unread_counts(user)
    joined, directs, projects = [], [], []
    for ch in visible:
        ch.unread = counts.get(ch.id, 0)
        ch.label = ch.label_for(user)
        if ch.kind == Channel.Kind.DIRECT:
            if user in ch.members.all():
                directs.append(ch)
        elif ch.kind == Channel.Kind.PROJECT:
            projects.append(ch)  # visible_to() already limits these to projects the person can see
        elif user in ch.members.all() or ch.kind == Channel.Kind.PRIVATE:
            joined.append(ch)
    others = Channel.objects.filter(kind=Channel.Kind.PUBLIC, is_archived=False).exclude(members=user)
    return {"joined": joined, "directs": directs, "project_channels": projects, "discover": others}


@login_required
def index(request):
    side = _sidebar(request.user)
    first = (side["project_channels"] or side["joined"] or side["directs"] or [None])[0]
    if first:
        return redirect(first)
    return render(request, "chat/empty.html", side)


@login_required
def channel_view(request, slug):
    channel = get_object_or_404(Channel.objects.select_related("project"), slug=slug)
    if not channel.can_view(request.user):
        raise PermissionDenied("This is a private conversation.")
    msgs = list(channel.messages.select_related("author").order_by("-id")[:PAGE])[::-1]
    _mark_read(request.user, channel)
    ctx = _sidebar(request.user)
    ctx.update({
        "channel": channel,
        "label": channel.label_for(request.user),
        "initial": [_serialize(m, request.user) for m in msgs],
        "can_post": channel.can_post(request.user),
        "is_member": channel.members.filter(pk=request.user.pk).exists(),
        "people": User.objects.filter(is_active=True).exclude(pk=request.user.pk),
        "channel_members": channel.members.all() if channel.kind != Channel.Kind.PROJECT else
            User.objects.filter(Q(projects=channel.project) | Q(led_projects=channel.project)).distinct(),
    })
    return render(request, "chat/channel.html", ctx)


@login_required
def poll(request, slug):
    """Returns messages newer than ?after=<id>. The page calls this every few seconds."""
    channel = get_object_or_404(Channel, slug=slug)
    if not channel.can_view(request.user):
        raise PermissionDenied
    after = _int_param(request, "after")
    before = _int_param(request, "before")
    qs = channel.messages.select_related("author")
    if before:
        msgs = list(qs.filter(id__lt=before).order_by("-id")[:PAGE])[::-1]
    else:
        msgs = list(qs.filter(id__gt=after).order_by("id")[:200])
        if msgs:
            _mark_read(request.user, channel)
    # Also send back messages that were edited or deleted since the last poll.
    changed = []
    since = request.GET.get("since")
    if since and not before:
        try:
            since_dt = datetime.fromisoformat(since)
            if timezone.is_naive(since_dt):
                since_dt = timezone.make_aware(since_dt)
            changed = [_serialize(m, request.user) for m in qs.filter(id__lte=after, edited_at__gt=since_dt)]
        except (ValueError, OverflowError):
            pass
    return JsonResponse({
        "messages": [_serialize(m, request.user) for m in msgs],
        "changed": changed,
        "now": timezone.now().isoformat(),
        "unread_total": unread_total(request.user),
    })


@login_required
@require_POST
def post_message(request, slug):
    channel = get_object_or_404(Channel, slug=slug)
    if not channel.can_post(request.user):
        raise PermissionDenied("You can't post in this conversation.")
    body = clean_text(request.POST.get("body")).strip()
    if not body or len(body) > MAX_BODY:
        return JsonResponse({"ok": False, "error": "Message is empty or too long."}, status=400)
    msg = Message.objects.create(channel=channel, author=request.user, body=body)
    if channel.kind in (Channel.Kind.PUBLIC, Channel.Kind.PRIVATE):
        channel.members.add(request.user)
    for username in set(MENTION_RE.findall(body)):
        u = User.objects.filter(username__iexact=username, is_active=True).exclude(pk=request.user.pk).first()
        if u and channel.can_view(u):
            notify(u, f"{request.user.display_name} mentioned you in {channel.label_for(u)}", channel.get_absolute_url())
    _mark_read(request.user, channel)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True, "message": _serialize(msg, request.user)})
    return redirect(channel)


@login_required
@require_POST
def edit_message(request, pk):
    msg = get_object_or_404(Message.objects.select_related("channel__project"), pk=pk, author=request.user,
                            kind=Message.Kind.USER, is_deleted=False)
    if not msg.channel.can_post(request.user):
        raise PermissionDenied("You can't change messages in this conversation.")
    action = request.POST.get("action")
    if action == "delete":
        msg.is_deleted = True
        msg.body = ""
    else:
        body = clean_text(request.POST.get("body")).strip()
        if not body or len(body) > MAX_BODY:
            return JsonResponse({"ok": False, "error": "Message is empty or too long."}, status=400)
        msg.body = body
    msg.edited_at = timezone.now()
    msg.save()
    return JsonResponse({"ok": True, "message": _serialize(msg, request.user)})


class ChannelForm(forms.ModelForm):
    kind = forms.ChoiceField(choices=[(Channel.Kind.PUBLIC, Channel.Kind.PUBLIC.label), (Channel.Kind.PRIVATE, Channel.Kind.PRIVATE.label)],
                             widget=forms.RadioSelect, initial=Channel.Kind.PUBLIC, label="Who can see it")
    members = forms.ModelMultipleChoiceField(queryset=User.objects.filter(is_active=True), required=False,
                                             widget=forms.CheckboxSelectMultiple, help_text="For private channels, choose who's in it.")

    class Meta:
        model = Channel
        fields = ["name", "topic", "kind", "members"]
        help_texts = {"name": "Short and lowercase works best, e.g. firmware, pcb-reviews, suppliers."}


@login_required
def channel_create(request):
    if request.user.is_read_only:
        raise PermissionDenied
    form = ChannelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ch = form.save(commit=False)
        ch.slug = Channel.unique_slug(ch.name)
        ch.created_by = request.user
        ch.save()
        form.save_m2m()
        ch.members.add(request.user)
        Message.objects.create(channel=ch, kind=Message.Kind.SYSTEM, body=f"{request.user.display_name} created this channel.")
        return redirect(ch)
    return render(request, "chat/channel_form.html", {"form": form, **_sidebar(request.user)})


@login_required
@require_POST
def channel_join(request, slug):
    ch = get_object_or_404(Channel, slug=slug, kind=Channel.Kind.PUBLIC)
    ch.members.add(request.user)
    return redirect(ch)


@login_required
@require_POST
def channel_leave(request, slug):
    ch = get_object_or_404(Channel, slug=slug)
    if ch.kind in (Channel.Kind.PUBLIC, Channel.Kind.PRIVATE):
        ch.members.remove(request.user)
        messages.info(request, f"You left #{ch.name}.")
    return redirect("chat:index")


@login_required
@require_POST
def direct(request):
    other = get_object_or_404(User, pk=request.POST.get("user"), is_active=True)
    return redirect(Channel.direct_between(request.user, other))


@login_required
def unread(request):
    return JsonResponse({"unread_total": unread_total(request.user)})


@login_required
@require_POST
def upload(request, slug):
    """Share a file in a channel. It's stored in the project's Files (or Shared for public channels)."""
    from apps.files.models import Folder
    from apps.files.views import store_upload, validate_upload

    channel = get_object_or_404(Channel.objects.select_related("project"), slug=slug)
    if not channel.can_post(request.user):
        raise PermissionDenied
    if channel.kind not in (Channel.Kind.PROJECT, Channel.Kind.PUBLIC):
        messages.error(request, "Files can be shared in project and public channels. For private conversations, upload to the project's Files and paste the link.")
        return redirect(channel)
    project = channel.project if channel.kind == Channel.Kind.PROJECT else None
    folder = Folder.get_or_create_path(project, "Chat uploads", *([] if project else [channel.name]), user=request.user)
    for f in request.FILES.getlist("files")[:10]:
        err = validate_upload(f)
        if err:
            messages.error(request, err)
            continue
        # Each share is its own document, so a same-named file never replaces someone else's.
        doc, v, _ = store_upload(f, project=project, folder=folder, user=request.user, request=request, as_new=True)
        size = f"{doc.size / 1024 / 1024:.1f} MB" if doc.size > 1024 * 1024 else f"{max(doc.size // 1024, 1)} KB"
        Message.objects.create(channel=channel, author=request.user, body=f"shared a file: **{doc.name}** ({size})",
                               url=doc.get_absolute_url())
    return redirect(channel)
