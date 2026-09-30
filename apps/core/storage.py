"""Where uploaded files live, and helpers that work the same for every storage.

The storage is chosen by an administrator under System → Storage and saved in
the database (secrets encrypted), so it survives restarts and updates. It can
also be pinned in the server's .env (WORKBENCH_STORAGE=…), which then wins.

All code reads and writes files through Django's storage API (never local
paths), via DynamicStorage below, which forwards every call to the storage
that is active right now. While files are being moved, and afterwards until an
admin says the old storage can be forgotten, a file that isn't found in the
active storage is looked up in the previous one, so nothing ever goes missing.
"""
import copy
import mimetypes
import time

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.db import DatabaseError
from django.http import FileResponse, Http404
from django.utils.deconstruct import deconstructible

# --- Providers -----------------------------------------------------------------------

# Every provider below speaks the S3 protocol. `endpoint` may contain {region}.
PROVIDERS = {
    "aws": {"label": "Amazon S3", "endpoint": "", "region_hint": "e.g. ca-central-1", "sse": True,
            "help": "Create a private bucket, then an IAM user (or give the server an IAM role) with access to it."},
    "r2": {"label": "Cloudflare R2", "endpoint": "https://ACCOUNT_ID.r2.cloudflarestorage.com", "region": "auto",
           "help": "R2 → Manage API tokens → Create token with Object Read & Write. Replace ACCOUNT_ID in the endpoint."},
    "b2": {"label": "Backblaze B2", "endpoint": "https://s3.{region}.backblazeb2.com", "region_hint": "e.g. us-west-004",
           "help": "Use an Application Key limited to the bucket. The region is part of the bucket's S3 endpoint."},
    "wasabi": {"label": "Wasabi", "endpoint": "https://s3.{region}.wasabisys.com", "region_hint": "e.g. ca-central-1",
               "help": "Create an access key under Access Keys; use the bucket's region."},
    "spaces": {"label": "DigitalOcean Spaces", "endpoint": "https://{region}.digitaloceanspaces.com", "region_hint": "e.g. tor1",
               "help": "API → Spaces Keys → Generate. The region is where the Space was created."},
    "gcs": {"label": "Google Cloud Storage", "endpoint": "https://storage.googleapis.com", "region": "auto",
            "help": "Cloud Storage → Settings → Interoperability → create an HMAC key for a service account with access to the bucket."},
    "minio": {"label": "MinIO, NAS or other S3-compatible", "endpoint": "", "path_style": True,
              "help": "Self-hosted MinIO, Synology/QNAP S3 servers, Ceph, and any other service with an S3 API. Enter its address."},
}
KINDS = {"local": "Folder on this server", "s3": "Cloud / S3-compatible storage"}


def endpoint_for(cfg):
    if cfg.get("endpoint"):
        return cfg["endpoint"].rstrip("/")
    tmpl = PROVIDERS.get(cfg.get("provider") or "aws", {}).get("endpoint", "")
    if "{region}" in tmpl and cfg.get("region"):
        return tmpl.format(region=cfg["region"])
    return "" if "{" in tmpl or "ACCOUNT_ID" in tmpl else tmpl


def default_local():
    return {"kind": "local", "path": str(settings.FILES_DIR)}


# --- Building a storage from a saved configuration -------------------------------------

SECRET_KEYS = ("access_key", "secret_key")


def seal(cfg):
    """Encrypt the secrets in a config before it is saved to the database."""
    from .crypto import encrypt
    out = dict(cfg)
    for k in SECRET_KEYS:
        if out.get(k) and not str(out[k]).startswith("enc:"):
            out[k] = "enc:" + encrypt(out[k])
    return out


def unseal(cfg):
    from .crypto import decrypt
    out = dict(cfg or {})
    for k in SECRET_KEYS:
        if str(out.get(k, "")).startswith("enc:"):
            out[k] = decrypt(out[k][4:])
    return out


def build(cfg):
    """A storage instance for a (decrypted) config dict."""
    cfg = cfg or default_local()
    if cfg.get("kind") == "s3":
        from botocore.config import Config
        from storages.backends.s3 import S3Storage

        provider = cfg.get("provider") or "aws"
        info = PROVIDERS.get(provider, PROVIDERS["minio"])
        opts = {
            "bucket_name": cfg["bucket"],
            "location": (cfg.get("prefix") or "").strip("/"),
            "default_acl": None,          # objects stay private
            "querystring_auth": True,
            "file_overwrite": True,       # paths are unique (document id + version)
        }
        client = {"retries": {"max_attempts": 5, "mode": "standard"}, "connect_timeout": 10, "read_timeout": 120}
        if info.get("sse"):
            opts["object_parameters"] = {"ServerSideEncryption": "AES256"}
        else:
            # Non-AWS services differ in which newer checksum headers they accept.
            client.update(request_checksum_calculation="when_required", response_checksum_validation="when_required")
        if info.get("path_style"):
            client["s3"] = {"addressing_style": "path"}
        opts["client_config"] = Config(**client)
        region = cfg.get("region") or info.get("region")
        if region:
            opts["region_name"] = region
        endpoint = endpoint_for(cfg)
        if endpoint:
            opts["endpoint_url"] = endpoint
        if cfg.get("access_key"):  # otherwise the server's IAM role / environment credentials are used
            opts["access_key"] = cfg["access_key"]
            opts["secret_key"] = cfg.get("secret_key", "")
        return S3Storage(**opts)
    return FileSystemStorage(location=cfg.get("path") or str(settings.FILES_DIR))


def describe(cfg):
    """Human-readable facts about a config, for pages and logs (never secrets)."""
    cfg = cfg or default_local()
    if cfg.get("kind") == "s3":
        info = PROVIDERS.get(cfg.get("provider") or "aws", PROVIDERS["minio"])
        return {"kind": "s3", "provider": cfg.get("provider") or "aws", "label": info["label"], "bucket": cfg.get("bucket", ""),
                "prefix": (cfg.get("prefix") or "").strip("/"), "region": cfg.get("region") or info.get("region") or "",
                "endpoint": endpoint_for(cfg), "keys": bool(cfg.get("access_key")),
                "where": f"{info['label']} · {cfg.get('bucket', '')}" + (f"/{cfg['prefix'].strip('/')}" if cfg.get("prefix") else "")}
    path = cfg.get("path") or str(settings.FILES_DIR)
    return {"kind": "local", "label": KINDS["local"], "path": path, "where": path,
            "default": path == str(settings.FILES_DIR)}


def same_place(a, b):
    a, b = describe(a), describe(b)
    if a["kind"] != b["kind"]:
        return False
    if a["kind"] == "local":
        return a["path"].rstrip("/") == b["path"].rstrip("/")
    return (a["bucket"], a["prefix"], a["endpoint"]) == (b["bucket"], b["prefix"], b["endpoint"])


# --- What is active right now -----------------------------------------------------------

_TTL = 0.0 if getattr(settings, "TESTING", False) else 3.0  # other web workers notice a switch within this time
_state = {"checked": 0.0, "rev": None, "active": None, "previous": None, "cfg": None}


def env_config():
    """Storage pinned in the server configuration (.env), or None."""
    return getattr(settings, "STORAGE_ENV", None)


def reset_cache():
    _state.update(checked=0.0, rev=None, active=None, previous=None, cfg=None)


def _refresh():
    now = time.monotonic()
    if _state["active"] is not None and now - _state["checked"] < _TTL:
        return
    pinned = env_config()
    if pinned:
        if _state["active"] is None:
            _state.update(active=build(pinned), previous=None, cfg=pinned, rev="env")
        _state["checked"] = now
        return
    from .models import StorageSettings
    try:
        row = StorageSettings.objects.filter(pk=1).values("rev", "active", "previous").first()
    except DatabaseError:  # before migrations have run
        row = None
    rev = row["rev"] if row else 0
    if rev != _state["rev"] or _state["active"] is None:
        active = unseal(row["active"]) if row and row["active"] else default_local()
        prev = unseal(row["previous"]) if row and row["previous"] else None
        _state.update(active=build(active), previous=build(prev) if prev else None, cfg=active, rev=rev)
    _state["checked"] = now


def active_storage():
    _refresh()
    return _state["active"]


def previous_storage():
    _refresh()
    return _state["previous"]


def active_config():
    _refresh()
    return copy.deepcopy(_state["cfg"])


def _missing(exc):
    if isinstance(exc, (FileNotFoundError, IsADirectoryError)):
        return True
    text = str(exc)
    return "NoSuchKey" in text or "404" in text or "Not Found" in text


@deconstructible
class DynamicStorage(Storage):
    """The project's default storage: forwards to whatever storage is active."""

    def _open(self, name, mode="rb"):
        try:
            return active_storage().open(name, mode)
        except Exception as exc:
            prev = previous_storage()
            if prev is not None and "w" not in mode and _missing(exc):
                return prev.open(name, mode)
            raise

    def save(self, name, content, max_length=None):
        return active_storage().save(name, content, max_length=max_length)

    def delete(self, name):
        active_storage().delete(name)
        prev = previous_storage()
        if prev is not None:
            try:
                prev.delete(name)
            except Exception:  # the old storage may be gone; the file is gone from the active one
                pass

    def exists(self, name):
        if active_storage().exists(name):
            return True
        prev = previous_storage()
        return bool(prev is not None and prev.exists(name))

    def size(self, name):
        try:
            return active_storage().size(name)
        except Exception as exc:
            prev = previous_storage()
            if prev is not None and _missing(exc):
                return prev.size(name)
            raise

    def listdir(self, path):
        return active_storage().listdir(path)

    def path(self, name):
        return active_storage().path(name)

    def url(self, name):
        return active_storage().url(name)

    def get_modified_time(self, name):
        return active_storage().get_modified_time(name)

    def get_created_time(self, name):
        return active_storage().get_created_time(name)

    def get_accessed_time(self, name):
        return active_storage().get_accessed_time(name)

    def get_valid_name(self, name):
        return active_storage().get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        return active_storage().get_available_name(name, max_length=max_length)

    def generate_filename(self, filename):
        return active_storage().generate_filename(filename)


# --- Helpers used around the app -----------------------------------------------------------

def is_s3(storage=None):
    if storage is None or isinstance(getattr(storage, "_wrapped", storage), DynamicStorage):
        storage = active_storage()
    return type(getattr(storage, "_wrapped", storage)).__name__ == "S3Storage"


def local_root():
    """The folder files are kept in when the active storage is local, else None."""
    cfg = active_config()
    return None if cfg.get("kind") == "s3" else (cfg.get("path") or str(settings.FILES_DIR))


def storage_info():
    info = describe(active_config())
    info["pinned"] = bool(env_config())
    return info


def file_totals():
    """How many files Workbench stores and their size, in total and by kind (all versions)."""
    from django.apps import apps
    from django.db.models import Count, Sum
    kinds = [("files.DocumentVersion", "Files & chat attachments"), ("firmware.FirmwareArtifact", "Firmware"),
             ("design.DesignFile", "PCB design files"), ("mechanical.MechanicalFile", "Mechanical CAD")]
    rows, count, size = [], 0, 0
    for label, name in kinds:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        agg = model.objects.aggregate(n=Count("pk"), s=Sum("size"))
        n, sz = agg["n"] or 0, agg["s"] or 0
        rows.append({"label": name, "count": n, "size": sz})
        count += n
        size += sz
    return {"count": count, "size": size, "by_kind": rows}


def stored_files():
    """Every file Workbench knows about, as (storage name, size) pairs."""
    from apps.core.models import SiteSettings
    from apps.files.models import DocumentVersion
    from apps.firmware.models import FirmwareArtifact

    seen = set()
    sources = [DocumentVersion.objects.values_list("file", "size"), FirmwareArtifact.objects.values_list("file", "size")]
    from django.apps import apps
    if apps.is_installed("apps.design"):
        sources.append(apps.get_model("design", "DesignFile").objects.values_list("file", "size"))
    if apps.is_installed("apps.mechanical"):
        sources.append(apps.get_model("mechanical", "MechanicalFile").objects.values_list("file", "size"))
    # 3D previews can be rebuilt, but copying them saves converting every model again
    for label in ("design.DesignFile", "mechanical.MechanicalFile"):
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        for field in ("mesh", "thumb"):
            names = model.objects.exclude(**{field: ""}).values_list(field, flat=True).distinct()
            sources.append([(n, None) for n in names])
    for qs in sources:
        for name, size in qs:
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


def test_storage(cfg):
    """Write, read back and delete a small file. Returns (ok, message)."""
    import uuid
    probe = f".workbench-check-{uuid.uuid4().hex[:8]}.txt"
    body = b"Workbench storage check\n"
    try:
        st = build(cfg)
        if cfg.get("kind") != "s3":
            import os
            root = cfg.get("path") or str(settings.FILES_DIR)
            if not os.path.isdir(root):
                return False, f"The folder {root} doesn't exist inside the container. Mount it first (see the help)."
        from io import BytesIO
        saved = st.save(probe, File(BytesIO(body), name=probe))
        with st.open(saved, "rb") as fh:
            back = fh.read()
        st.delete(saved)
        if back != body:
            return False, "A test file was written but read back differently."
        return True, "Connected: a test file was written, read back and deleted."
    except Exception as exc:  # show the useful part of the error
        return False, explain_error(exc)


def explain_error(exc):
    text = str(exc)
    hints = [
        ("InvalidAccessKeyId", "The access key ID isn't recognised."),
        ("SignatureDoesNotMatch", "The secret access key is wrong."),
        ("NoSuchBucket", "That bucket doesn't exist (check the name and region)."),
        ("AccessDenied", "Access denied: the key doesn't have permission to read and write this bucket."),
        ("PermanentRedirect", "The bucket is in a different region than the one entered."),
        ("AuthorizationHeaderMalformed", "The region doesn't match the bucket's region."),
        ("Could not connect", "Couldn't reach the storage address. Check the endpoint and the server's internet access."),
        ("Unable to locate credentials", "No access key was given and the server has no IAM role."),
        ("Permission denied", "Workbench can't write to that folder (check its owner and permissions)."),
    ]
    for needle, message in hints:
        if needle in text:
            return message
    return f"{type(exc).__name__}: {text[:240]}"


def file_response(field_file, *, filename, inline=False, content_type=None):
    """Stream a stored file to the browser (works for every storage)."""
    try:
        fh = field_file.storage.open(field_file.name, "rb")
    except Exception as exc:
        if _missing(exc) or isinstance(exc, OSError):
            raise Http404("The file is missing from storage.")
        raise
    ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    resp = FileResponse(fh, content_type=ctype, as_attachment=not inline, filename=filename)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
