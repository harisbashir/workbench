"""Firmware sets: which released version each controller on a board ships with, per revision.

Views call these functions; the rules live here so the revision page, builds and
the flashing bundle all agree on what's allowed and what needs a warning.
"""
from django.db import transaction

from apps.projects.models import Revision

from .models import ControllerSlot, FirmwareRelease, FirmwareSetEntry

LOCKED_MESSAGE = "This firmware set is locked. Ask a lead to unlock it."


def issues_for(release, revision):
    """Problems with flashing `release` on `revision`, as (level, text) pairs; level is "bad" or "warn"."""
    S = FirmwareRelease.Status
    issues = []
    if release.status == S.RECALLED:
        issues.append(("bad", "Recalled — do not flash"))
    elif release.status == S.DEPRECATED:
        issues.append(("warn", "Deprecated — replaced by a newer version"))
    elif release.status != S.RELEASED:
        issues.append(("warn", f"{release.get_status_display()} — not released yet"))
    if not release.revisions.filter(pk=revision.pk).exists():
        issues.append(("warn", f"Not marked compatible with {revision.title}"))
    newer = release.firmware.recommended_for(revision)
    if newer and newer.sort_key > release.sort_key:
        issues.append(("warn", f"Newer release {newer.version} is available"))
    return issues


def slots_for(revision):
    return ControllerSlot.objects.filter(controller__board_id=revision.board_id).select_related("controller", "firmware")


def selectable_releases(slot, revision):
    allowed = ["released"] if revision.status == Revision.Status.RELEASED else ["released", "testing"]
    return slot.firmware.releases.filter(status__in=allowed).order_by("-sort_key")


def set_rows(revision):
    entries = {e.slot_id: e for e in revision.firmware_set.select_related("release__firmware")}
    rows = []
    for slot in slots_for(revision):
        entry = entries.get(slot.pk)
        rel = entry.release if entry else None
        rows.append({
            "slot": slot, "entry": entry, "release": rel,
            "recommended": slot.firmware.recommended_for(revision),
            "choices": list(selectable_releases(slot, revision)),
            "issues": issues_for(rel, revision) if rel else [("warn", "No version chosen")],
        })
    return rows


def missing_count(revision):
    return slots_for(revision).exclude(entries__revision=revision).count()


def save_set(revision, choices, user):
    """Save {slot id: release id or None}. Returns error messages; slots with an error are left as they were."""
    errors = []
    with transaction.atomic():
        locked = Revision.objects.select_for_update().get(pk=revision.pk)
        if locked.firmware_set_locked:
            return [LOCKED_MESSAGE]
        slots = {s.pk: s for s in slots_for(locked)}
        for slot_id, release_id in choices.items():
            slot = slots.get(slot_id)
            if slot is None:
                continue
            if release_id is None:
                FirmwareSetEntry.objects.filter(revision=locked, slot=slot).delete()
                continue
            rel = selectable_releases(slot, locked).filter(pk=release_id).first()
            if rel is None:
                version = FirmwareRelease.objects.filter(pk=release_id, firmware=slot.firmware).values_list("version", flat=True).first()
                errors.append(f"{slot.firmware.name} {version or '?'} can't be used on {locked.title}.")
                continue
            FirmwareSetEntry.objects.update_or_create(revision=locked, slot=slot, defaults={"release": rel, "updated_by": user})
    return errors


def set_lock(revision, locked):
    """Lock or unlock a revision's firmware set. Returns False if it was already in that state."""
    changed = Revision.objects.filter(pk=revision.pk, firmware_set_locked=not locked).update(firmware_set_locked=locked)
    revision.firmware_set_locked = locked
    return bool(changed)
