import json
import logging
import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.projects.models import Project

from . import github
from .models import PullRequest, WebhookDelivery

log = logging.getLogger("workbench")


def webhook_secret():
    return os.environ.get("WORKBENCH_GITHUB_WEBHOOK_SECRET", "")


@csrf_exempt  # GitHub can't send a CSRF token; the HMAC signature authenticates it instead.
@require_POST
def github_webhook(request):
    body = request.body
    if len(body) > 5 * 1024 * 1024:
        return HttpResponseBadRequest("Payload too large")
    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")[:64]
    if not github.verify_signature(webhook_secret(), body, request.headers.get("X-Hub-Signature-256", "")):
        log.warning("Rejected GitHub webhook with a bad or missing signature (delivery %s)", delivery)
        # Record a few rejections so admins can spot a mismatched secret, but cap it
        # so unauthenticated requests can't fill the database.
        recent = WebhookDelivery.objects.filter(status="rejected", received_at__gte=timezone.now() - timedelta(minutes=10)).count()
        if delivery and recent < 20:
            WebhookDelivery.objects.get_or_create(delivery_id=f"rejected-{delivery}", defaults={
                "event": event[:40], "status": "rejected", "message": "Signature didn't match the webhook secret."})
        return HttpResponseForbidden("Invalid signature")
    if not delivery or not event:
        return HttpResponseBadRequest("Missing GitHub headers")
    try:
        with transaction.atomic():
            record = WebhookDelivery.objects.create(delivery_id=delivery, event=event[:40], status="received")
    except IntegrityError:
        return HttpResponse("Duplicate delivery ignored")  # replay protection
    try:
        payload = json.loads(body)
        record.repository = ((payload.get("repository") or {}).get("full_name") or "")[:200]
        with transaction.atomic():
            record.status, record.message = github.handle(event, payload)
    except Exception as exc:  # never leak internals to the caller
        log.exception("GitHub webhook failed")
        record.status, record.message = "error", str(exc)[:300]
    record.save()
    return JsonResponse({"status": record.status, "message": record.message})


@login_required
def overview(request):
    visible = Project.objects.visible_to(request.user)
    return render(request, "integrations/overview.html", {
        "webhook_url": settings.SITE_URL.rstrip("/") + reverse("integrations:github_webhook"),
        "secret_set": bool(webhook_secret()),
        "projects": visible.order_by("key"),
        "pulls": PullRequest.objects.filter(project__in=visible).exclude(state__in=["closed", "merged"]).select_related("project").prefetch_related("tasks")[:30],
        "deliveries": WebhookDelivery.objects.all()[:25] if request.user.can_manage_projects else [],
    })
