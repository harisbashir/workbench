"""Draws one large KiCad file in a separate process, so a huge or hostile file can't hurt the web server.

    python -m apps.sources.kicad.worker INPUT OUTDIR KIND PREFIX MEMORY_MB SECONDS

Writes OUTDIR/drawing.svg and OUTDIR/result.json: the page facts {"ok": true, …} or {"ok": false, "error": "…"}.
Doesn't import Django.
"""
import json
import sys

from apps.cad.worker import _limit, _write



def main(argv):
    src, outdir, kind, prefix, mem_mb, seconds = argv[1], argv[2], argv[3], argv[4], int(argv[5]), int(argv[6])
    _limit(mem_mb, seconds)

    def result(**r):
        _write(outdir, "result.json", json.dumps(r).encode())

    try:
        from apps.design.gerber import MAX_ELEMENTS, Budget, GerberLimitError

        from . import drawing, sexpr
        from .sexpr import KicadError
    except MemoryError:
        result(ok=False, error="The file is too large or complex to draw on this server.")
        return 0
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        del data
        size_mb = len(text) // (1024 * 1024) + 1
        sexpr.MAX_BYTES = len(text) + 1  # the size was checked before this process was started
        sexpr.MAX_NODES = max(sexpr.MAX_NODES, len(text) // 2)  # every token takes two bytes or more; memory is capped above
        out = drawing.draw(text, kind, prefix=prefix, budget=Budget(seconds=seconds, max_elements=MAX_ELEMENTS * max(1, size_mb // 25)))
        del text
        _write(outdir, "drawing.svg", out.pop("svg").encode("utf-8"))
        result(**out)
    except (KicadError, GerberLimitError) as exc:
        result(ok=False, error=str(exc)[:300])
    except (MemoryError, RecursionError):
        result(ok=False, error="The file is too large or complex to draw on this server.")
    except Exception as exc:  # noqa: BLE001 — reported to the page, details to the log
        print(f"drawing failed: {exc!r}", file=sys.stderr)
        result(ok=False, error="Something in this file couldn't be drawn. You can still view it as text.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
