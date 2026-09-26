"""Turns plain chat text into safe HTML.

Text is HTML-escaped first; only then are links, task keys, @mentions,
`code` and **bold** turned into markup, so user input can never inject HTML.
"""
import re

from django.utils.html import escape
from django.utils.safestring import mark_safe

URL_RE = re.compile(r"(https?://[^\s<]+[^\s<.,;:!?)\]'\"])")
TASK_RE = re.compile(r"(?<![\w/-])([A-Z][A-Z0-9]{1,9})-(\d+)\b")
MENTION_RE = re.compile(r"(?<![\w])@([\w.+-]+\w)")
CODE_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
FENCE_RE = re.compile(r"```\n?(.*?)```", re.S)


def render(text, known_keys=None):
    blocks = []

    def stash_fence(m):
        blocks.append(f"<pre><code>{m.group(1)}</code></pre>")
        return f"\x00{len(blocks) - 1}\x00"

    html = escape(text)
    html = FENCE_RE.sub(stash_fence, html)

    codes = []

    def stash_code(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x01{len(codes) - 1}\x01"

    html = CODE_RE.sub(stash_code, html)
    html = URL_RE.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', html)

    def task_link(m):
        key, num = m.group(1), m.group(2)
        if known_keys is not None and key not in known_keys:
            return m.group(0)
        return f'<a class="task-ref" href="/projects/{key}/tasks/{num}/">{key}-{num}</a>'

    # Avoid touching text inside the <a> tags we just made.
    parts = re.split(r"(<a [^>]*>.*?</a>)", html)
    for i, part in enumerate(parts):
        if not part.startswith("<a "):
            part = TASK_RE.sub(task_link, part)
            part = MENTION_RE.sub(r'<span class="mention">@\1</span>', part)
            part = BOLD_RE.sub(r"<strong>\1</strong>", part)
            parts[i] = part
    html = "".join(parts)
    html = re.sub("\x01(\\d+)\x01", lambda m: codes[int(m.group(1))], html)
    # Markdown-style headings ("## Changes") in notes and descriptions.
    html = re.sub(r"(?m)^#{1,3} +(.+)$", r'<strong class="md-h">\1</strong>', html)
    html = html.replace("\n", "<br>")
    html = re.sub("\x00(\\d+)\x00", lambda m: blocks[int(m.group(1))], html)
    return mark_safe(html)
