"""Converting uploaded 3D files into viewer meshes, in the background.

A conversion starts in a background thread right after upload, and the
scheduler picks up anything left pending (for example after a restart).
Converted meshes are keyed by the file's SHA-256, so identical files are
only converted once.
"""
import logging
import threading

from django.apps import apps
from django.core.files.base import ContentFile
from django.db import close_old_connections

from . import formats, thumbs
from .mesh import MeshError
from .models import MeshStatus

log = logging.getLogger(__name__)
SYNC_LIMIT = 8 * 1024 * 1024  # small non-STEP files are converted during the upload itself


def preview_models():
    out = []
    for label in ("design.DesignFile", "mechanical.MechanicalFile"):
        try:
            out.append(apps.get_model(label))
        except (LookupError, ValueError):
            pass
    return out


def convert(obj):
    """Convert one file now. Returns True if the mesh is ready."""
    model = type(obj)
    # Claim the job so a thread and the scheduler don't both do it.
    if obj.mesh_status != MeshStatus.CONVERTING:
        claimed = model.objects.filter(pk=obj.pk, mesh_status=MeshStatus.PENDING).update(mesh_status=MeshStatus.CONVERTING)
        if not claimed:
            return False
    obj.mesh_status = MeshStatus.CONVERTING
    same = None
    for m in preview_models():
        same = m.objects.filter(sha256=obj.sha256, mesh_status=MeshStatus.READY).exclude(mesh="").first()
        if same:
            break
    try:
        if same is not None and same.mesh.storage.exists(same.mesh.name):
            obj.mesh.name, obj.thumb.name, obj.mesh_info = same.mesh.name, same.thumb.name, same.mesh_info
        else:
            with obj.file.open("rb") as fh:
                data = fh.read()
            mesh = formats.convert(data, obj.name)
            obj.mesh.save("mesh.wbm.gz", ContentFile(mesh.to_bytes()), save=False)
            obj.mesh_info = mesh.info()
            try:
                obj.thumb.save("thumb.png", ContentFile(thumbs.render_png(mesh)), save=False)
            except Exception:  # a missing picture shouldn't stop the 3D view
                log.exception("thumbnail failed for %s", obj.pk)
        obj.mesh_status, obj.mesh_message = MeshStatus.READY, ""
    except MeshError as exc:
        obj.mesh_status, obj.mesh_message = MeshStatus.FAILED, str(exc)[:300]
    except MemoryError:
        obj.mesh_status, obj.mesh_message = MeshStatus.FAILED, "The model is too large to prepare on this server."
    except Exception as exc:  # never leave a file stuck in "converting"
        log.exception("3D conversion failed for %s %s", model.__name__, obj.pk)
        obj.mesh_status, obj.mesh_message = MeshStatus.FAILED, f"Unexpected error while reading the file ({exc.__class__.__name__})."
    model.objects.filter(pk=obj.pk).update(mesh=obj.mesh.name or "", thumb=obj.thumb.name or "", mesh_status=obj.mesh_status,
                                          mesh_message=obj.mesh_message, mesh_info=obj.mesh_info)
    return obj.mesh_status == MeshStatus.READY


def queue(obj, background=True):
    """Call after saving a new file: sets its preview status and starts the conversion."""
    obj.initial_mesh_state()
    type(obj).objects.filter(pk=obj.pk).update(mesh_status=obj.mesh_status, mesh_message=obj.mesh_message)
    if obj.mesh_status != MeshStatus.PENDING:
        return
    brep = formats.ext_of(obj.name) in ("step", "stp", "iges", "igs")
    if not brep and (obj.size or 0) <= SYNC_LIMIT or not background:
        convert(obj)
        obj.refresh_from_db()
        return
    model, pk = type(obj), obj.pk

    def work():
        close_old_connections()
        try:
            o = model.objects.filter(pk=pk).first()
            if o is not None:
                convert(o)
        finally:
            close_old_connections()
    threading.Thread(target=work, daemon=True, name=f"cad-{pk}").start()


def run_pending(limit=5):
    """Scheduler hook: convert files still waiting."""
    done = 0
    exts = "|".join(sorted(set(formats.VIEWABLE) | set(formats.NATIVE)))
    for model in preview_models():
        # Files uploaded before 3D previews existed
        for obj in model.objects.filter(mesh_status="", name__iregex=rf"\.({exts})$")[:50]:
            obj.initial_mesh_state()
            model.objects.filter(pk=obj.pk).update(mesh_status=obj.mesh_status, mesh_message=obj.mesh_message)
        for obj in model.objects.filter(mesh_status=MeshStatus.PENDING).order_by("pk")[:limit]:
            if convert(obj):
                done += 1
    return done


def recover_interrupted():
    """At scheduler start: conversions cut off by a restart go back to the queue."""
    for model in preview_models():
        model.objects.filter(mesh_status=MeshStatus.CONVERTING).update(mesh_status=MeshStatus.PENDING)


def delete_previews(obj):
    """Call before deleting a file: removes its 3D preview files unless another file shares them."""
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
