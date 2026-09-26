"""Copy every stored file from one storage to another, e.g. before switching to S3.

    manage migrate_storage --to s3           # copy local folder -> S3 bucket
    manage migrate_storage --to local        # copy S3 bucket -> local folder
    manage migrate_storage --to s3 --dry-run # just show what would be copied

Files are copied, never deleted from the source. After it finishes, set
WORKBENCH_STORAGE to the new value and restart Workbench.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.storage import backend, copy_between, stored_files


class Command(BaseCommand):
    help = "Copy all uploaded files between local storage and S3."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, choices=["local", "s3"])
        parser.add_argument("--from", dest="source", choices=["local", "s3"])
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        target_kind = o["to"]
        source_kind = o["source"] or ("s3" if target_kind == "local" else "local")
        if source_kind == target_kind:
            raise CommandError("Source and target are the same.")
        try:
            source, target = backend(source_kind), backend(target_kind)
        except ValueError as e:
            raise CommandError(str(e))
        copied = skipped = failed = 0
        total_bytes = 0
        for name, size in stored_files():
            try:
                if target.exists(name) and (size is None or target.size(name) == size):
                    skipped += 1
                    continue
                if o["dry_run"]:
                    self.stdout.write(f"would copy {name}")
                    copied += 1
                    total_bytes += size or 0
                    continue
                copy_between(name, source, target)
                if size is not None and target.size(name) != size:
                    raise RuntimeError("size mismatch after copy")
                copied += 1
                total_bytes += size or 0
                if copied % 50 == 0:
                    self.stdout.write(f"  {copied} files copied…")
            except Exception as e:
                failed += 1
                self.stderr.write(f"FAILED {name}: {e}")
        verb = "Would copy" if o["dry_run"] else "Copied"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {copied} files ({total_bytes / 1024 / 1024:.1f} MB), {skipped} already there, {failed} failed."))
        if not o["dry_run"] and not failed:
            current = settings.STORAGE_KIND
            if current != target_kind:
                self.stdout.write(f"Now set WORKBENCH_STORAGE={target_kind} (in .env) and restart Workbench.")
        if failed:
            raise CommandError("Some files failed to copy. Fix the errors above and run the command again — it skips files already copied.")
