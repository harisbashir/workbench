"""Converts one 3D file in a separate process, so a hostile or huge file can't hurt the web server.

    python -m apps.cad.worker INPUT OUTDIR FILENAME MEMORY_MB CPU_SECONDS

Writes OUTDIR/mesh.wbm.gz, OUTDIR/thumb.png (optional) and OUTDIR/result.json:
{"ok": true, "info": {...}} or {"ok": false, "message": "..."}.
Doesn't import Django: it only needs numpy, Pillow and (for STEP/IGES) cascadio.
"""
import json
import os
import resource
import sys


def _limit(mem_mb, cpu_s):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
    if mem_mb > 0:
        b = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (b, b))
    if cpu_s > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))


def _write(outdir, name, data):
    tmp = os.path.join(outdir, name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, os.path.join(outdir, name))


def main(argv):
    src, outdir, filename, mem_mb, cpu_s = argv[1], argv[2], argv[3], int(argv[4]), int(argv[5])
    _limit(mem_mb, cpu_s)

    def result(**r):
        _write(outdir, "result.json", json.dumps(r).encode())

    try:
        from apps.cad import formats, thumbs
        from apps.cad.mesh import MeshError
    except MemoryError:
        result(ok=False, message="The 3D converter couldn't start within its memory limit.")
        return 0
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        mesh = formats.convert(data, filename)
        del data
        _write(outdir, "mesh.wbm.gz", mesh.to_bytes())
        try:
            _write(outdir, "thumb.png", thumbs.render_png(mesh))
        except Exception as exc:  # a missing picture shouldn't stop the 3D view
            print(f"thumbnail failed: {exc!r}", file=sys.stderr)
        result(ok=True, info=mesh.info())
    except MeshError as exc:
        result(ok=False, message=str(exc)[:300])
    except MemoryError:
        result(ok=False, message="The model is too large to prepare on this server.")
    except RecursionError:
        result(ok=False, message="The file is nested too deeply to read.")
    except Exception as exc:
        result(ok=False, message=f"Unexpected error while reading the file ({exc.__class__.__name__}).")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
