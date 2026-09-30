"""Converting uploaded 3D files into viewer meshes, in the background.

Uploads never convert in the request: `queue()` marks the file pending and starts a
background thread, which runs the conversion in a separate *process* (apps/cad/worker.py)
with memory, CPU and wall-clock limits. A hostile file or a crash of the STEP converter
therefore only ends that process, never the web server or the scheduler.

`tick()` is called by the scheduler on every pass: it picks up pending files (up to
CAD_MAX_PARALLEL at a time), and retries or fails conversions whose process died.
Converted meshes are keyed by the file's SHA-256, so identical files are only converted once.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import formats
from .models import MeshJob, MeshStatus

log = logging.getLogger(__name__)


def _setting(name, env, default):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(env, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def timeout_seconds():
    return _setting("CAD_CONVERT_TIMEOUT", "WORKBENCH_CAD_TIMEOUT", 300)


def memory_mb():
    return _setting("CAD_CONVERT_MEMORY_MB", "WORKBENCH_CAD_MEMORY_MB", 2048)


def max_parallel():
    return max(1, _setting("CAD_MAX_PARALLEL", "WORKBENCH_CAD_WORKERS", 2))


def max_attempts():
    return max(1, _setting("CAD_MAX_ATTEMPTS", "WORKBENCH_CAD_ATTEMPTS", 3))


def stale_after():
    """A conversion older than this has certainly lost its process."""
    return timedelta(seconds=timeout_seconds() + 120)


def preview_models():
    out = []
    for label in ("design.DesignFile", "mechanical.MechanicalFile"):
        try:
            out.append(apps.get_model(label))
        except (LookupError, ValueError):
            pass
    return out


def _label(model):
    return model._meta.label


def converting_count():
    return sum(m.objects.filter(mesh_status=MeshStatus.CONVERTING).count() for m in preview_models())


def _claim(obj, respect_limit=True):
    """Mark the file as converting (only one process may do it). Returns False if it's taken or no slot is free."""
    model = type(obj)
    with transaction.atomic():
        if respect_limit and converting_count() >= max_parallel():
            return False
        if not model.objects.filter(pk=obj.pk, mesh_status=MeshStatus.PENDING).update(mesh_status=MeshStatus.CONVERTING):
            return False
        job, _ = MeshJob.objects.get_or_create(model=_label(model), object_id=obj.pk)
        job.attempts += 1
        job.started_at = timezone.now()
        job.save()
    obj.mesh_status = MeshStatus.CONVERTING
    return True


# --- the worker process ------------------------------------------------------------------------------------

def _worker_command(src, outdir, filename):
    cpu = timeout_seconds() * 4  # the STEP converter uses several threads; the wall-clock limit is the real one
    return [sys.executable, "-m", "apps.cad.worker", src, outdir, filename, str(memory_mb()), str(cpu)]


def _worker_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(settings.BASE_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Keep the address space small: one BLAS thread, few malloc arenas.
    env.update({"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2"})
    return env


def run_worker(obj):
    """Convert obj's file in a separate process. Returns (result dict, mesh bytes, thumb bytes)."""
    with tempfile.TemporaryDirectory(prefix="wb-cad-") as tmp:
        src = os.path.join(tmp, "input")
        with obj.file.open("rb") as fh, open(src, "wb") as out:
            shutil.copyfileobj(fh, out, 1024 * 1024)
        errpath = os.path.join(tmp, "stderr.txt")
        limit = timeout_seconds()
        try:
            with open(errpath, "wb") as err:
                proc = subprocess.run(_worker_command(src, tmp, obj.name), cwd=str(settings.BASE_DIR), env=_worker_env(),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, timeout=limit)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"Preparing the 3D view took longer than {limit} seconds, so it was stopped. "
                                            "Export the model with a coarser resolution or fewer parts."}, None, None
        result_path = Path(tmp) / "result.json"
        try:
            res = json.loads(result_path.read_text())
        except (OSError, ValueError):
            tail = Path(errpath).read_bytes()[-2000:].decode("utf-8", "replace")
            log.warning("3D converter for %s %s ended with code %s: %s", type(obj).__name__, obj.pk, code, tail)
            if code in (-9, -24, 137, 152):  # SIGKILL / SIGXCPU: over the memory or CPU limit
                msg = "The model is too large or complex to prepare on this server."
            else:
                msg = "The 3D converter crashed on this file. Check that it opens in your CAD program, or export it again."
            return {"ok": False, "message": msg}, None, None
        if not res.get("ok"):
            return res, None, None
        mesh = (Path(tmp) / "mesh.wbm.gz").read_bytes()
        thumb_path = Path(tmp) / "thumb.png"
        thumb = thumb_path.read_bytes() if thumb_path.exists() else None
        return res, mesh, thumb


def convert(obj, respect_limit=True):
    """Convert one pending file now (in this thread, the work itself in a subprocess). Returns True if the mesh is ready."""
    model = type(obj)
    if not _claim(obj, respect_limit=respect_limit):
        return False
    try:
        same = None
        for m in preview_models():
            same = m.objects.filter(sha256=obj.sha256, mesh_status=MeshStatus.READY).exclude(mesh="").first()
            if same:
                break
        if same is not None and same.mesh.storage.exists(same.mesh.name):
            obj.mesh.name, obj.thumb.name, obj.mesh_info = same.mesh.name, same.thumb.name, same.mesh_info
            obj.mesh_status, obj.mesh_message = MeshStatus.READY, ""
        else:
            res, mesh, thumb = run_worker(obj)
            if res.get("ok"):
                obj.mesh.save("mesh.wbm.gz", ContentFile(mesh), save=False)
                if thumb:
                    obj.thumb.save("thumb.png", ContentFile(thumb), save=False)
                obj.mesh_info = res.get("info") or {}
                obj.mesh_status, obj.mesh_message = MeshStatus.READY, ""
            else:
                obj.mesh_status, obj.mesh_message = MeshStatus.FAILED, str(res.get("message") or "The file couldn't be read.")[:300]
    except Exception as exc:  # never leave a file stuck in "converting"
        log.exception("3D conversion failed for %s %s", model.__name__, obj.pk)
        obj.mesh_status, obj.mesh_message = MeshStatus.FAILED, f"Unexpected error while preparing the 3D view ({exc.__class__.__name__})."
    model.objects.filter(pk=obj.pk).update(mesh=obj.mesh.name or "", thumb=obj.thumb.name or "", mesh_status=obj.mesh_status,
                                          mesh_message=obj.mesh_message, mesh_info=obj.mesh_info)
    MeshJob.objects.filter(model=_label(model), object_id=obj.pk).delete()
    return obj.mesh_status == MeshStatus.READY


def _start_thread(model, pk):
    def work():
        close_old_connections()
        try:
            o = model.objects.filter(pk=pk).first()
            if o is not None:
                convert(o)
        except Exception:
            log.exception("3D conversion thread failed")
        finally:
            close_old_connections()
    t = threading.Thread(target=work, daemon=True, name=f"cad-{pk}")
    t.start()
    return t


def queue(obj, background=True):
    """Call after saving a new file: sets its preview status and starts the conversion.

    With background=False (demo data, tests) the conversion runs before this returns — still in a subprocess.
    """
    obj.initial_mesh_state()
    type(obj).objects.filter(pk=obj.pk).update(mesh_status=obj.mesh_status, mesh_message=obj.mesh_message)
    if obj.mesh_status != MeshStatus.PENDING:
        return
    if not background:
        convert(obj, respect_limit=False)
        obj.refresh_from_db()
        return
    # The request only starts a thread that waits for the worker process; if every slot is busy
    # the file stays pending and the scheduler's tick() picks it up.
    if converting_count() < max_parallel():
        _start_thread(type(obj), obj.pk)


# --- scheduler -------------------------------------------------------------------------------------------

def recover_stuck(now=None):
    """Conversions whose process died (restart, crash): retry them, or fail them after max_attempts()."""
    now = now or timezone.now()
    handled = 0
    for model in preview_models():
        label = _label(model)
        converting = set(model.objects.filter(mesh_status=MeshStatus.CONVERTING).values_list("pk", flat=True))
        jobs = {j.object_id: j for j in MeshJob.objects.filter(model=label)}
        for pk in converting:
            job = jobs.get(pk)
            if job is not None and job.started_at and job.started_at > now - stale_after():
                continue  # still within its time limit
            handled += 1
            if job is not None and job.attempts >= max_attempts():
                model.objects.filter(pk=pk, mesh_status=MeshStatus.CONVERTING).update(
                    mesh_status=MeshStatus.FAILED,
                    mesh_message=f"Preparing the 3D view failed {job.attempts} times (the converter stopped without an answer). "
                                 "Upload the file again, or export it with a coarser resolution.")
                job.delete()
            else:
                model.objects.filter(pk=pk, mesh_status=MeshStatus.CONVERTING).update(mesh_status=MeshStatus.PENDING)
        # jobs of files that were deleted or finished without cleaning up
        MeshJob.objects.filter(model=label).exclude(object_id__in=converting).delete()
    return handled


def tick(block=False, limit=None):
    """Scheduler hook, call it on every pass. Returns how many conversions it started.

    Recovers stuck conversions, then starts pending ones while slots are free (in threads, so the
    scheduler isn't held up; block=True converts them one after the other before returning).
    """
    recover_stuck()
    exts = "|".join(sorted(set(formats.VIEWABLE) | set(formats.NATIVE)))
    started = 0
    for model in preview_models():
        # Files uploaded before 3D previews existed
        for obj in model.objects.filter(mesh_status="", name__iregex=rf"\.({exts})$")[:50]:
            obj.initial_mesh_state()
            model.objects.filter(pk=obj.pk).update(mesh_status=obj.mesh_status, mesh_message=obj.mesh_message)
    for model in preview_models():
        for obj in model.objects.filter(mesh_status=MeshStatus.PENDING).order_by("pk")[: limit or max_parallel()]:
            if converting_count() >= max_parallel():
                return started
            if block:
                convert(obj)
            else:
                _start_thread(model, obj.pk)
            started += 1
    return started


def run_pending(limit=5):
    """Older name of tick(), kept for the scheduler."""
    return tick(limit=limit)


def recover_interrupted():
    """At scheduler start. Only conversions past their time limit are touched, so work in progress elsewhere continues."""
    return recover_stuck()


def delete_previews(obj):
    """Call before deleting a file: removes its 3D preview files unless another file shares them."""
    MeshJob.objects.filter(model=_label(type(obj)), object_id=obj.pk).delete()
    for field in ("mesh", "thumb"):
        f = getattr(obj, field)
        if not f:
            continue
        shared = any(m.objects.filter(**{field: f.name}).exclude(pk=obj.pk if isinstance(obj, m) else None).exists()
                     for m in preview_models())
        if not shared:
            try:
                f.delete(save=False)
            except Exception:  # a missing preview is not a problem
                log.warning("couldn't delete preview %s", f.name)
