"""Comparing versions and files."""
import difflib

DIFF_BYTES = 1024 * 1024
DIFF_LINES = 20_000


def lines_of(blob):
    if blob is None:
        return []
    text = blob.text(limit=DIFF_BYTES)
    if text is None:
        return None
    lines = text.splitlines()
    return lines if len(lines) <= DIFF_LINES else None


def count_lines(old, new):
    """(lines added, lines removed) between two blobs; (0, 0) when either is binary or too large."""
    a, b = lines_of(old), lines_of(new)
    if a is None or b is None:
        return 0, 0
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed
