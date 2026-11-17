#!/usr/bin/env python3
"""Comments state constraints, not history: a shrink-only per-file ratchet.

Scans Python comments and docstrings (``tokenize``/``ast``) and JavaScript/
TypeScript comments (``source_scan.js_segments``), never string literals or
identifiers, for PATTERNS: dates, ticket or lane slugs, review and agent
provenance, and markers of unfinished work. Each file's hit count is compared
with BASELINE; a count above it, or a hit in a file absent from it, fails.
``--update`` lowers the baseline and refuses to raise any count or add a file.

Fix a failure by rewriting the comment as the constraint it protects ("the
column must stay nullable because imported bundles omit it"), or by doing the
unfinished work. Where a change came from belongs in the commit message.
A renamed file keeps its count: rename its key in the baseline by hand.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys

import file_count_ratchet
from source_scan import (
    JS_ROOTS,
    JS_SUFFIXES,
    PY_ROOTS,
    PY_SUFFIXES,
    REPO,
    js_comments,
    python_comments,
    python_docstrings,
    tracked_files,
)

BASELINE = REPO / "scripts/ci/comment_history_baseline.json"
SELF = "scripts/ci/lint_comment_history.py"

# Standards, encodings, licences and model families spelled like ticket slugs.
STANDARD_PREFIXES = (
    "AES",
    "BCP",
    "BERT",
    "BY",
    "CLIP",
    "CVE",
    "EPSG",
    "GPT",
    "HTTP",
    "ISO",
    "MPEG",
    "ORCHID",
    "PDF",
    "RFC",
    "SHA",
    "UTF",
    "VL",
)
CODE_NOUNS = (
    r"paths?|code|helpers?|shims?|wrappers?|alias(?:es)?|apis?|endpoints?|routes?"
    r"|functions?|class(?:es)?|modules?|implementations?|registry|reducer|handlers?"
    r"|runner|maprunner|forms?|actions?|bindings?|registration|adapters?"
    r"|branch(?:es)?|flows?|surfaces?|pipelines?|executors?|services?|clients?"
    r"|callers?|consumers?|components?|ui|views?|tests?|entry points?"
)
PATTERNS = {
    "date": re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b"),
    "slug": re.compile(
        rf"\b(?!(?:{'|'.join(STANDARD_PREFIXES)})-)[A-Z]{{2,}}-\d+[a-z]?\b"
    ),
    "provenance": re.compile(
        r"(?i)\b(review round|codex|per (?:the )?review(?:'s)?|cut ?over later)\b"
    ),
    "unfinished": re.compile(r"\b(TODO|FIXME|XXX|HACK)\b|(?i:\bfor now\b)"),
    "legacy-code": re.compile(
        rf"(?i)\blegacy[\s-]+(?:`?\w+`?[\s-]+)?(?:{CODE_NOUNS})\b"
    ),
}

Hit = tuple[int, str, str]


def comment_hits(comments: list[tuple[int, str]]) -> list[Hit]:
    """(line, pattern name, text) for every pattern match in the comments."""
    hits: list[Hit] = []
    for start, text in comments:
        for offset, line in enumerate(text.split("\n")):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append((start + offset, name, line.strip()))
    return hits


def file_hits(rel: str) -> list[Hit]:
    try:
        text = (REPO / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if not any(pattern.search(text) for pattern in PATTERNS.values()):
        return []
    if rel.endswith(".py"):
        comments = python_comments(text)
        try:
            comments += python_docstrings(ast.parse(text))
        except (SyntaxError, ValueError):
            pass
    else:
        comments = js_comments(text)
    return comment_hits(comments)


def measure() -> dict[str, list[Hit]]:
    files = tracked_files(PY_ROOTS, PY_SUFFIXES) + tracked_files(JS_ROOTS, JS_SUFFIXES)
    found = {rel: file_hits(rel) for rel in files if rel != SELF}
    return {rel: hits for rel, hits in found.items() if hits}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    args = parser.parse_args(argv)
    hits = measure()
    return file_count_ratchet.enforce(
        "lint_comment_history",
        file_count_ratchet.read(BASELINE),
        {rel: len(found) for rel, found in hits.items()},
        unit="history comment(s)",
        fix=(
            "Comments state the constraint the code protects, never dates, ticket "
            "slugs, review or agent provenance, or unfinished-work markers; put "
            "history in the commit message and finish or delete the unfinished path."
        ),
        update=(lambda counts: file_count_ratchet.write(BASELINE, counts))
        if args.update
        else None,
        detail=lambda rel: [
            f"{rel}:{line}: [{name}] {text[:120]}" for line, name, text in hits[rel]
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
