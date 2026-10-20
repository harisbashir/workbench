"""Checking in a new version: what changes, what's refused, and committing it safely.

Two rules stop people overwriting each other's work:
  * Stale files: a file that changed since the version you started from (your "base")
    can't be overwritten or deleted by your check-in. Download the latest, merge, retry.
  * Locks: KiCad schematics and PCBs must be checked out before they're changed, and a
    file someone else has checked out can't be changed by anyone else.
"""
import os
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from . import diff
from .models import Lock, Repository, Version, VersionFile

LOCKED_TYPES = (".kicad_sch", ".kicad_pcb")


def tree(version):
    if version is None:
        return {}
    return {f.path: f.blob for f in version.files.select_related("blob")}


def needs_lock(path):
    return path.lower().endswith(LOCKED_TYPES)


@dataclass
class Plan:
    base: int
    latest: int
    added: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    refused: list = field(default_factory=list)   # [(path, reason)]
    new_tree: dict = field(default_factory=dict)
    old_tree: dict = field(default_factory=dict)

    @property
    def has_changes(self):
        return bool(self.added or self.changed or self.deleted)


def _version(repo, number):
    return repo.versions.filter(number=number).first() if number else None


def plan(repo, staged, mode, base, user):
    latest_v = repo.latest()
    latest = latest_v.number if latest_v else 0
    base = min(max(base or 0, 0), latest)
    latest_tree = tree(latest_v)
    base_tree = latest_tree if base == latest else tree(_version(repo, base))
    locks = {lk.path: lk.user for lk in repo.locks.select_related("user")}
    p = Plan(base=base, latest=latest, new_tree=dict(latest_tree), old_tree=latest_tree)

    candidates = []
    for path, blob in sorted(staged.items()):
        current = latest_tree.get(path)
        if current is not None and current.pk == blob.pk:
            p.unchanged.append(path)
        else:
            candidates.append((path, blob, "changed" if current is not None else "added"))
    if mode == "replace":
        candidates += [(path, None, "deleted") for path in sorted(latest_tree) if path not in staged]

    for path, blob, status in candidates:
        was, now = base_tree.get(path), latest_tree.get(path)
        holder = locks.get(path)
        name = os.path.basename(path)
        if (was.pk if was else None) != (now.pk if now else None):
            p.refused.append((path, f"Changed by someone else since v{base}. Download v{latest} and merge your changes."))
        elif holder is not None and holder.pk != user.pk:
            p.refused.append((path, f"Checked out by {holder.get_full_name() or holder.username}."))
        elif needs_lock(path) and now is not None and holder is None:
            p.refused.append((path, f"Check out {name} first, so nobody else changes it at the same time."))
        else:
            getattr(p, status).append(path)
            if blob is None:
                p.new_tree.pop(path, None)
            else:
                p.new_tree[path] = blob
    return p


def commit(repo, staged, mode, base, message, user, *, allow_partial=False, release_locks=True):
    """Commit a check-in. Returns (Version or None, Plan); None when nothing changed or files were refused
    (unless `allow_partial`, which checks in everything else)."""
    with transaction.atomic():
        repo = Repository.objects.select_for_update().get(pk=repo.pk)
        p = plan(repo, staged, mode, base, user)
        if not p.has_changes or (p.refused and not allow_partial):
            return None, p
        parent = repo.latest()
        added = removed = 0
        for path in p.changed:
            a, r = diff.count_lines(p.old_tree[path], p.new_tree[path])
            added, removed = added + a, removed + r
        for path in p.added:
            added += diff.count_lines(None, p.new_tree[path])[0]
        for path in p.deleted:
            removed += diff.count_lines(p.old_tree[path], None)[1]
        number = repo.latest_number + 1
        v = Version.objects.create(repository=repo, number=number, message=message[:300], author=user, parent=parent,
                                   added=len(p.added), changed=len(p.changed), deleted=len(p.deleted),
                                   lines_added=added, lines_removed=removed)
        VersionFile.objects.bulk_create([VersionFile(version=v, path=path, blob=blob) for path, blob in sorted(p.new_tree.items())])
        Repository.objects.filter(pk=repo.pk).update(latest_number=number)
        if release_locks:
            repo.locks.filter(user=user, path__in=p.added + p.changed + p.deleted).delete()
    return v, p


def lock(repo, path, user):
    """Check out a file. Returns None on success, or why it couldn't be."""
    if path not in tree(repo.latest()):
        return "That file isn't in the latest version."
    try:
        with transaction.atomic():
            Lock.objects.create(repository=repo, path=path, user=user)
    except IntegrityError:
        holder = Lock.objects.select_related("user").get(repository=repo, path=path).user
        if holder.pk == user.pk:
            return None
        return f"Already checked out by {holder.get_full_name() or holder.username}."
    return None


def unlock(repo, path, user, *, force=False):
    """Release a check-out: your own, or anyone's with `force`. Returns whether a lock was removed."""
    locks = Lock.objects.filter(repository=repo, path=path)
    if not force:
        locks = locks.filter(user=user)
    return locks.delete()[0] > 0
