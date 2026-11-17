#!/usr/bin/env python3
"""Glossary avoid-terms: a shrink-only per-file ratchet on words GLOSSARY.md rejects.

The term-to-avoid mapping is read from GLOSSARY.md at run time: each entry's
``_Avoid_:`` line lists comma- or semicolon-separated items. A bare word or
phrase is a plain avoid and is checked; an item with a qualifier (a
parenthesis, or text after a quoted word) is guidance for people and is
checked only where SCOPES_FILE names it. SCOPES_FILE also lists plain avoids
that are skipped in one scope because the word is ordinary English there.

Two scopes are counted per file and per avoided term, and compared with
BASELINE (a count above it, or a hit in a file absent from it, fails):

- ``text``: words a person reads. Web: JSX text and the ``title``, ``label``,
  ``aria-label``, ``placeholder`` and ``alt`` attributes, plus message-like
  object properties, in ``web/src`` outside generated code, tests and stories.
  Python: string arguments of ``ActionError``/``RouteError`` and ``help=``
  strings, in ``src/frisket``. Whole words, case-insensitive, plural forms.
- ``names``: components of declared names (snake_case and camelCase segments)
  and of file names in ``src/frisket`` and ``web/src``. It stops growth;
  existing names are renamed separately.

``--update`` lowers the baseline and refuses to raise any count or add a file.
``--explain TERM`` prints the glossary entry that avoids TERM. Fix a failure by
using the term the glossary prefers. A renamed file keeps its counts: rename
its keys in the baseline by hand.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import file_count_ratchet
from source_scan import REPO, js_segments, tracked_files

GLOSSARY = REPO / "GLOSSARY.md"
BASELINE = REPO / "scripts/ci/glossary_avoid_baseline.json"
SCOPES_FILE = REPO / "scripts/ci/glossary_avoid_scopes.json"
PY_ROOT = ("src/frisket",)
WEB_ROOT = ("web/src",)
TS_SUFFIXES = {".ts", ".tsx"}
NOT_SHIPPED = re.compile(
    r"(^|/)(generated|__tests__|__mocks__|__pycache__)/|\.(test|spec|stories)\.[jt]sx?$"
)
ENTRY = re.compile(r"^\*\*(?P<term>[^*]+)\*\*\s+—")
BARE_ITEM = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)*")
QUALIFIED_TERM = re.compile(r'^"(?P<quoted>[^"]+)"|^(?P<bare>[a-z]+(?: [a-z]+)*)\s*\(')


@dataclass(frozen=True)
class Avoided:
    """One avoided word and the glossary term that replaces it."""

    word: str
    preferred: str
    plain: bool


def split_items(line: str) -> list[str]:
    """Items of an Avoid line, splitting on commas and semicolons outside parentheses."""
    items, depth, current = [], 0, ""
    for ch in line.rstrip().rstrip("."):
        depth += ch == "("
        depth -= ch == ")"
        if ch in ",;" and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    items.append(current.strip())
    return [item for item in items if item]


def parse_glossary(text: str) -> list[Avoided]:
    """Every avoided word in the glossary, with its entry's term."""
    found: list[Avoided] = []
    preferred = ""
    for line in text.splitlines():
        entry = ENTRY.match(line)
        if entry:
            preferred = entry["term"]
        elif line.startswith("_Avoid_:") and preferred:
            for item in split_items(line.removeprefix("_Avoid_:")):
                if BARE_ITEM.fullmatch(item):
                    found.append(Avoided(item.lower(), preferred, True))
                    continue
                named = QUALIFIED_TERM.match(item)
                if named:
                    word = named["quoted"] or named["bare"]
                    found.append(Avoided(word, preferred, False))
    return found


def load_scopes() -> dict[str, dict[str, str]]:
    """{"skip_text": {word: reason}, "skip_names": {...}, "qualified_text": {...}}."""
    return json.loads(SCOPES_FILE.read_text(encoding="utf-8"))


def checked_words(avoided: list[Avoided], scope: str, scopes: dict) -> dict[str, str]:
    """{word: preferred term} that the gate counts in a scope ("text" or "names")."""
    skip = scopes[f"skip_{scope}"]
    qualified = scopes.get(f"qualified_{scope}", {})
    out: dict[str, str] = {}
    for item in avoided:
        if item.word in skip or not (item.plain or item.word in qualified):
            continue
        out.setdefault(item.word, item.preferred)
    return out


def word_pattern(word: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in word.split())
    return re.compile(rf"(?<![\w.-]){body}(?:e?s)?(?![A-Za-z])", re.IGNORECASE)


_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")


def prose(strings: list[str]) -> list[str]:
    """Strings that read as prose: not a bare token, template substitutions removed."""
    cleaned = [_SUBSTITUTION.sub(" ", s) for s in strings]
    return [s for s in cleaned if re.search(r"\s", s.strip())]


def count_words(strings: list[str], words: dict[str, str]) -> dict[str, int]:
    strings = prose(strings)
    counts: dict[str, int] = {}
    for word in words:
        pattern = word_pattern(word)
        n = sum(len(pattern.findall(s)) for s in strings)
        if n:
            counts[word] = n
    return counts


_COMPONENT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def components(name: str) -> list[str]:
    return [part.lower() for part in _COMPONENT.findall(name)]


def count_names(names: list[str], words: dict[str, str]) -> dict[str, int]:
    """How many names contain each avoided word as consecutive components."""
    counts: dict[str, int] = {}
    for word in words:
        want = word.split()
        tails = {want[-1], want[-1] + "s", want[-1] + "es"}
        for name in names:
            parts = components(name)
            for i in range(len(parts) - len(want) + 1):
                if parts[i : i + len(want) - 1] == want[:-1] and (
                    parts[i + len(want) - 1] in tails
                ):
                    counts[word] = counts.get(word, 0) + 1
                    break
    return counts


# ---- user-facing text -----------------------------------------------------

_TEXT_ATTRS = r"title|label|aria-label|placeholder|alt"
_TEXT_PROPS = (
    r"title|label|message|description|placeholder|tooltip|heading|hint|helperText"
    r"|emptyMessage|errorMessage|confirmLabel|cancelLabel|ariaLabel"
)
JSX_ATTR = re.compile(
    rf"""\b(?:{_TEXT_ATTRS})\s*=\s*\{{?\s*(["'`])((?:\\.|(?!\1).)*)\1"""
)
TEXT_PROP = re.compile(rf"""\b(?:{_TEXT_PROPS})\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""")
JSX_TEXT = re.compile(
    r"(?:(?<![\w\]$])<[A-Za-z][\w.]*(?:[^<>]|=>)*(?<![/=])>|</[\w.]*>|/>)"
    r"\s*([^<>{}();=\n][^<>{}();=]*?)\s*(?=<|\{)",
    re.DOTALL,
)


def blank_comments(text: str) -> str:
    """The source with comments replaced by spaces, keeping line structure."""
    pieces = []
    for kind, _, chunk in js_segments(text):
        pieces.append(re.sub(r"[^\n]", " ", chunk) if kind == "comment" else chunk)
    return "".join(pieces)


def web_text(source: str) -> list[str]:
    """Strings a person reads in a TS/TSX file."""
    code = blank_comments(source)
    found = [m[2] for m in JSX_ATTR.finditer(code)]
    found += [m[2] for m in TEXT_PROP.finditer(code)]
    found += [m[1] for m in JSX_TEXT.finditer(code) if re.search(r"[A-Za-z]{2}", m[1])]
    return found


_USER_ERRORS = {"ActionError", "RouteError"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _constant_strings(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def python_text(source: str) -> list[str]:
    """Strings a person reads: user-error arguments and ``help=`` strings."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) in _USER_ERRORS:
            found += [s for arg in node.args for s in _constant_strings(arg)]
            found += [s for kw in node.keywords for s in _constant_strings(kw.value)]
        else:
            found += [
                s
                for kw in node.keywords
                if kw.arg == "help"
                for s in _constant_strings(kw.value)
            ]
    return found


# ---- declared names ---------------------------------------------------------

_TS_DECL = re.compile(
    r"\b(?:function\*?|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)"
)
_TS_PROP = re.compile(
    r"^\s*(?:readonly\s+)?([A-Za-z_$][\w$]*)\??\s*:(?!:)", re.MULTILINE
)
_TS_PARAMS = re.compile(r"(?:function\*?\s*[\w$]*|\bconst\s+[\w$]+\s*=)\s*\(([^()]*)\)")
_TS_ARROW = re.compile(r"\(([^()]*)\)\s*(?::[^=;{]*)?=>")
_TS_PARAM_NAME = re.compile(r"^\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)")
_TS_NOT_NAMES = {"case", "default", "type", "return"}


def ts_names(source: str) -> list[str]:
    code = "".join(
        '""' if kind == "string" else (" " if kind == "comment" else chunk)
        for kind, _, chunk in js_segments(source)
    )
    names = _TS_DECL.findall(code)
    names += [n for n in _TS_PROP.findall(code) if n not in _TS_NOT_NAMES]
    for pattern in (_TS_PARAMS, _TS_ARROW):
        for params in pattern.findall(code):
            for part in params.split(","):
                match = _TS_PARAM_NAME.match(part)
                if match:
                    names.append(match[1])
    return names


def python_names(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.append(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.append(node.attr)
    return names


# ---- measurement ------------------------------------------------------------


def shipped(roots: tuple[str, ...], suffixes: set[str]) -> list[str]:
    return [r for r in tracked_files(roots, suffixes) if not NOT_SHIPPED.search(r)]


def read(rel: str) -> str:
    try:
        return (REPO / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def measure(words: dict[str, dict[str, str]]) -> dict[str, int]:
    """Counts keyed ``"<file> [<scope>] <word>"``."""
    counts: dict[str, int] = {}

    def add(rel: str, scope: str, found: dict[str, int]) -> None:
        for word, n in found.items():
            counts[f"{rel} [{scope}] {word}"] = n

    for rel in shipped(PY_ROOT, {".py"}):
        source = read(rel)
        add(rel, "text", count_words(python_text(source), words["text"]))
        names = python_names(source) + [Path(rel).stem]
        add(rel, "names", count_names(names, words["names"]))
    for rel in shipped(WEB_ROOT, TS_SUFFIXES):
        source = read(rel)
        add(rel, "text", count_words(web_text(source), words["text"]))
        names = ts_names(source) + [Path(rel).stem]
        add(rel, "names", count_names(names, words["names"]))
    return counts


def load_words() -> dict[str, dict[str, str]]:
    avoided = parse_glossary(GLOSSARY.read_text(encoding="utf-8"))
    scopes = load_scopes()
    return {scope: checked_words(avoided, scope, scopes) for scope in ("text", "names")}


def explain(term: str, avoided: list[Avoided]) -> list[str]:
    hits = [a for a in avoided if a.word == term.lower()]
    return [
        f"'{a.word}': the glossary says {a.preferred}; see GLOSSARY.md "
        f"({'plain avoid' if a.plain else 'qualified avoid, guidance only unless listed in scopes file'})"
        for a in hits
    ] or [f"'{term}' is not an avoided term in GLOSSARY.md"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    parser.add_argument("--explain", metavar="TERM", help="show the glossary entry")
    parser.add_argument("--report", action="store_true", help="print totals per term")
    args = parser.parse_args(argv)
    avoided = parse_glossary(GLOSSARY.read_text(encoding="utf-8"))
    if args.explain:
        print("\n".join(explain(args.explain, avoided)))
        return 0
    words = load_words()
    current = measure(words)
    if args.report:
        totals: dict[str, int] = {}
        for key, n in current.items():
            tail = key.split(" [", 1)[1].replace("] ", " ")
            totals[tail] = totals.get(tail, 0) + n
        for tail, n in sorted(totals.items()):
            print(f"{n:6d}  {tail}")
        return 0
    preferred = {w: p for scope in words.values() for w, p in scope.items()}
    return file_count_ratchet.enforce(
        "lint_glossary_avoid",
        file_count_ratchet.read(BASELINE),
        current,
        unit="avoided term use(s)",
        fix=(
            "Use the term GLOSSARY.md prefers in user-facing text and in new "
            "names (`--explain <term>` shows the entry); rename existing uses "
            "rather than adding more."
        ),
        update=(lambda counts: file_count_ratchet.write(BASELINE, counts))
        if args.update
        else None,
        detail=lambda key: [
            f"'{key.rsplit(' ', 1)[1]}': the glossary says "
            f"{preferred.get(key.rsplit(' ', 1)[1], '?')}; see GLOSSARY.md"
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
