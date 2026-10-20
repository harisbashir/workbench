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

from apps.design.gerber import GerberLimitError

from . import pcb, schematic
from .sexpr import MAX_BYTES, KicadError

log = logging.getLogger("workbench")
RENDER_VERSION = "1"
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


def parse_blob(blob, path):
    """The parsed Schematic or Board (no drawing)."""
    text = _load(blob)
    return schematic.load_schematic(text) if kind_of(path) == "sch" else pcb.load_pcb(text)


def _draw(blob, path, highlight=None, model=None):
    kind = kind_of(path)
    if kind == "sch":
        sch = model or schematic.load_schematic(_load(blob))
        return {"ok": True, "kind": "sch", "svg": schematic.draw_schematic(sch, highlight=highlight),
                "parts": sch.parts(), "sheets": [{"name": s.name, "file": s.file} for s in sch.sheets],
                "nets": sorted(sch.nets()), "title": sch.title, "newer": sch.newer, "version": sch.version}
    board = model or pcb.load_pcb(_load(blob))
    result = pcb.render_pcb("", board=board, highlight=highlight)
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
    try:
        result = _draw(blob, path, highlight=highlight, model=model)
    except (KicadError, GerberLimitError) as exc:
        result = {"ok": False, "error": str(exc)}
    except (MemoryError, RecursionError):
        result = {"ok": False, "error": "The file is too complex to draw."}
    except Exception:
        log.exception("Couldn't draw KiCad file %s", path)
        result = {"ok": False, "error": "Something in this file couldn't be drawn. You can still view it as text."}
    if use_cache:
        try:
            tmp = target.with_suffix(".partial")
            tmp.write_text(json.dumps(result))
            os.replace(tmp, target)
        except OSError:
            log.warning("Couldn't write KiCad preview cache %s", target)
    log.debug("Drew %s in %.2f s", path, time.monotonic() - started)
    return result


def prerender(limit=20, days=7):
    """Draw KiCad files from recent check-ins that aren't in the cache yet. Returns how many were drawn."""
    from apps.sources.models import VersionFile
    since = timezone.now() - timedelta(days=days)
    done, seen = 0, set()
    files = (VersionFile.objects.filter(version__created_at__gte=since).select_related("blob")
             .order_by("-version__created_at"))
    for vf in files.iterator():
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
    """Remove cached drawings not used for a while."""
    cutoff = time.time() - days * 86400
    removed = 0
    for p in cache_dir().glob("*.json"):
        try:
            if p.stat().st_atime < cutoff and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
