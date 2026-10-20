"""Comparing versions and files."""
import difflib
import time

from django.utils.html import escape
from django.utils.safestring import mark_safe

from . import highlight

DIFF_BYTES = 1024 * 1024
DIFF_LINES = 20_000
DIFF_SECONDS = 2.0      # one file
PAGE_SECONDS = 15.0     # all files on one compare page


class DiffTimeout(Exception):
    pass


class Budget:
    """Time allowance shared by several diffs (a compare page or a check-in)."""

    def __init__(self, seconds=PAGE_SECONDS):
        self.deadline = time.monotonic() + seconds

    @property
    def left(self):
        return self.deadline - time.monotonic()


class _Matcher(difflib.SequenceMatcher):
    """SequenceMatcher that gives up at a deadline instead of running for minutes on hostile input."""

    def __init__(self, a, b, deadline):
        self._deadline = deadline
        self._calls = 0
        super().__init__(None, a, b, autojunk=len(a) + len(b) > 4000)

    def find_longest_match(self, *args, **kwargs):
        self._calls += 1
        if self._calls & 63 == 0 or self._calls == 1:
            if time.monotonic() > self._deadline:
                raise DiffTimeout
        return super().find_longest_match(*args, **kwargs)


def split_lines(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def text_of(blob):
    """The text of a blob for comparing: "" for no file, None when binary or too large."""
    if blob is None:
        return ""
    text = blob.text(limit=DIFF_BYTES)
    if text is None or text.count("\n") > DIFF_LINES:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _opcodes(a, b, budget=None):
    """Raises DiffTimeout past DIFF_SECONDS (or what's left of `budget`)."""
    seconds = DIFF_SECONDS if budget is None else min(DIFF_SECONDS, budget.left)
    if seconds <= 0:
        raise DiffTimeout
    if not a or not b:  # nothing to match: all added or all removed
        return [("insert", 0, 0, 0, len(b))] if b else ([("delete", 0, len(a), 0, 0)] if a else [])
    return _Matcher(a, b, time.monotonic() + seconds).get_opcodes()


def count_lines(old, new, budget=None):
    """(lines added, lines removed) between two blobs; (0, 0) when either is binary, too large or too slow to compare."""
    a, b = text_of(old), text_of(new)
    if a is None or b is None:
        return 0, 0
    try:
        ops = _opcodes(split_lines(a), split_lines(b), budget)
    except DiffTimeout:
        return 0, 0
    added = removed = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def changes(old_tree, new_tree):
    out = []
    for path in sorted(set(old_tree) | set(new_tree)):
        old, new = old_tree.get(path), new_tree.get(path)
        if old is not None and new is not None and old.pk == new.pk:
            continue
        status = "added" if old is None else "deleted" if new is None else "changed"
        out.append({"path": path, "status": status, "old": old, "new": new})
    return out


def _fold(n):
    return ("fold", None, None, mark_safe(f"{n} unchanged line{'s' if n != 1 else ''}"))


def file_diff(path, old, new, context=3, budget=None):
    """Rows for showing a file's changes.

    rows:  unified, [(tag, old line no, new line no, html)] with tag same/add/del/fold
    split: side by side, [(left (no, html, tag) or None, right (no, html, tag) or None)] or ("fold", text)
    """
    if (old is not None and not old.is_text) or (new is not None and not new.is_text):
        return {"kind": "binary", "rows": [], "split": [], "added": 0, "removed": 0}
    a_text, b_text = text_of(old), text_of(new)
    if a_text is None or b_text is None or a_text.count("\n") > DIFF_LINES or b_text.count("\n") > DIFF_LINES:
        return {"kind": "too_large", "rows": [], "split": [], "added": 0, "removed": 0}
    a, b = split_lines(a_text), split_lines(b_text)
    try:
        ops = _opcodes(a, b, budget)
    except DiffTimeout:
        return {"kind": "too_large", "rows": [], "split": [], "added": 0, "removed": 0}
    if budget is not None and budget.left < 3:  # running out of time: skip the colours
        ha, hb = [escape(x) for x in a], [escape(x) for x in b]
    else:
        ha = highlight.render(path, a_text)[0] if a else []
        hb = highlight.render(path, b_text)[0] if b else []
    rows, split, added, removed = [], [], 0, 0
    last = len(ops) - 1
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            size = i2 - i1
            head = 0 if n == 0 else context
            tail = 0 if n == last else context
            if size > head + tail + 1:
                keep = [(i1 + k, j1 + k) for k in range(head)]
                for i, j in keep:
                    rows.append(("same", i + 1, j + 1, hb[j]))
                    split.append(((i + 1, ha[i], "same"), (j + 1, hb[j], "same")))
                folded = size - head - tail
                rows.append(_fold(folded))
                split.append(("fold", _fold(folded)[3]))
                for k in range(size - tail, size):
                    i, j = i1 + k, j1 + k
                    rows.append(("same", i + 1, j + 1, hb[j]))
                    split.append(((i + 1, ha[i], "same"), (j + 1, hb[j], "same")))
            else:
                for k in range(size):
                    i, j = i1 + k, j1 + k
                    rows.append(("same", i + 1, j + 1, hb[j]))
                    split.append(((i + 1, ha[i], "same"), (j + 1, hb[j], "same")))
            continue
        for i in range(i1, i2):
            rows.append(("del", i + 1, None, ha[i]))
        for j in range(j1, j2):
            rows.append(("add", None, j + 1, hb[j]))
        removed += i2 - i1
        added += j2 - j1
        for k in range(max(i2 - i1, j2 - j1)):
            left = (i1 + k + 1, ha[i1 + k], "del") if i1 + k < i2 else None
            right = (j1 + k + 1, hb[j1 + k], "add") if j1 + k < j2 else None
            split.append((left, right))
    return {"kind": "text", "rows": rows, "split": split, "added": added, "removed": removed}
