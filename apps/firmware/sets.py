"""Firmware sets: which released version each controller on a board ships with, per revision.

Views call these functions; the rules live here so the revision page, builds and
the flashing bundle all agree on what's allowed and what needs a warning.
"""
from collections import defaultdict

from django.db import transaction

from apps.projects.models import Revision

from .models import ControllerSlot, FirmwareRelease, FirmwareSetEntry

LOCKED_MESSAGE = "This firmware set is locked. Ask a lead to unlock it."


class Releases:
    """Every release of some firmwares and which of them suit one revision, read once, so a page with many
    controllers doesn't ask the database again for each one."""

    def __init__(self, firmware_ids, revision):
        self.revision = revision
        self.by_firmware = defaultdict(list)
        for r in FirmwareRelease.objects.filter(firmware_id__in=set(firmware_ids)).select_related("firmware").order_by("-sort_key"):
            self.by_firmware[r.firmware_id].append(r)
        self.compatible = set(FirmwareRelease.revisions.through.objects.filter(
            revision_id=revision.pk, firmwarerelease__firmware_id__in=set(firmware_ids)).values_list("firmwarerelease_id", flat=True))

    def recommended(self, firmware_id):
        """The newest released version compatible with the revision."""
        return next((r for r in self.by_firmware[firmware_id]
                     if r.status == FirmwareRelease.Status.RELEASED and r.pk in self.compatible), None)

    def selectable(self, firmware_id):
        allowed = selectable_statuses(self.revision)
        return [r for r in self.by_firmware[firmware_id] if r.status in allowed]


def issues_for(release, revision, known=None):
    """Problems with flashing `release` on `revision`, as (level, text) pairs; level is "bad" or "warn".
    `known`: a Releases already read for this revision."""
    S = FirmwareRelease.Status
    issues = []
    if release.status == S.RECALLED:
        issues.append(("bad", "Recalled — do not flash"))
    elif release.status == S.DEPRECATED:
        issues.append(("warn", "Deprecated — replaced by a newer version"))
    elif release.status != S.RELEASED:
        issues.append(("warn", f"{release.get_status_display()} — not released yet"))
    compatible = release.pk in known.compatible if known else release.revisions.filter(pk=revision.pk).exists()
    if not compatible:
        issues.append(("warn", f"Not marked compatible with {revision.title}"))
    newer = known.recommended(release.firmware_id) if known else release.firmware.recommended_for(revision)
    if newer and newer.sort_key > release.sort_key:
        issues.append(("warn", f"Newer release {newer.version} is available"))
    return issues


def slots_for(revision):
    return ControllerSlot.objects.filter(controller__board_id=revision.board_id).select_related("controller", "firmware")


def selectable_statuses(revision):
    return ["released"] if revision.status == Revision.Status.RELEASED else ["released", "testing"]


def selectable_releases(slot, revision):
    return slot.firmware.releases.filter(status__in=selectable_statuses(revision)).order_by("-sort_key")


def set_rows(revision):
    entries = {e.slot_id: e for e in revision.firmware_set.select_related("release__firmware")}
    slots = list(slots_for(revision))
    known = Releases([s.firmware_id for s in slots], revision)
    rows = []
    for slot in slots:
        entry = entries.get(slot.pk)
        rel = entry.release if entry else None
        rows.append({
            "slot": slot, "entry": entry, "release": rel,
            "recommended": known.recommended(slot.firmware_id),
            "choices": known.selectable(slot.firmware_id),
            "issues": issues_for(rel, revision, known) if rel else [("warn", "No version chosen")],
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
            current = FirmwareSetEntry.objects.filter(revision=locked, slot=slot).values_list("release_id", flat=True).first()
            if current == release_id:
                continue  # unchanged: keep it even if it's no longer selectable (e.g. deprecated since)
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
