"""Project document-model Markdown into the OCR plain-text contract."""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from markdown_it import MarkdownIt


_HTML_TAGS = {
    "a",
    "article",
    "aside",
    "b",
    "blockquote",
    "br",
    "code",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "i",
    "img",
    "li",
    "main",
    "mark",
    "nav",
    "ol",
    "p",
    "pre",
    "s",
    "script",
    "section",
    "small",
    "span",
    "strong",
    "style",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

_START_TAG_NAME = re.compile(r"<\s*/?\s*([^\s/>]+)")
_VOID_TAGS = {"br", "hr", "img"}


def _preserve_inline_angle_text(children):
    """Render unknown and unmatched case-variant HTML tokens as literal text."""
    open_tags: dict[str, list[int]] = {}
    paired: set[int] = set()
    parsed: dict[int, tuple[str, str]] = {}

    for index, token in enumerate(children):
        if token.type != "html_inline":
            continue
        raw = token.content
        match = _START_TAG_NAME.match(raw)
        if match is None:
            continue
        raw_tag = match.group(1)
        tag = raw_tag.lower()
        parsed[index] = (raw_tag, tag)
        if tag not in _HTML_TAGS or tag in _VOID_TAGS or raw.rstrip().endswith("/>"):
            continue
        if raw.startswith("</"):
            openings = open_tags.get(tag)
            if openings:
                paired.update((openings.pop(), index))
        else:
            open_tags.setdefault(tag, []).append(index)

    for index, (raw_tag, tag) in parsed.items():
        if tag not in _HTML_TAGS or (
            raw_tag != tag and tag not in _VOID_TAGS and index not in paired
        ):
            children[index].type = "text"


def _preserve_block_angle_text(token):
    """Keep a suspicious first-line angle token that CommonMark made HTML."""
    first_line, *remaining_lines = token.content.splitlines(keepends=True)
    raw = first_line.strip()
    match = _START_TAG_NAME.match(raw)
    if match is None or not raw.endswith(">") or raw.find(">") != len(raw) - 1:
        return
    raw_tag = match.group(1)
    tag = raw_tag.lower()
    if tag not in _HTML_TAGS or (raw_tag != tag and tag not in _VOID_TAGS):
        # Leave the token boundary to MarkdownIt; escaping only prevents the
        # downstream HTML projector from mistaking literal legal prose for a
        # structural tag. Preserve any following block lines verbatim; for a
        # one-line block, two breaks retain the source block boundary.
        token.content = (
            escape(first_line) + "".join(remaining_lines)
            if remaining_lines
            else escape(raw) + "\n\n"
        )


class _PlainText(HTMLParser):
    """Read rendered HTML; code literals have already been escaped by MarkdownIt."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.lists: list[list[str | int]] = []
        self.table_depth = 0

    def _break(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag not in _HTML_TAGS:
            if not self.hidden:
                self.parts.append(self.get_starttag_text())
            return
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "ol":
            values = dict(attrs)
            try:
                start = int(values.get("start", "1"))
            except (TypeError, ValueError):
                start = 1
            self.lists.append(["ol", start - 1])
        elif tag == "ul":
            self.lists.append(["ul", 0])
        elif tag == "table":
            self.table_depth += 1
        elif tag == "li" and self.lists and self.lists[-1][0] == "ol":
            self._break()
            self.lists[-1][1] = int(self.lists[-1][1]) + 1
            self.parts.append(f"{self.lists[-1][1]}. ")
        elif tag == "br":
            self._break()
        elif tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag not in _HTML_TAGS:
            if not self.hidden:
                self.parts.append(f"</{tag}>")
            return
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in {"ol", "ul"}:
            if self.lists:
                self.lists.pop()
            self._break()
            return
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            return
        if tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in {
            "p",
            "div",
            "li",
            "tr",
            "blockquote",
            "pre",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self._break()

    def handle_startendtag(self, tag, attrs):
        if tag not in _HTML_TAGS:
            if not self.hidden:
                self.parts.append(self.get_starttag_text())
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.hidden:
            return
        if not data.strip() and "\n" in data:
            if self.lists:
                self._break()
                return
            if self.table_depth:
                return
        self.parts.append(data)


def markdown_to_plain_text(markdown: str) -> str:
    # A real parser preserves literal markup inside code, escaped characters,
    # nested links, and HTML tables instead of guessing with substitutions.
    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    env = {}
    tokens = parser.parse(markdown, env)
    for token in tokens:
        if token.children:
            _preserve_inline_angle_text(token.children)
        elif token.type == "html_block":
            _preserve_block_angle_text(token)
    rendered = parser.renderer.render(tokens, parser.options, env)
    reader = _PlainText()
    reader.feed(rendered)
    reader.close()
    return "\n".join(
        line.rstrip() for line in "".join(reader.parts).splitlines()
    ).strip()
