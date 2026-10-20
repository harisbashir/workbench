"""Turning an upload (loose files, a folder or zips) into checked file paths and stored contents.

Everything here treats the upload as hostile: paths can't leave the repository, zips can't
unpack into something enormous, and file types that could run in a browser are refused.
"""
import os
import re
import stat
import struct
import zipfile
from dataclasses import dataclass, field

from django.conf import settings

from apps.core.models import SiteSettings

from . import blobs

MAX_FILES = 5000
MAX_ZIP_ENTRIES = 20_000     # entries (files and folders) in one zip; its index is read into memory
MAX_PATH = 400
MAX_RATIO = 100              # unpacked size / compressed size, for entries over 1 MB
MB = 1024 * 1024

# Program files for Windows are refused. Scripts and web files (.sh, .js, .html…) are allowed: firmware
# projects need them, and Sources never runs or serves them as web pages (downloads are plain files).
BLOCKED_EXTENSIONS = {".exe", ".msi", ".scr", ".com", ".dll", ".hta", ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".lnk", ".pif", ".cpl"}
# Control characters and the characters that reverse text direction (used to disguise file names).
BAD_CHARS = re.compile("[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# Names Windows can't save: a version with one couldn't be unzipped or opened there.
WINDOWS_CHARS = re.compile(r'[<>:"|?*]')
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)

IGNORED_DIRS = {".git", "__pycache__", "__MACOSX", ".svn", ".hg"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "fp-info-cache"}
IGNORED_RE = re.compile(r"(^_autosave-|^~.*\.lck$|\.kicad_prl$|^#auto_saved_files#$)")


def normalise_path(raw):
    """A clean relative path ("src/main.c"), or None if it could escape the repository or is unusable."""
    if not raw or BAD_CHARS.search(raw) or len(raw) > MAX_PATH or raw.endswith(("/", "\\")):
        return None
    p = raw.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    parts = [s for s in p.split("/") if s]
    if not parts or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    if any(s in (".", "..") or s != s.strip() for s in parts):
        return None
    return "/".join(parts)


def windows_problem(path):
    """Why Windows couldn't save a file at `path`, or None."""
    for part in path.split("/"):
        if WINDOWS_CHARS.search(part):
            return 'Windows can\'t save names with < > : " | ? or *. Rename it and check it in again.'
        if part.endswith((".", " ")):
            return "Windows can't save names ending in a dot or a space. Rename it and check it in again."
        if WINDOWS_RESERVED.match(part):
            return f"Windows reserves the name {part.split('.')[0].upper()}, so it can't be saved there. Rename it and check it in again."
    return None


def is_ignored(path):
    parts = path.split("/")
    if any(p in IGNORED_DIRS or p.endswith("-backups") for p in parts[:-1]):
        return True
    name = parts[-1]
    return name in IGNORED_NAMES or bool(IGNORED_RE.search(name)) or name.startswith("._")


def file_limit():
    return SiteSettings.load().max_upload_mb * MB


def total_limit():
    return settings.MAX_UPLOAD_MB * MB


@dataclass
class Intake:
    files: dict = field(default_factory=dict)      # path -> Blob
    refused: list = field(default_factory=list)    # [(path, reason)]
    ignored: int = 0
    total: int = 0
    _lower: dict = field(default_factory=dict)
    _full: bool = False

    def refuse(self, path, reason):
        self.refused.append((path, reason))

    def check(self, path, size):
        """Whether a file at `path` of `size` bytes may be added. Records refusals."""
        if path is None:
            return False
        if is_ignored(path):
            self.ignored += 1
            return False
        problem = windows_problem(path)
        if problem:
            self.refuse(path, problem)
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext in BLOCKED_EXTENSIONS:
            self.refuse(path, f"Program files ({ext}) can't be checked in for security reasons. Zip them and tick “Keep zip files as zips”.")
            return False
        if len(self.files) >= MAX_FILES:
            if not self._full:
                self.refuse(path, f"A check-in can have at most {MAX_FILES:,} files. Check the rest in separately.")
                self._full = True
            return False
        if self.total + size > total_limit():
            self.refuse(path, f"The check-in is too large (over {settings.MAX_UPLOAD_MB} MB). Check it in in parts.")
            return False
        return True

    def add(self, path, blob):
        key = path.lower()
        earlier = self._lower.get(key)
        if earlier is not None:
            self.total -= self.files.pop(earlier).size
            self.refuse(earlier, "Listed twice in this upload (names differing only in capitals count as the same); the later one is kept.")
        self._lower[key] = path
        self.files[path] = blob
        self.total += blob.size


def _join(into, name):
    return normalise_path(f"{into}/{name}" if into else name)


def zip_entry_count(fh):
    """How many entries a zip says it has, read from its end record without loading its index; None if
    that record can't be found."""
    try:
        fh.seek(0, 2)
        size = fh.tell()
        tail_len = min(size, 65_536 + 22 + 20 + 56)
        fh.seek(size - tail_len)
        tail = fh.read(tail_len)
    except (OSError, ValueError):
        return None
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) < at + 22:
        return None
    count = struct.unpack("<H", tail[at + 10:at + 12])[0]
    z64 = tail.rfind(b"PK\x06\x06", 0, at)
    if count == 0xFFFF and z64 >= 0 and len(tail) >= z64 + 40:
        count = struct.unpack("<Q", tail[z64 + 32:z64 + 40])[0]
    fh.seek(0)
    return count


def _take_zip(result, upload, into):
    count = zip_entry_count(upload)
    if count is not None and count > MAX_ZIP_ENTRIES:
        result.refuse(upload.name, f"This zip has too many files ({count:,}; the limit is {MAX_ZIP_ENTRIES:,}). Split it into smaller zips.")
        return
    try:
        z = zipfile.ZipFile(upload)
    except zipfile.BadZipFile:
        result.refuse(upload.name, "This zip can't be read.")
        return
    with z:
        for info in z.infolist():
            if info.is_dir():
                continue
            raw = info.filename
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                result.refuse(raw, "Links inside zips aren't allowed.")
                continue
            path = _join(into, raw)
            if path is None:
                result.refuse(raw, "This path isn't allowed (it could point outside the repository).")
                continue
            if info.file_size > MB and info.file_size > MAX_RATIO * max(info.compress_size, 1):
                result.refuse(path, "This file unpacks to far more than its compressed size, so it was left out.")
                continue
            if not result.check(path, info.file_size):
                continue
            if info.file_size > file_limit():
                result.refuse(path, f"Larger than the {file_limit() // MB} MB limit for one file.")
                continue
            try:
                with z.open(info) as fh:
                    blob = blobs.store(fh, min(file_limit(), total_limit() - result.total), name=path)
            except ValueError as exc:
                result.refuse(path, str(exc))
                continue
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError) as exc:
                result.refuse(path, f"Couldn't be read from the zip ({exc}).")
                continue
            result.add(path, blob)


def take(uploads, into="", keep_zips=False):
    """uploads: [(relative path or file name, UploadedFile)]. Returns an Intake."""
    result = Intake()
    into = (normalise_path(into) or "") if into else ""
    for name, upload in uploads:
        if name.lower().endswith(".zip") and not keep_zips:
            _take_zip(result, upload, into)
            continue
        path = _join(into, name)
        if path is None:
            result.refuse(name, "This path isn't allowed (it could point outside the repository).")
            continue
        if not result.check(path, upload.size):
            continue
        try:
            blob = blobs.store(upload, file_limit(), name=path)
        except ValueError as exc:
            result.refuse(path, str(exc))
            continue
        result.add(path, blob)
    return result
