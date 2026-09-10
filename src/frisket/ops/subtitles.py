"""Best-effort plain-text extraction from downloaded caption sidecar files.

``subtitle_text`` turns a VTT or SRT caption file into a readable transcript:
headers, cue identifiers, and ``-->`` timestamp lines are dropped; inline
timing/markup tags (``<00:00:01.500>``, ``<c>``, ``</c>``, ``<c.colorname>``,
``<b>``, ``<i>``, ...) are stripped; basic HTML entities are decoded. Other
collected caption formats (ass/ttml/srv1/srv3/json3) are not understood and
return ``None``, as does a file with no extractable text.

YouTube AUTO captions render as a rolling window: each cue repeats the tail
of the previous cue's text before appending new words. Dedup is
CONSECUTIVE-ONLY (a line is dropped iff it equals the immediately-preceding
*emitted* line, after whitespace normalization) so this rolling repetition
collapses away while dialogue that legitimately recurs later in the
transcript is preserved. Surviving lines are joined with ``\\n``; lines that
clean to empty (e.g. a cue that was pure markup) are dropped, which is this
module's way of collapsing blank-line runs down to nothing.
"""

from __future__ import annotations

import html
import re

_SUPPORTED_EXTS = frozenset({"vtt", "srt"})
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def subtitle_text(data: bytes, filename: str) -> str | None:
    """Extract a deduped plain-text transcript from a caption sidecar file.

    Returns ``None`` when the format is not VTT/SRT, or when parsing yields
    no text (so callers can leave the destination cell empty rather than
    fail the row).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _SUPPORTED_EXTS:
        return None
    text = data.decode("utf-8", errors="replace")
    raw_lines = _vtt_cue_lines(text) if ext == "vtt" else _srt_cue_lines(text)
    cleaned = [_clean_line(line) for line in raw_lines]
    cleaned = [line for line in cleaned if line]
    deduped = _dedupe_consecutive(cleaned)
    if not deduped:
        return None
    return "\n".join(deduped)


def _blocks(text: str) -> list[list[str]]:
    """Split caption source text into blank-line-delimited blocks."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(raw_line)
    if current:
        blocks.append(current)
    return blocks


def _cue_lines_from_block(block: list[str]) -> list[str]:
    """Drop a cue block's identifier/index line and its timestamp line,
    keeping only the text lines that follow the ``-->`` line."""
    lines: list[str] = []
    seen_timestamp = False
    for raw_line in block:
        if "-->" in raw_line:
            seen_timestamp = True
            continue
        if not seen_timestamp:
            continue
        lines.append(raw_line)
    return lines


def _vtt_cue_lines(text: str) -> list[str]:
    blocks = _blocks(text)
    out: list[str] = []
    for index, block in enumerate(blocks):
        head = block[0].strip().upper()
        if index == 0 and head.startswith("WEBVTT"):
            continue
        if head.startswith("NOTE") or head.startswith("STYLE"):
            continue
        out.extend(_cue_lines_from_block(block))
    return out


def _srt_cue_lines(text: str) -> list[str]:
    out: list[str] = []
    for block in _blocks(text):
        out.extend(_cue_lines_from_block(block))
    return out


def _clean_line(raw_line: str) -> str:
    without_tags = _TAG_RE.sub("", raw_line)
    unescaped = html.unescape(without_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def _dedupe_consecutive(lines: list[str]) -> list[str]:
    out: list[str] = []
    previous: str | None = None
    for line in lines:
        if line == previous:
            continue
        out.append(line)
        previous = line
    return out
