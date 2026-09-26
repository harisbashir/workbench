"""Helpers that work the same whether files live in a local folder or in S3.

All code that reads or writes uploaded files goes through Django's storage API
(never through local paths), so changing WORKBENCH_STORAGE needs no code changes.
"""
import mimetypes

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404


def is_s3(storage=None):
    storage = storage or default_storage
    return type(getattr(storage, "_wrapped", storage)).__name__ == "S3Storage" or \
        storage.__class__.__name__ == "S3Storage"


def storage_info():
    if settings.STORAGE_KIND == "s3":
        return {"kind": "s3", "label": "Amazon S3" if not settings.S3["endpoint"] else "S3-compatible storage",
                "bucket": settings.S3["bucket"], "prefix": settings.S3["prefix"],
                "region": settings.S3["region"] or "default", "endpoint": settings.S3["endpoint"]}
    return {"kind": "local", "label": "Folder on this server", "path": str(settings.FILES_DIR)}


def stored_files():
    """Every file Workbench knows about, as (storage name, size) pairs."""
    from apps.core.models import SiteSettings
    from apps.files.models import DocumentVersion
    from apps.firmware.models import FirmwareArtifact

    seen = set()
    for name, size in DocumentVersion.objects.values_list("file", "size"):
        if name and name not in seen:
            seen.add(name)
            yield name, size
    for name, size in FirmwareArtifact.objects.values_list("file", "size"):
        if name and name not in seen:
            seen.add(name)
            yield name, size
    site = SiteSettings.objects.filter(pk=1).first()
    for f in (site.logo, site.logo_dark) if site else ():
        if f and f.name not in seen:
            seen.add(f.name)
            yield f.name, None


def write_exact(name, fileobj, storage=None):
    """Save under exactly `name`, replacing anything already there."""
    storage = storage or default_storage
    if storage.exists(name):
        storage.delete(name)
    saved = storage.save(name, File(fileobj, name=name))
    if saved != name:  # pragma: no cover - storages that rename on save
        raise RuntimeError(f"Storage saved {name} as {saved}")
    return saved


def copy_between(name, source, target):
    with source.open(name, "rb") as fh:
        write_exact(name, fh, target)


def backend(kind):
    """A storage instance for 'local' or 's3', regardless of the current setting."""
    from django.utils.module_loading import import_string

    cfg = settings.STORAGE_CONFIGS.get(kind)
    if cfg is None:
        raise ValueError(f"Storage '{kind}' isn't configured. For S3, set WORKBENCH_S3_BUCKET (and credentials).")
    return import_string(cfg["BACKEND"])(**cfg.get("OPTIONS", {}))


def file_response(field_file, *, filename, inline=False, content_type=None):
    """Stream a stored file to the browser (works for local and S3)."""
    try:
        fh = field_file.storage.open(field_file.name, "rb")
    except (FileNotFoundError, OSError):
        raise Http404("The file is missing from storage.")
    except Exception as exc:  # e.g. botocore ClientError for a missing S3 object
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            raise Http404("The file is missing from storage.")
        raise
    ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    resp = FileResponse(fh, content_type=ctype, as_attachment=not inline, filename=filename)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
