"""System → Storage: choose where uploaded files are kept and move them there."""
from django.contrib import messages
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from . import storage as st
from .forms import StorageLocationForm
from .models import StorageMove, StorageSettings
from .utils import admin_required, audit


def _worker_alive():
    from . import system
    beat = system.health().get("scheduler_seen")
    return bool(beat) and (timezone.now() - timezone.datetime.fromisoformat(beat)).total_seconds() < 180


def _file_totals():
    from apps.files.models import DocumentVersion
    from apps.firmware.models import FirmwareArtifact
    n, size = 0, 0
    for model in (DocumentVersion, FirmwareArtifact):
        agg = model.objects.aggregate(s=Sum("size"))
        n += model.objects.count()
        size += agg["s"] or 0
    from django.apps import apps
    if apps.is_installed("apps.design"):
        m = apps.get_model("design", "DesignFile")
        n += m.objects.count()
        size += m.objects.aggregate(s=Sum("size"))["s"] or 0
    return n, size


@admin_required
def storage_page(request):
    cfg = StorageSettings.load()
    pinned = st.env_config()
    active = st.unseal(cfg.active) if cfg.active else st.default_local()
    draft = st.unseal(cfg.draft) if cfg.draft else None
    running = StorageMove.objects.filter(status__in=["pending", "running"]).first()
    editing = draft or active

    if request.method == "POST" and not pinned:
        action = request.POST.get("action")
        if running and action != "refresh":
            messages.error(request, "Files are being moved right now. Wait for that to finish.")
            return redirect("core:storage")
        if action == "test":
            form = StorageLocationForm(request.POST, saved=editing)
            if form.is_valid():
                new = form.config()
                if cfg.locked and not st.same_place(new, active):
                    messages.error(request, "Storage is locked. You can update keys for the same bucket, but not move files elsewhere.")
                    return redirect("core:storage")
                ok, msg = st.test_storage(new)
                cfg.draft = st.seal(new)
                cfg.draft_tested_at, cfg.draft_test_ok, cfg.draft_test_message = timezone.now(), ok, msg
                cfg.save()
                audit(request, "storage.tested", None, where=st.describe(new)["where"], ok=ok)
                (messages.success if ok else messages.error)(request, msg)
                return redirect("/system/storage/#change")
        elif action == "discard":
            cfg.draft, cfg.draft_test_ok, cfg.draft_test_message = {}, False, ""
            cfg.save()
            return redirect("core:storage")
        elif action == "apply":
            if not (draft and cfg.draft_test_ok):
                messages.error(request, "Test the connection first.")
            elif not st.same_place(draft, active):
                messages.error(request, "That's a different place — use “Move files and switch”.")
            else:
                cfg.active, cfg.draft, cfg.draft_test_message = cfg.draft, {}, ""
                cfg.save()
                audit(request, "storage.keys_updated", None, where=st.describe(draft)["where"])
                messages.success(request, "Storage settings updated.")
            return redirect("core:storage")
        elif action == "move":
            if cfg.locked:
                messages.error(request, "Storage is locked.")
            elif not (draft and cfg.draft_test_ok):
                messages.error(request, "Test the connection first.")
            elif st.same_place(draft, active):
                messages.error(request, "Files are already stored there.")
            else:
                move = StorageMove.objects.create(requested_by=request.user, source=cfg.active or st.default_local(),
                                                  target=cfg.draft, lock_after=bool(request.POST.get("lock")))
                audit(request, "storage.move_started", None, to=st.describe(draft)["where"], lock=move.lock_after)
                if not _worker_alive():
                    from .storage_move import start_in_background
                    start_in_background(move)
                messages.success(request, "Moving files. You can leave this page — you'll get a notification when it's done.")
            return redirect("core:storage")
        elif action == "forget_previous":
            where = st.describe(st.unseal(cfg.previous))["where"] if cfg.previous else ""
            cfg.previous = {}
            cfg.save()
            audit(request, "storage.previous_forgotten", None, where=where)
            messages.success(request, f"Workbench no longer reads from {where}. The files there weren't deleted — remove them yourself when you're ready.")
            return redirect("core:storage")

    initial = {"kind": editing.get("kind", "local"), "path": editing.get("path", ""), "provider": editing.get("provider", "aws"),
               "bucket": editing.get("bucket", ""), "region": editing.get("region", ""), "endpoint": editing.get("endpoint", ""),
               "prefix": editing.get("prefix", "workbench") if editing.get("kind") == "s3" else "workbench",
               "access_key": editing.get("access_key", "")}
    if initial["kind"] == "local" and not initial["path"]:
        initial["path"] = str(st.default_local()["path"])
    form = StorageLocationForm(initial=initial, saved=editing)
    count, size = _file_totals()
    return render(request, "core/system/storage.html", {
        "tab": "storage", "cfg": cfg, "pinned": st.describe(pinned) if pinned else None,
        "active": st.describe(active), "draft": st.describe(draft) if draft else None,
        "draft_same_place": bool(draft) and st.same_place(draft, active),
        "previous": st.describe(st.unseal(cfg.previous)) if cfg.previous else None,
        "form": form, "providers": st.PROVIDERS, "running": running,
        "moves": StorageMove.objects.select_related("requested_by")[:5], "file_count": count, "file_size": size,
    })


@admin_required
def storage_status(request):
    move = StorageMove.objects.first()
    if not move:
        return JsonResponse({"status": "none"})
    return JsonResponse({"status": move.status, "percent": move.percent, "done": move.done_count, "total": move.total,
                         "copied": move.copied, "skipped": move.skipped, "failed": move.failed, "message": move.message})
