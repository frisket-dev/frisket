"""Project document-model Markdown into the OCR plain-text contract."""

from __future__ import annotations

from html.parser import HTMLParser

from markdown_it import MarkdownIt


class _PlainText(HTMLParser):
    """Read rendered HTML; code literals have already been escaped by MarkdownIt."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
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
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def markdown_to_plain_text(markdown: str) -> str:
    # A real parser preserves literal markup inside code, escaped characters,
    # nested links, and HTML tables instead of guessing with substitutions.
    rendered = MarkdownIt("commonmark", {"html": True}).enable("table").render(markdown)
    reader = _PlainText()
    reader.feed(rendered)
    reader.close()
    return "\n".join(
        line.rstrip() for line in "".join(reader.parts).splitlines()
    ).strip()
