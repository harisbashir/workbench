"""Drawing KiCad files for Sources, with a disk cache keyed by the file's SHA-256.

The cache lives in <data>/cache/kicad and can be deleted at any time; it's rebuilt as needed.
The scheduler pre-draws files from recent check-ins so pages open quickly.
"""
import hashlib
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
RENDER_VERSION = "4"
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
    """The cached facts (.json); the drawing itself is kept beside it (.svg) so pages can read the facts alone."""
    return cache_dir() / f"{sha}-{kind}-{RENDER_VERSION}{('-' + extra) if extra else ''}.json"


def _read(target, svg=True):
    try:
        data = json.loads(target.read_text())
        if svg and data.get("ok"):
            data["svg"] = target.with_suffix(".svg").read_text()
        return data
    except (OSError, ValueError):
        return None


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


def _draw(blob, path, highlight=None, model=None, prefix="k", instance=None):
    kind = kind_of(path)
    budget = Budget()  # one time allowance for reading and drawing
    if kind == "sch":
        sch = model or schematic.load_schematic(_load(blob), budget)
        if instance:
            sch.use_instance(*instance)
        return {"ok": True, "kind": "sch", "svg": schematic.draw_schematic(sch, highlight=highlight, budget=budget),
                "parts": sch.parts(), "sheets": [{"name": s.name, "file": s.file, "uuid": s.uuid} for s in sch.sheets],
                "nets": sorted(sch.nets()), "title": sch.title, "newer": sch.newer, "version": sch.version,
                "uuid": sch.uuid, "instance_paths": sch.instance_paths(),
                "symbol_instances": {k: list(v) for k, v in sch.symbol_instances.items()}}
    board = model or pcb.load_pcb(_load(blob), budget)
    result = pcb.render_pcb("", board=board, highlight=highlight, prefix=prefix, budget=budget)
    result.update({"ok": True, "kind": "pcb", "newer": board.newer, "version": board.version})
    return result


def render_blob(blob, path, highlight=None, extra="", use_cache=True, model=None, svg=True, instance=None, v6=None):
    """{"ok": True, "svg": …, …} or {"ok": False, "error": "…"}. `extra` names a highlighted variant in the cache.
    svg=False leaves the drawing out (it's read from disk only when asked for). `instance` draws one copy of a
    schematic sheet used more than once, with that copy's references (`v6`: the top sheet's KiCad 6 list)."""
    kind = kind_of(path)
    if kind is None:
        return {"ok": False, "error": "This isn't a KiCad schematic or PCB."}
    if instance:
        key = hashlib.sha1(json.dumps([instance, sorted((v6 or {}).items())]).encode()).hexdigest()[:16]
        extra = f"{extra}-i{key}" if extra else f"i{key}"
    target = _cache_file(blob.sha256, kind, extra)
    if use_cache:
        cached = _read(target, svg)
        if cached is not None:
            return cached
    started = time.monotonic()
    lasting = True  # only results that would come out the same next time are kept
    try:
        # ids get the file's checksum so two drawings on one page (compare) don't share them
        result = _draw(blob, path, highlight=highlight, model=model, prefix=f"k{blob.sha256[:8]}{extra[-3:]}",
                       instance=(instance, v6) if instance else None)
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
        if result.get("ok"):
            _write(target.with_suffix(".svg"), result["svg"])
        _write(target, {k: v for k, v in result.items() if k != "svg"})
    log.debug("Drew %s in %.2f s", path, time.monotonic() - started)
    if not svg:
        result = {k: v for k, v in result.items() if k != "svg"}
    return result


def _write(target, data):
    try:
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{time.monotonic_ns()}.partial")
        tmp.write_text(data if isinstance(data, str) else json.dumps(data))
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
    cached = _read(_cache_file(blob.sha256, kind, extra))
    if cached is not None:
        return cached
    try:
        summary = compare_blobs(other, blob, path) if older else compare_blobs(blob, other, path)
    except KicadError as exc:
        return {"ok": False, "error": str(exc)}
    highlight = summary["highlight_b"] if older else summary["highlight_a"]
    return render_blob(blob, path, highlight=highlight, extra=extra)


def prerender(limit=20, days=7, seconds=60):
    """Draw KiCad files from recent check-ins that aren't in the cache yet. Returns how many were drawn.
    A bookmark in the cache folder remembers the last file looked at, so each file is only looked at once."""
    from django.db.models import Q

    from apps.sources.models import VersionFile
    mark_file = cache_dir() / "prerender.mark"
    try:
        mark = int(mark_file.read_text())
    except (OSError, ValueError):
        mark = 0
    since = timezone.now() - timedelta(days=days)
    kicad = Q()
    for ext in KINDS:
        kicad |= Q(path__iendswith=ext)
    files = (VersionFile.objects.filter(kicad, pk__gt=mark, version__created_at__gte=since)
             .select_related("blob").order_by("pk"))
    deadline = time.monotonic() + seconds
    done, seen, start = 0, set(), mark
    for vf in files.iterator():
        if time.monotonic() > deadline:
            break
        kind = kind_of(vf.path)
        if kind is not None and vf.blob.sha256 not in seen:
            if not _cache_file(vf.blob.sha256, kind).exists():
                if done >= limit:
                    break  # this one is drawn next time
                render_blob(vf.blob, vf.path)
                done += 1
            seen.add(vf.blob.sha256)
        mark = vf.pk
    if mark != start:
        _write(mark_file, str(mark))
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
            if (p.suffix == ".partial" and st.st_mtime < now - 3600) or (p.suffix in (".json", ".svg") and last < cutoff):
                p.unlink()
                removed += 1
            elif p.suffix in (".json", ".svg"):
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
