"""A tiny, dependency-free Markdown-to-HTML renderer for the action-flight
report. ``.artifacts/action-flight/index.html`` is a static, self-contained
file with no CDN scripts.

Not a general Markdown implementation -- just enough to render what Haiku
actually produces in columns 2 (panel prediction) and 3 (dissonance judgment):
paragraphs, bullet
lists (``-``/``*``), numbered lists, **bold**, *italic*, `inline code`, and
blank-line-separated blocks. Headings are supported too since Haiku
occasionally opens with one. All output is HTML-escaped first, then a small
set of regexes turn Markdown syntax into tags -- inputs are LLM prose, not
adversarial HTML, but escaping first means a stray ``<`` or ``&`` in a
prediction can never break the page.
"""

from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_BULLET_LINE = re.compile(r"^\s*[-*]\s+(.*)$")
_NUM_LINE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$")


def _inline(text: str) -> str:
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return text


def render_markdown(text: str | None) -> str:
    """Render a Markdown string to a small, safe HTML fragment."""
    if not text or not text.strip():
        return ""

    escaped = html.escape(text, quote=True)
    # Markdown markers survive html.escape because they contain none of &<>"'.
    lines = escaped.splitlines()

    out: list[str] = []
    para: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            out.append(f"<{list_tag}>")
            for item in list_items:
                out.append(f"<li>{_inline(item)}</li>")
            out.append(f"</{list_tag}>")
            list_items.clear()
        list_tag = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue

        heading = _HEADING_LINE.match(line)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        bullet = _BULLET_LINE.match(line)
        if bullet:
            flush_para()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(bullet.group(1))
            continue

        numbered = _NUM_LINE.match(line)
        if numbered:
            flush_para()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(numbered.group(1))
            continue

        flush_list()
        para.append(line.strip())

    flush_para()
    flush_list()

    return "\n".join(out) if out else f"<p>{_inline(escaped)}</p>"
