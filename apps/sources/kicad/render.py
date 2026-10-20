"""Drawing KiCad files for Sources, with a disk cache keyed by the file's SHA-256.

The cache lives in <data>/cache/kicad and can be deleted at any time; it's rebuilt as needed.
The scheduler pre-draws files from recent check-ins so pages open quickly.
"""
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.design.gerber import Budget, GerberLimitError

from . import compare as kicad_compare
from . import drawing, pcb, schematic
from .sexpr import MAX_BYTES, KicadError

log = logging.getLogger("workbench")
RENDER_VERSION = "5"
MAX_CACHE_BYTES = 2 * 1024 ** 3   # previews kept on disk; the least recently used go first
KINDS = {".kicad_sch": "sch", ".kicad_pcb": "pcb"}
MB = 1024 * 1024
# Files over MAX_BYTES are drawn by the scheduler in a separate process (see draw_large), up to this size.
LARGE_MAX_BYTES = 200 * MB
PENDING = "This file is large, so it's drawn in the background. That usually takes a few minutes; refresh the page then."


def _setting(name, env, default):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(env, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def large_memory_mb():
    return _setting("KICAD_LARGE_MEMORY_MB", "WORKBENCH_KICAD_MEMORY_MB", 3072)


def large_timeout():
    return _setting("KICAD_LARGE_TIMEOUT", "WORKBENCH_KICAD_TIMEOUT", 600)


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
    """Cached facts, with the drawing (svg=True), or just where the drawing is (svg="path": "svg_path";
    a compressed copy may be beside it at svg_path + ".gz")."""
    try:
        data = json.loads(target.read_text())
        if svg == "path" and data.get("ok"):
            drawing = target.with_suffix(".svg")
            if not drawing.exists():
                return None
            data["svg_path"] = str(drawing)
        elif svg and data.get("ok"):
            data["svg"] = target.with_suffix(".svg").read_text()
        return data
    except (OSError, ValueError):
        return None


def _compress(drawing):
    """Keep a compressed copy beside a cached drawing; browsers get it instead (often 10–20 times smaller)."""
    gz = drawing.with_name(drawing.name + ".gz")
    tmp = gz.with_name(f"{gz.name}.{os.getpid()}.{time.monotonic_ns()}.partial")
    try:
        with open(drawing, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, MB)
        os.replace(tmp, gz)
    except OSError:
        tmp.unlink(missing_ok=True)
        log.warning("Couldn't compress KiCad preview %s", drawing)


def _load(blob):
    if blob.size > MAX_BYTES:
        raise KicadError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB, so it isn't drawn.")
    text = blob.text()
    if text is None:
        raise KicadError("This file can't be read as text.")
    return text


def parse_blob(blob, path, budget=None):
    """The parsed Schematic or Board (no drawing)."""
    if blob.size > MAX_BYTES:
        raise KicadError(f"Files larger than {MAX_BYTES // MB} MB can't be compared here. Open each version's drawing instead.")
    text = _load(blob)
    budget = budget or Budget()
    return schematic.load_schematic(text, budget) if kind_of(path) == "sch" else pcb.load_pcb(text, budget)


def _draw(blob, path, highlight=None, model=None, prefix="k", instance=None):
    return drawing.draw(None if model is not None else _load(blob), kind_of(path), highlight=highlight, prefix=prefix,
                        instance=instance, budget=Budget(), model=model)


def render_blob(blob, path, highlight=None, extra="", use_cache=True, model=None, svg=True, instance=None, v6=None):
    """{"ok": True, "svg": …, …} or {"ok": False, "error": "…"}. `extra` names a highlighted variant in the cache.
    svg=False leaves the drawing out (it's read from disk only when asked for). `instance` draws one copy of a
    schematic sheet used more than once, with that copy's references (`v6`: the top sheet's KiCad 6 list)."""
    kind = kind_of(path)
    if kind is None:
        return {"ok": False, "error": "This isn't a KiCad schematic or PCB."}
    if blob.size > MAX_BYTES:
        return _large(blob, path, svg) if not (highlight or extra or instance or model is not None) else render_blob(blob, path, svg=svg)
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
            _compress(target.with_suffix(".svg"))
        _write(target, {k: v for k, v in result.items() if k != "svg"})
    log.debug("Drew %s in %.2f s", path, time.monotonic() - started)
    if svg == "path" and use_cache and lasting and result.get("ok") and target.with_suffix(".svg").exists():
        result = {**{k: v for k, v in result.items() if k != "svg"}, "svg_path": str(target.with_suffix(".svg"))}
    elif not svg:
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


def highlighted(blob, other, path, older, svg=True):
    """`blob` drawn with what changed against `other` highlighted. `older`: `other` is the earlier version."""
    kind = kind_of(path)
    if blob.size > MAX_BYTES or other.size > MAX_BYTES:
        return render_blob(blob, path, svg=svg)  # large files are shown without highlights
    extra = f"vs-{other.sha256[:16]}-{'new' if older else 'old'}"
    cached = _read(_cache_file(blob.sha256, kind, extra), svg)
    if cached is not None:
        return cached
    try:
        summary = compare_blobs(other, blob, path) if older else compare_blobs(blob, other, path)
    except KicadError as exc:
        return {"ok": False, "error": str(exc)}
    highlight = summary["highlight_b"] if older else summary["highlight_a"]
    return render_blob(blob, path, highlight=highlight, extra=extra, svg=svg)


# --- large files -----------------------------------------------------------------------

def _want_file(sha, kind):
    return cache_dir() / f"want-{sha}-{kind}.json"


def _large(blob, path, svg=True):
    """A large file's drawing from the cache, or a request for the scheduler to draw it."""
    kind = kind_of(path)
    target = _cache_file(blob.sha256, kind)
    cached = _read(target, svg)
    if cached is not None:
        return cached
    if blob.size > LARGE_MAX_BYTES or not blob.is_text:
        result = {"ok": False, "error": f"The file is larger than {LARGE_MAX_BYTES // MB} MB, so it isn't drawn."
                  if blob.is_text else "This file can't be read as text."}
        _write(target, result)
        return result
    want = _want_file(blob.sha256, kind)
    if not want.exists():
        _write(want, {"blob": blob.pk, "path": path})
    return {"ok": False, "pending": True, "error": PENDING}


def _worker_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(settings.BASE_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["MALLOC_ARENA_MAX"] = "2"
    return env


def draw_large(blob, path):
    """Draw a large file in a separate process with memory and time limits, and keep the result (or why it
    failed, so it isn't tried again on every run)."""
    kind = kind_of(path)
    target = _cache_file(blob.sha256, kind)
    limit = large_timeout()
    with tempfile.TemporaryDirectory(prefix="draw-", dir=cache_dir()) as tmp:
        src = os.path.join(tmp, "input")
        with blob.file.open("rb") as fh, open(src, "wb") as out:
            shutil.copyfileobj(fh, out, MB)
        cmd = [sys.executable, "-m", "apps.sources.kicad.worker", src, tmp, kind, f"k{blob.sha256[:8]}",
               str(large_memory_mb()), str(limit)]
        errpath = os.path.join(tmp, "stderr.txt")
        started = time.monotonic()
        try:
            with open(errpath, "wb") as err:
                proc = subprocess.run(cmd, cwd=str(settings.BASE_DIR), env=_worker_env(), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=err, timeout=limit)
        except subprocess.TimeoutExpired:
            result = {"ok": False, "error": f"Drawing this file took longer than {max(limit // 60, 1)} minutes, so it was stopped."}
        else:
            try:
                result = json.loads(Path(tmp, "result.json").read_text())
            except (OSError, ValueError):
                tail = Path(errpath).read_bytes()[-2000:].decode("utf-8", "replace") if os.path.exists(errpath) else ""
                log.warning("KiCad drawing of %s ended with code %s: %s", path, proc.returncode, tail)
                result = {"ok": False, "error": "The file is too large or complex to draw on this server."
                          if proc.returncode in (-9, -24, 137, 152) else "Something in this file couldn't be drawn. You can still view it as text."}
            if result.get("ok"):
                try:
                    os.replace(os.path.join(tmp, "drawing.svg"), target.with_suffix(".svg"))
                    _compress(target.with_suffix(".svg"))
                except OSError:
                    result = {"ok": False, "error": "Something in this file couldn't be drawn. You can still view it as text."}
    _write(target, result)
    _want_file(blob.sha256, kind).unlink(missing_ok=True)
    log.info("Drew large KiCad file %s in %.1f s", path, time.monotonic() - started)
    return result


_drawing = threading.Lock()  # one large drawing at a time: each can use gigabytes of memory


def _draw_wanted(block=None):
    """Start drawing the oldest large file waiting for the scheduler, in a thread so the scheduler isn't held up
    (block=True, as in tests: draw it before returning). Returns how many were started (0 or 1)."""
    from apps.sources.models import Blob
    if block is None:
        block = getattr(settings, "TESTING", False)
    if not _drawing.acquire(blocking=False):
        return 0
    try:
        job = _next_wanted(Blob)
    except Exception:
        _drawing.release()
        raise
    if job is None:
        _drawing.release()
        return 0

    def run():
        try:
            draw_large(*job)
        except Exception:
            log.exception("Couldn't draw large KiCad file %s", job[1])
        finally:
            _drawing.release()
            if not block:
                close_old_connections()
    if block:
        run()
    else:
        threading.Thread(target=run, name="kicad-large", daemon=True).start()
    return 1


def _next_wanted(Blob):
    wants = []
    for p in cache_dir().glob("want-*.json"):
        try:
            wants.append((p.stat().st_mtime_ns, p))
        except OSError:
            pass
    for _, p in sorted(wants):
        try:
            info = json.loads(p.read_text())
            blob = Blob.objects.filter(pk=int(info["blob"])).first()
            path = str(info["path"])
        except (OSError, ValueError, KeyError, TypeError):
            p.unlink(missing_ok=True)
            continue
        if blob is None or kind_of(path) is None or _cache_file(blob.sha256, kind_of(path)).exists():
            p.unlink(missing_ok=True)
            continue
        return blob, path
    return None


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
            if vf.blob.size > MAX_BYTES:
                _large(vf.blob, vf.path, svg=False)  # queued for the separate drawing below
            elif not _cache_file(vf.blob.sha256, kind).exists():
                if done >= limit:
                    break  # this one is drawn next time
                render_blob(vf.blob, vf.path)
                done += 1
            seen.add(vf.blob.sha256)
        mark = vf.pk
    if mark != start:
        _write(mark_file, str(mark))
    return done + _draw_wanted()


def prune(days=30):
    """Remove cached drawings not used for a while, leftovers of interrupted writes, and the least
    recently used drawings while the cache is larger than MAX_CACHE_BYTES."""
    now = time.time()
    cutoff = now - days * 86400
    removed, kept = 0, []
    for p in cache_dir().iterdir():
        try:
            st = p.stat()
            if p.is_dir():  # a drawing interrupted by a restart
                if p.name.startswith("draw-") and st.st_mtime < now - 2 * large_timeout() - 3600:
                    shutil.rmtree(p, ignore_errors=True)
                continue
            last = max(st.st_atime, st.st_mtime)
            if (p.suffix == ".partial" and st.st_mtime < now - 3600) or (p.suffix in (".json", ".svg", ".gz") and last < cutoff):
                p.unlink()
                removed += 1
            elif p.suffix in (".json", ".svg", ".gz") and not p.name.startswith("want-"):
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
