"""Flashing bundle: every image a board needs, in flashing order, plus a manifest a flashing station can read."""
import json
import zipfile
from collections import namedtuple

from django.conf import settings
from django.utils import timezone

from .models import FirmwareArtifact, FirmwareRelease

BundleRow = namedtuple("BundleRow", "label designator release flash_order")
FLASHABLE = (FirmwareArtifact.Kind.IMAGE, FirmwareArtifact.Kind.BOOTLOADER, FirmwareArtifact.Kind.PACKAGE)


class BundleError(Exception):
    pass


def write_bundle(rows, fh, title=""):
    """Write the bundle zip for `rows` to the binary file `fh` and return the manifest."""
    rows = sorted(rows, key=lambda r: (r.designator or "~", r.flash_order, r.release.firmware.name))
    for r in rows:
        if r.release.status == FirmwareRelease.Status.RECALLED:
            raise BundleError(f"{r.release.firmware.name} {r.release.version} is recalled. Choose another version first.")
    files = [(r, a) for r in rows for a in r.release.artifacts.all() if a.kind in FLASHABLE]
    if not files:
        raise BundleError("There are no firmware files to download yet.")
    total, limit = sum(a.size for _, a in files), settings.MAX_UPLOAD_MB * 1024 * 1024
    if total > limit:
        raise BundleError(f"The firmware files are too large to download together ({total // (1024 * 1024)} MB; "
                          f"the limit is {settings.MAX_UPLOAD_MB} MB). Download them one by one from each release.")
    manifest = {"title": title, "created": timezone.now().isoformat(), "items": []}
    folders = {}
    with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for r, a in files:
            if r not in folders:
                rel = r.release
                folders[r] = f"{len(folders) + 1:02d}-{r.designator or 'board'}-{rel.firmware.slug}-{rel.version}"
            path = f"{folders[r]}/{a.name}"
            with a.file.open("rb") as src, z.open(path, "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
            manifest["items"].append({
                "controller": r.label, "designator": r.designator, "firmware": r.release.firmware.name,
                "version": r.release.version, "status": r.release.status, "file": path, "sha256": a.sha256,
                "size": a.size, "flash_order": r.flash_order})
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest
