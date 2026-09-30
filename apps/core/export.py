"""'Download everything' — an organised, human-readable copy of all Workbench data.

Unlike a backup (made for restoring Workbench), an export is made for people:
real folder and file names, CSV spreadsheets and plain-text chat transcripts
that open anywhere, without Workbench.

    Workbench export 2026-09-25/
      README.txt
      Shared files/<folders>/<files>
      Projects/PWR - Power management board/
        Files/<folders>/<files>              (latest versions; older ones in _older versions/ if chosen)
        Firmware/<firmware>/<version> (<status>)/<binaries> + RELEASE NOTES.md
        Hardware/<board>/<rev>/BOM.csv, Release checklist.csv, Design files/<type>/<files>
        Hardware/<board>/Block diagrams/<name> v<n>.pdf + .svg
        Hardware/System diagrams/<name> v<n>.pdf + .svg
        Hardware/Mechanical/<part> rev <x>/<type>/<files>
        Tasks.csv, Task comments.csv, Time entries.csv, Chat - #pwr.txt
      Parts/Parts.csv, Suppliers.csv, Stock movements.csv
      Production/Purchase orders.csv, Builds.csv
      Chat/<public channel>.txt
      People.csv, Audit log.csv
"""
import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("workbench")

EXPORT_DIR_NAME = "exports"


def export_dir():
    d = Path(settings.BACKUP_DIR) / EXPORT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def clean(name, fallback="untitled"):
    """Make a name safe as a file/folder name on Windows, macOS and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(name or "")).strip(" .")
    return (name or fallback)[:120]


class Writer:
    def __init__(self, z, root):
        self.z, self.root, self.used = z, root, set()

    def path(self, *parts):
        p = "/".join([self.root] + [clean(x) for x in parts])
        base, n = p, 2
        while p in self.used:  # avoid duplicate names in the zip
            stem, dot, ext = base.rpartition(".")
            p = f"{stem} ({n}).{ext}" if dot and "/" not in ext else f"{base} ({n})"
            n += 1
        self.used.add(p)
        return p

    def text(self, parts, content):
        self.z.writestr(self.path(*parts), content)

    def bytes(self, parts, content):
        self.z.writestr(self.path(*parts), content)

    def csv(self, parts, header, rows):
        buf = io.StringIO()
        buf.write("﻿")  # so Excel opens UTF-8 correctly
        from .csvsafe import writer
        w = writer(buf)
        w.writerow(header)
        w.writerows(rows)
        self.text(parts, buf.getvalue())

    def stored(self, parts, field_file):
        target = self.path(*parts)
        try:
            with field_file.storage.open(field_file.name, "rb") as src, self.z.open(target, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            return True
        except Exception as exc:
            log.warning("Export: couldn't read %s: %s", field_file.name, exc)
            self.z.writestr(target + " (MISSING).txt", f"This file couldn't be read from storage: {exc}\n")
            return False


def _folder_parts(folder):
    return [f.name for f in folder.ancestors()] + [folder.name] if folder else []


def _files(w, base, docs, include_versions):
    n = 0
    for d in docs:
        versions = list(d.versions.order_by("-number"))
        if not versions:
            continue
        w.stored(base + _folder_parts(d.folder) + [d.name], versions[0].file)
        n += 1
        if include_versions:
            for v in versions[1:]:
                w.stored(base + _folder_parts(d.folder) + ["_older versions", d.name, f"v{v.number} - {d.name}"], v.file)
    return n


def _diagram(w, base, d):
    """A diagram's newest version (and the approved one, if different) as PDF and SVG."""
    from apps.diagrams import render
    from apps.diagrams.views import title_block
    n = 0
    wanted = {d.current_version}
    if d.approved_version:
        wanted.add(d.approved_version)
    for v in d.versions.filter(number__in=wanted).select_related("created_by"):
        tag = " (approved)" if v.number == d.approved_version else (" (draft)" if d.status != "approved" else "")
        name = clean(f"{d.name} v{v.number}{tag}")
        w.bytes(base + [name + ".pdf"], render.to_pdf(v.data, title_block=title_block(d, v)))
        w.text(base + [name + ".svg"], render.to_svg(v.data))
        n += 2
    return n


def build_export(include_versions=False, user=None):
    from apps.accounts.models import User
    from apps.chat.models import Channel
    from apps.files.models import Document
    from apps.firmware.models import Firmware
    from apps.inventory.models import Part, StockMovement, Supplier
    from apps.production.models import BuildOrder, PurchaseOrder
    from apps.projects.models import Project
    from apps.timesheets.models import TimeEntry

    from .models import AuditLog, SiteSettings

    now = timezone.localtime()
    stamp = now.strftime("%Y-%m-%d %H%M")
    root = f"Workbench export {stamp}"
    final = export_dir() / f"workbench-export-{now:%Y%m%d-%H%M%S}.zip"
    tmp = final.with_suffix(".partial")
    counts = {"files": 0, "firmware": 0}
    site = SiteSettings.load()
    fmt = lambda dt: timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else ""  # noqa: E731

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        w = Writer(z, root)

        # Shared files
        counts["files"] += _files(w, ["Shared files"], Document.objects.alive().filter(project__isnull=True)
                                  .select_related("folder"), include_versions)

        for p in Project.objects.all().order_by("key"):
            pbase = ["Projects", f"{p.key} - {p.name}"]
            counts["files"] += _files(w, pbase + ["Files"], Document.objects.alive().filter(project=p).select_related("folder"),
                                      include_versions)
            # Firmware
            for fw in Firmware.objects.filter(project=p):
                for rel in fw.releases.order_by("-sort_key"):
                    rbase = pbase + ["Firmware", fw.name, f"{rel.version} ({rel.get_status_display()})"]
                    for art in rel.artifacts.all():
                        w.stored(rbase + [art.name], art.file)
                        counts["firmware"] += 1
                    lines = [f"# {fw.name} {rel.version}", "",
                             f"Status: {rel.get_status_display()}" + (f" — {rel.status_note}" if rel.status_note else ""),
                             f"Built from: {rel.git_ref or '—'}",
                             f"Compatible board revisions: {', '.join(r.title for r in rel.revisions.all()) or '—'}",
                             f"Created: {fmt(rel.created_at)} by {rel.created_by or 'GitHub'}",
                             f"Released: {fmt(rel.released_at)} by {rel.released_by or '—'}" if rel.released_at else "", "",
                             "## Files (SHA-256)", ""]
                    lines += [f"- {a.name}  {a.sha256}" for a in rel.artifacts.all()] or ["- none"]
                    lines += ["", "## Release notes", "", rel.notes or "(none)", ""]
                    w.text(rbase + ["RELEASE NOTES.md"], "\n".join(x for x in lines if x is not None))
            # Hardware: boards → revisions (BOM, checklist, design files) and diagrams; mechanical parts
            hw = pbase + ["Hardware"]
            for d in p.diagrams.select_related("board", "approved_by"):
                counts["files"] += _diagram(w, hw + ([d.board.name, "Block diagrams"] if d.board else ["System diagrams"]), d)
            for part in p.mechanical_parts.all():
                mb = hw + ["Mechanical", f"{part.name} rev {part.revision}"]
                for mf in part.files.order_by("kind", "name", "-version"):
                    if mf.is_current:
                        w.stored(mb + [mf.get_kind_display(), mf.name], mf.file)
                        counts["files"] += 1
                    elif include_versions:
                        w.stored(mb + [mf.get_kind_display(), "_older versions", f"v{mf.version} - {mf.name}"], mf.file)
            for rev in p.revisions.select_related("board"):
                rb = hw + [rev.board.name, rev.name]
                w.csv(rb + ["BOM.csv"], ["Part number", "Qty per board", "References", "Description", "Value", "Footprint",
                                         "Manufacturer", "MPN", "Supplier", "Unit cost", "Do not populate"],
                      [[l.part.ipn, l.quantity, l.references, l.part.description, l.part.value, l.part.footprint,
                        l.part.manufacturer, l.part.mpn, l.part.supplier or "", l.part.unit_cost, "yes" if l.dnp else ""]
                       for l in rev.bom_lines.select_related("part", "part__supplier")])
                w.csv(rb + ["Release checklist.csv"], ["Item", "Signed off by", "When", "Note"],
                      [[c.text, c.done_by or "", fmt(c.done_at), c.note] for c in rev.checks.select_related("done_by")])
                for df in rev.design_files.order_by("category", "name", "-version"):
                    if df.is_current:
                        w.stored(rb + ["Design files", df.get_category_display(), df.name], df.file)
                        counts["files"] += 1
                    elif include_versions:
                        w.stored(rb + ["Design files", df.get_category_display(), "_older versions", f"v{df.version} - {df.name}"], df.file)
            # Tasks, comments, time
            tasks = p.tasks.select_related("assignee", "reviewer", "revision", "created_by").order_by("number")
            w.csv(pbase + ["Tasks.csv"], ["ID", "Title", "Type", "Status", "Priority", "Assignee", "Reviewer", "Revision",
                                          "Due", "Blocked by", "Created", "Completed", "Description"],
                  [[t.key, t.title, t.get_kind_display(), t.get_status_display(), t.get_priority_display(),
                    t.assignee or "", t.reviewer or "", t.revision.title if t.revision else "", t.due_date or "",
                    t.blocked_reason, fmt(t.created_at), fmt(t.completed_at), t.description] for t in tasks])
            comments = [[t.key, fmt(c.created_at), c.author or "", c.body] for t in tasks for c in t.comments.select_related("author")]
            if comments:
                w.csv(pbase + ["Task comments.csv"], ["Task", "When", "Who", "Comment"], comments)
            entries = TimeEntry.objects.filter(project=p).select_related("user", "task", "task__project").order_by("date")
            if entries.exists():
                w.csv(pbase + ["Time entries.csv"], ["Date", "Person", "Task", "Hours", "Note"],
                      [[e.date, e.user.display_name, e.task.key if e.task else "", e.hours, e.note] for e in entries])
            ch = Channel.objects.filter(project=p).first()
            if ch:
                w.text(pbase + [f"Chat - #{ch.name}.txt"], _transcript(ch, fmt))

        # Company-wide channels (public only — private channels and direct messages stay private)
        for ch in Channel.objects.filter(kind=Channel.Kind.PUBLIC):
            w.text(["Chat", f"#{ch.name}.txt"], _transcript(ch, fmt))

        # Parts & production
        w.csv(["Parts", "Parts.csv"], ["Part number", "Description", "Category", "Value", "Footprint", "Manufacturer", "MPN",
                                       "Supplier", "Supplier SKU", "Unit cost", "Stock", "Reorder level", "Location", "Lifecycle"],
              [[x.ipn, x.description, x.get_category_display(), x.value, x.footprint, x.manufacturer, x.mpn, x.supplier or "",
                x.supplier_sku, x.unit_cost, x.stock, x.min_stock, x.location, x.get_lifecycle_display()]
               for x in Part.objects.select_related("supplier")])
        w.csv(["Parts", "Suppliers.csv"], ["Name", "Type", "Contact", "Email", "Phone", "Country", "Lead time (days)", "Website"],
              [[s.name, s.get_kind_display(), s.contact_name, s.email, s.phone, s.country, s.lead_time_days, s.website]
               for s in Supplier.objects.all()])
        w.csv(["Parts", "Stock movements.csv"], ["When", "Part", "Change", "Balance", "Reason", "Reference", "By"],
              [[fmt(m.created_at), m.part.ipn, m.delta, m.balance_after, m.get_reason_display(), m.reference, m.user or ""]
               for m in StockMovement.objects.select_related("part", "user").order_by("created_at")])
        w.csv(["Production", "Purchase orders.csv"], ["Order", "Supplier", "Status", "Supplier ref", "Part", "Qty", "Unit cost",
                                                     "Received", "Created", "Ordered"],
              [[po.number, po.supplier, po.get_status_display(), po.reference, l.part.ipn, l.quantity, l.unit_cost,
                l.received_qty, fmt(po.created_at), fmt(po.ordered_at)]
               for po in PurchaseOrder.objects.select_related("supplier") for l in po.lines.select_related("part")])
        w.csv(["Production", "Builds.csv"], ["Build", "Project", "Revision", "Quantity", "Status", "Built by", "Firmware",
                                            "Passed", "Failed", "Started", "Completed", "Notes"],
              [[b.number, b.revision.project.key, b.revision.title, b.quantity, b.get_status_display(), b.manufacturer or "In-house",
                "; ".join(str(r) for r in b.firmware_releases.all()), b.completed_qty, b.failed_qty, fmt(b.started_at),
                fmt(b.completed_at), b.notes] for b in BuildOrder.objects.select_related("revision__project", "manufacturer")])

        # People & audit
        w.csv(["People.csv"], ["Username", "Name", "Email", "Role", "Job title", "Time zone", "Active", "Last sign-in"],
              [[u.username, u.get_full_name(), u.email, u.get_role_display(), u.job_title, u.time_zone,
                "yes" if u.is_active else "no", fmt(u.last_login)] for u in User.objects.all()])
        w.csv(["Audit log.csv"], ["When", "Who", "Action", "Item", "Details", "IP address"],
              [[fmt(a.created_at), a.actor or "", a.action, a.object_repr, "; ".join(f"{k}={v}" for k, v in a.details.items()),
                a.ip_address or ""] for a in AuditLog.objects.select_related("actor").order_by("created_at")])

        w.text(["README.txt"], "\n".join([
            f"{site.company_name} — Workbench export",
            f"Created {now:%Y-%m-%d %H:%M %Z}" + (f" by {user.display_name}" if user else ""),
            "",
            "This is a readable copy of everything in Workbench: every file (latest versions"
            + (", plus older versions in '_older versions' folders" if include_versions else "") + "),",
            "all firmware releases with their binaries and release notes, BOMs, tasks, time entries,",
            "chat transcripts (project and public channels), parts, orders, builds, people and the audit log.",
            "",
            "CSV files open in Excel, LibreOffice or Google Sheets.",
            "Private channels, direct messages, passwords and security keys are NOT included.",
            "",
            "To restore Workbench itself, use a backup (System & backups → Backups), not this export.",
            "",
            f"Files: {counts['files']}   Firmware files: {counts['firmware']}",
        ]))
    tmp.replace(final)
    return final, counts


def _transcript(channel, fmt):
    lines = [f"#{channel.name} — {channel.topic}".rstrip(" —"), ""]
    for m in channel.messages.select_related("author").order_by("created_at"):
        if m.is_deleted:
            continue
        body = m.body.replace("\n", "\n    ")
        lines.append(f"[{fmt(m.created_at)}] {m.author_name}: {body}" + (f"  ({m.url})" if m.url else ""))
    return "\n".join(lines) + "\n"


def run_job(job):
    from .models import ExportJob
    job.status = ExportJob.Status.RUNNING
    job.save(update_fields=["status"])
    try:
        path, counts = build_export(job.include_versions, job.requested_by)
        job.status, job.file_name, job.size = ExportJob.Status.READY, path.name, path.stat().st_size
        job.message = f"{counts['files']} files, {counts['firmware']} firmware files"
    except Exception as exc:
        log.exception("Export failed")
        job.status, job.message = ExportJob.Status.FAILED, str(exc)[:300]
    job.finished_at = timezone.now()
    job.save()
    if job.requested_by and job.status == ExportJob.Status.READY:
        from .utils import notify
        notify(job.requested_by, "Your full export is ready to download", "/system/backups/#exports")
    return job


def recover(stale_hours=3):
    """Exports left 'running' by a restart go back in the queue; very old ones are failed."""
    from .models import ExportJob
    old = timezone.now() - timezone.timedelta(hours=stale_hours)
    ExportJob.objects.filter(status=ExportJob.Status.RUNNING, created_at__lt=old).update(
        status=ExportJob.Status.FAILED, message="Stopped (the server restarted or it took too long). Try again.")
    return ExportJob.objects.filter(status=ExportJob.Status.RUNNING).update(status=ExportJob.Status.PENDING)


def run_pending():
    from .models import ExportJob
    done = 0
    for job in ExportJob.objects.filter(status=ExportJob.Status.PENDING).order_by("created_at")[:3]:
        run_job(job)
        done += 1
    return done


def prune_exports(keep=5):
    from .models import ExportJob
    files = sorted(export_dir().glob("workbench-export-*.zip"), reverse=True)
    for f in files[keep:]:
        f.unlink(missing_ok=True)
        ExportJob.objects.filter(file_name=f.name).update(status=ExportJob.Status.FAILED, message="Deleted (only the newest exports are kept)")
