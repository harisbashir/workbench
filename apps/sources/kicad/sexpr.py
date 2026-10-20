"""A small, safe reader for KiCad's s-expression files (.kicad_sch, .kicad_pcb).

The files are untrusted, so the reader is iterative (no recursion limit to hit), and it stops
at a size, depth and node limit with a message people can read.
"""
import math

MAX_BYTES = 50 * 1024 * 1024
MAX_DEPTH = 200
MAX_NODES = 5_000_000


class KicadError(ValueError):
    """A problem with a KiCad file; the message is shown to people."""


class Str(str):
    """A quoted string in the file (as opposed to a bare word like `yes` or a number)."""


def parse(text):
    if len(text) > MAX_BYTES:
        raise KicadError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB, so it isn't drawn.")
    stack, root, nodes = [], None, 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            if root is not None and not stack:
                raise KicadError("The file has extra content after the end.")
            if len(stack) >= MAX_DEPTH:
                raise KicadError("The file is nested too deeply to read.")
            node = []
            if stack:
                stack[-1].append(node)
            else:
                root = node
            stack.append(node)
            i += 1
        elif c == ")":
            if not stack:
                raise KicadError("The file has an unexpected “)”, so it can't be read.")
            stack.pop()
            i += 1
        elif c == '"':
            j, out = i + 1, []
            while True:
                k = text.find('"', j)
                b = text.find("\\", j)
                if k == -1:
                    raise KicadError("The file has a text value that never ends, so it can't be read.")
                if b != -1 and b < k:
                    out.append(text[j:b])
                    if b + 1 < n:
                        nxt = text[b + 1]
                        out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                    j = b + 2
                    continue
                out.append(text[j:k])
                i = k + 1
                break
            if not stack:
                raise KicadError("This doesn't look like a KiCad file.")
            stack[-1].append(Str("".join(out)))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            if not stack:
                raise KicadError("This doesn't look like a KiCad file.")
            stack[-1].append(text[i:j])
            i = j
        nodes += 1
        if nodes > MAX_NODES:
            raise KicadError("The file is too large to draw.")
    if stack or root is None:
        raise KicadError("The file ends unexpectedly, so it can't be read.")
    return root


def num(atom, default=0.0):
    try:
        v = float(atom)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v) or abs(v) > 1e6:
        return default
    return v


def head(node):
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def children(node):
    return [c for c in node[1:] if isinstance(c, list)] if isinstance(node, list) else []


def find(node, name):
    for c in children(node):
        if c and c[0] == name:
            return c
    return None


def find_all(node, name):
    return [c for c in children(node) if c and c[0] == name]


def value(node, name, default=None):
    c = find(node, name)
    return c[1] if c is not None and len(c) > 1 and not isinstance(c[1], list) else default


def xy(node, name="at"):
    """(x, y, angle) of a child like (at 10 5 90); zeros when it's missing."""
    c = find(node, name)
    if c is None:
        return 0.0, 0.0, 0.0
    vals = [num(v) for v in c[1:4] if not isinstance(v, list)]
    vals += [0.0] * (3 - len(vals))
    return vals[0], vals[1], vals[2]


def points(node):
    """[(x, y)] from (pts (xy 1 2) (xy 3 4)…)."""
    pts = find(node, "pts")
    return [(num(p[1]), num(p[2])) for p in find_all(pts, "xy") if len(p) >= 3] if pts is not None else []


def has_flag(node, flag):
    """True for a bare `hide` (KiCad 6–8) or (hide yes) (KiCad 9)."""
    if not isinstance(node, list):
        return False
    for c in node[1:]:
        if c == flag and not isinstance(c, Str):
            return True
        if isinstance(c, list) and c and c[0] == flag:
            return len(c) == 1 or c[1] in ("yes", "true")
    return False


def file_version(tree):
    v = value(tree, "version", "")
    return int(v) if isinstance(v, str) and v.isdigit() and len(v) <= 10 else 0
