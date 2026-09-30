"""Rendering a revision's Gerbers, with a disk cache (renders are keyed by file checksums)."""
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from django.conf import settings

from .board import MAX_TOTAL, collect, looks_like, render_board
from .gerber import RENDER_SECONDS
from .models import Category, DesignFile

log = logging.getLogger("workbench")
RENDER_VERSION = "4"


def cache_dir():
    d = Path(settings.DATA_DIR) / "cache" / "pcb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gerber_files(revision, file=None):
    """The files that make up the board picture: one Gerber zip (plus loose drill
    files if the zip has none), or all loose Gerber and drill files."""
    current = DesignFile.objects.filter(revision=revision, is_current=True,
                                        category__in=[Category.GERBER, Category.DRILL]).order_by("-uploaded_at")
    if file is not None:
        if file.is_gerber_zip:
            return [file] + [f for f in current if f.category == Category.DRILL and f.ext != "zip"]
        return [file]
    zips = [f for f in current if f.ext == "zip"]
    if zips:
        return [zips[0]] + [f for f in current if f.category == Category.DRILL and f.ext != "zip"]
    return [f for f in current if f.ext != "zip"]


def _read(f):
    with f.file.open("rb") as fh:
        return fh.read()


def render_files(files):
    """Render (or load from cache) the board for these DesignFiles. Returns a dict."""
    if not files:
        return None
    key = hashlib.sha256((RENDER_VERSION + "|" + "|".join(sorted(f.sha256 for f in files))).encode()).hexdigest()[:40]
    path = cache_dir() / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    # One render per file set at a time: a slow render mustn't be started again by
    # every page view (and every worker) while it runs.
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        try:
            stale = time.time() - lock.stat().st_mtime > RENDER_SECONDS * 4
        except OSError:
            stale = True
        if not stale:
            return {"ok": False, "pending": True, "error": "The board is still being drawn. Reload the page in a moment.",
                    "sources": [{"id": f.pk, "name": f.name, "version": f.version} for f in files]}
        lock.touch()
    try:
        return _render_and_cache(files, path)
    finally:
        lock.unlink(missing_ok=True)


def _render_and_cache(files, path):
    try:
        if sum(f.size or 0 for f in files) > MAX_TOTAL:
            raise ValueError("The Gerber files are too large to show (over 120 MB).")
        main = [(f.name, _read(f)) for f in files if f.category != Category.DRILL or f.ext == "zip"]
        drills = [(f.name, _read(f)) for f in files if f.category == Category.DRILL and f.ext != "zip"]
        if drills and any(looks_like(n, d) == "drill" for n, d in collect(main)):
            drills = []  # the zip has its own drill files
        result = render_board(main + drills)
        result["ok"] = True
    except (ValueError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - never break the page over a strange file
        log.exception("Rendering Gerbers failed")
        result = {"ok": False, "error": f"These files couldn't be drawn ({type(exc).__name__})."}
    result["sources"] = [{"id": f.pk, "name": f.name, "version": f.version} for f in files]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(path)
    return result


def prune_cache(keep_mb=500):
    files = sorted(cache_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for p in files:
        total += p.stat().st_size
        if total > keep_mb * 1024 * 1024:
            p.unlink(missing_ok=True)
