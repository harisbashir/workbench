import hashlib
import json
import logging
import os
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings
from apps.projects.models import Project

from . import github
from .models import PullRequest, WebhookDelivery

log = logging.getLogger("workbench")


def webhook_secret():
    """The shared secret GitHub signs deliveries with (System → GitHub)."""
    return os.environ.get("WORKBENCH_GITHUB_WEBHOOK_SECRET") or SiteSettings.load().github_secret


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
    body_hash = hashlib.sha256(body).hexdigest()
    record = _claim_delivery(delivery, event, body_hash)
    if record is None:
        return HttpResponse("Duplicate delivery ignored")  # replay protection
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("payload isn't a JSON object")
        record.repository = ((payload.get("repository") or {}).get("full_name") or "")[:200]
        with transaction.atomic():
            record.status, record.message = github.handle(event, payload)
    except Exception as exc:
        log.exception("GitHub webhook failed")
        # The details go to the server log; GitHub (and the deliveries list) only get the type.
        record.status, record.message = "error", f"Processing failed ({type(exc).__name__}). See the server log."
    record.save()
    return JsonResponse({"status": record.status, "message": record.message})


def _claim_delivery(delivery, event, body_hash):
    """Records a delivery before processing it. Returns None for a replay: the same
    delivery ID or the same signed body seen before — except that a delivery which
    failed with an error may be sent again (GitHub's "Redeliver" keeps the ID)."""
    earlier = WebhookDelivery.objects.filter(Q(delivery_id=delivery) | Q(body_sha256=body_hash)).first()
    if earlier is not None:
        if earlier.status != "error":
            return None
        with transaction.atomic():
            # Claim the retry atomically so two redeliveries can't both run.
            claimed = WebhookDelivery.objects.filter(pk=earlier.pk, status="error").update(status="received", message="Retrying")
        if not claimed:
            return None
        earlier.refresh_from_db()
        return earlier
    try:
        with transaction.atomic():
            return WebhookDelivery.objects.create(delivery_id=delivery, event=event[:40], status="received",
                                                  body_sha256=body_hash)
    except IntegrityError:
        return None


@login_required
def overview(request):
    visible = Project.objects.visible_to(request.user)
    return render(request, "integrations/overview.html", {
        "webhook_url": SiteSettings.load().absolute_url(reverse("integrations:github_webhook"), request),
        "secret_set": bool(webhook_secret()),
        "projects": visible.order_by("key"),
        "pulls": PullRequest.objects.filter(project__in=visible).exclude(state__in=["closed", "merged"]).select_related("project").prefetch_related("tasks")[:30],
        "deliveries": WebhookDelivery.objects.all()[:25] if request.user.can_manage_projects else [],
    })
