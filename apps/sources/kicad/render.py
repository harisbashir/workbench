"""Drawing KiCad files for Sources, with a disk cache keyed by the file's SHA-256.

The cache lives in <data>/cache/kicad and can be deleted at any time; it's rebuilt as needed.
The scheduler pre-draws files from recent check-ins so pages open quickly.
"""
import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.design.gerber import Budget, GerberLimitError

from . import compare as kicad_compare
from . import pcb, schematic
from .sexpr import MAX_BYTES, KicadError

log = logging.getLogger("workbench")
RENDER_VERSION = "2"
MAX_CACHE_BYTES = 2 * 1024 ** 3   # previews kept on disk; the least recently used go first
KINDS = {".kicad_sch": "sch", ".kicad_pcb": "pcb"}


def kind_of(path):
    for ext, kind in KINDS.items():
        if path.lower().endswith(ext):
            return kind
    return None


def cache_dir():
    d = Path(settings.DATA_DIR) / "cache" / "kicad"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_file(sha, kind, extra=""):
    return cache_dir() / f"{sha}-{kind}-{RENDER_VERSION}{('-' + extra) if extra else ''}.json"


def _load(blob):
    if blob.size > MAX_BYTES:
        raise KicadError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB, so it isn't drawn.")
    text = blob.text()
    if text is None:
        raise KicadError("This file can't be read as text.")
    return text


def parse_blob(blob, path, budget=None):
    """The parsed Schematic or Board (no drawing)."""
    text = _load(blob)
    budget = budget or Budget()
    return schematic.load_schematic(text, budget) if kind_of(path) == "sch" else pcb.load_pcb(text, budget)


def _draw(blob, path, highlight=None, model=None, prefix="k"):
    kind = kind_of(path)
    budget = Budget()  # one time allowance for reading and drawing
    if kind == "sch":
        sch = model or schematic.load_schematic(_load(blob), budget)
        return {"ok": True, "kind": "sch", "svg": schematic.draw_schematic(sch, highlight=highlight, budget=budget),
                "parts": sch.parts(), "sheets": [{"name": s.name, "file": s.file} for s in sch.sheets],
                "nets": sorted(sch.nets()), "title": sch.title, "newer": sch.newer, "version": sch.version}
    board = model or pcb.load_pcb(_load(blob), budget)
    result = pcb.render_pcb("", board=board, highlight=highlight, prefix=prefix, budget=budget)
    result.update({"ok": True, "kind": "pcb", "newer": board.newer, "version": board.version})
    return result


def render_blob(blob, path, highlight=None, extra="", use_cache=True, model=None):
    """{"ok": True, "svg": …, …} or {"ok": False, "error": "…"}. `extra` names a highlighted variant in the cache."""
    kind = kind_of(path)
    if kind is None:
        return {"ok": False, "error": "This isn't a KiCad schematic or PCB."}
    target = _cache_file(blob.sha256, kind, extra)
    if use_cache and target.exists():
        try:
            return json.loads(target.read_text())
        except (OSError, ValueError):
            pass
    started = time.monotonic()
    lasting = True  # only results that would come out the same next time are kept
    try:
        # ids get the file's checksum so two drawings on one page (compare) don't share them
        result = _draw(blob, path, highlight=highlight, model=model, prefix=f"k{blob.sha256[:8]}{extra[-3:]}")
    except KicadError as exc:
        result = {"ok": False, "error": str(exc)}
    except GerberLimitError as exc:
        result, lasting = {"ok": False, "error": str(exc)}, "too long" not in str(exc)
    except (MemoryError, RecursionError):
        result, lasting = {"ok": False, "error": "The file is too complex to draw."}, False
    except Exception:
        log.exception("Couldn't draw KiCad file %s", path)
        result, lasting = {"ok": False, "error": "Something in this file couldn't be drawn. You can still view it as text."}, False
    if use_cache and lasting:
        _write(target, result)
    log.debug("Drew %s in %.2f s", path, time.monotonic() - started)
    return result


def _write(target, data):
    try:
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{time.monotonic_ns()}.partial")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, target)
    except OSError:
        log.warning("Couldn't write KiCad preview cache %s", target)


def compare_blobs(old, new, path):
    """The change summary between two versions of a KiCad file, cached. Raises KicadError."""
    kind = kind_of(path)
    target = cache_dir() / f"cmp-{old.sha256}-{new.sha256}-{kind}-{RENDER_VERSION}.json"
    if target.exists():
        try:
            return json.loads(target.read_text())
        except (OSError, ValueError):
            pass
    budget = Budget()
    summary = kicad_compare.compare(kind, parse_blob(old, path, budget), parse_blob(new, path, budget))
    _write(target, summary)
    return summary


def highlighted(blob, other, path, older):
    """`blob` drawn with what changed against `other` highlighted. `older`: `other` is the earlier version."""
    kind = kind_of(path)
    extra = f"vs-{other.sha256[:16]}-{'new' if older else 'old'}"
    target = _cache_file(blob.sha256, kind, extra)
    if target.exists():
        try:
            return json.loads(target.read_text())
        except (OSError, ValueError):
            pass
    try:
        summary = compare_blobs(other, blob, path) if older else compare_blobs(blob, other, path)
    except KicadError as exc:
        return {"ok": False, "error": str(exc)}
    highlight = summary["highlight_b"] if older else summary["highlight_a"]
    return render_blob(blob, path, highlight=highlight, extra=extra)


def prerender(limit=20, days=7, seconds=60):
    """Draw KiCad files from recent check-ins that aren't in the cache yet. Returns how many were drawn."""
    from apps.sources.models import VersionFile
    since = timezone.now() - timedelta(days=days)
    done, seen = 0, set()
    files = (VersionFile.objects.filter(version__created_at__gte=since).select_related("blob")
             .order_by("-version__created_at"))
    deadline = time.monotonic() + seconds
    for vf in files.iterator():
        if time.monotonic() > deadline:
            break
        kind = kind_of(vf.path)
        if kind is None or vf.blob.sha256 in seen:
            continue
        seen.add(vf.blob.sha256)
        if _cache_file(vf.blob.sha256, kind).exists():
            continue
        render_blob(vf.blob, vf.path)
        done += 1
        if done >= limit:
            break
    return done


def prune(days=30):
    """Remove cached drawings not used for a while, leftovers of interrupted writes, and the least
    recently used drawings while the cache is larger than MAX_CACHE_BYTES."""
    now = time.time()
    cutoff = now - days * 86400
    removed, kept = 0, []
    for p in cache_dir().iterdir():
        try:
            st = p.stat()
            last = max(st.st_atime, st.st_mtime)
            if (p.suffix == ".partial" and st.st_mtime < now - 3600) or (p.suffix == ".json" and last < cutoff):
                p.unlink()
                removed += 1
            elif p.suffix == ".json":
                kept.append((last, st.st_size, p))
        except OSError:
            pass
    total = sum(size for _, size, _ in kept)
    for last, size, p in sorted(kept):
        if total <= MAX_CACHE_BYTES:
            break
        try:
            p.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    return removed
