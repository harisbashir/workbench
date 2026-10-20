"""Syntax highlighting for the code viewer and diffs, as one HTML string per line.

Lines are built from Pygments tokens (not by splitting Pygments' HTML), so a comment
or string that spans lines still gives well-formed HTML on every line.
"""
from django.utils.html import escape
from django.utils.safestring import mark_safe
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.token import STANDARD_TYPES
from pygments.util import ClassNotFound

HIGHLIGHT_BYTES = 1024 * 1024
HIGHLIGHT_LINES = 20_000
PLAIN = {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym", ".kicad_mod", ".kicad_dru", ".net"}


def lexer_for(path):
    if any(path.lower().endswith(ext) for ext in PLAIN):
        return TextLexer(stripnl=False, ensurenl=False)
    try:
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return TextLexer(stripnl=False, ensurenl=False)


def _css_class(ttype):
    while ttype is not None:
        cls = STANDARD_TYPES.get(ttype)
        if cls is not None:
            return cls
        ttype = ttype.parent
    return ""


def render(path, text):
    """(list of safe HTML strings, one per line; language name)."""
    if len(text) > HIGHLIGHT_BYTES or text.count("\n") > HIGHLIGHT_LINES:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[:HIGHLIGHT_LINES + 1]
        if lines and lines[-1] == "":
            lines.pop()
        return [mark_safe(escape(line)) for line in lines], "Plain text"
    lexer = lexer_for(path)
    lines, current = [], []
    for ttype, value in lexer.get_tokens(text):
        cls = _css_class(ttype)
        pieces = value.split("\n")
        for i, piece in enumerate(pieces):
            if i:
                lines.append("".join(current))
                current = []
            if piece:
                current.append(f'<span class="{cls}">{escape(piece)}</span>' if cls else escape(piece))
    lines.append("".join(current))
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return [mark_safe(line) for line in lines], lexer.name
