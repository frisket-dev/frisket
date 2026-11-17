"""Source enumeration and comment/code tokenizing shared by the scripts/ci gates.

Python comments come from ``tokenize`` and docstrings from ``ast``. JavaScript
and TypeScript go through ``js_segments``, a lexer that separates comments,
string/template/regex literals and code without a full parser. Quoted strings
and regex literals cannot span lines, so an unterminated one (an apostrophe in
JSX text, say) ends at the newline instead of swallowing the rest of the file.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PY_ROOTS = ("src", "tests", "sidecar", "scripts")
JS_ROOTS = ("web/src", "web/tests", "sdk/src", "desktop")
PY_SUFFIXES = {".py"}
JS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {"node_modules", "dist", "build", "__pycache__", ".venv", "fixtures"}

# A `/` after one of these (or at the start) opens a regex literal, not a division.
_REGEX_AFTER_CHARS = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = {
    "return",
    "typeof",
    "case",
    "do",
    "else",
    "in",
    "of",
    "new",
    "delete",
    "void",
    "throw",
    "yield",
    "await",
    "instanceof",
}
_WORD_TAIL = re.compile(r"[A-Za-z_$][\w$]*$")


def tracked_files(roots: tuple[str, ...], suffixes: set[str]) -> list[str]:
    """Repo-relative paths git knows (tracked or unignored) under roots."""
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *roots,
        ],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    out = []
    for rel in sorted(set(listed.split("\0"))):
        path = Path(rel)
        if not rel or path.suffix not in suffixes:
            continue
        if SKIP_DIRS.intersection(path.parts[:-1]) or not (REPO / rel).is_file():
            continue
        out.append(rel)
    return out


def python_comments(text: str) -> list[tuple[int, str]]:
    """(line, text) for every ``#`` comment; empty when the file does not tokenize."""
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError):
        return []
    return out


def python_docstrings(tree: ast.Module) -> list[tuple[int, str]]:
    """(first line, text) for module, class and function docstrings."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append((value.lineno, value.value))
    return out


def _regex_allowed(code_before: str) -> bool:
    stripped = code_before.rstrip()
    if not stripped:
        return True
    if stripped[-1] in _REGEX_AFTER_CHARS:
        # `</tag>` closes JSX; the slash is not a regex.
        return not stripped.endswith("<")
    word = _WORD_TAIL.search(stripped)
    return bool(word and word.group() in _REGEX_AFTER_WORDS)


_JS_SPECIAL = re.compile(r"[/'\"`{}]")


def js_segments(text: str) -> Iterator[tuple[str, int, str]]:
    """Yield (kind, line, text) with kind in {"code", "comment", "string"}.

    Template-literal substitutions (``${...}``) are lexed as code, so comments
    and literals nested inside them are classified too.
    """
    i, n, line = 0, len(text), 1
    code_start = 0
    # Code preceding the current position, for the regex-or-division decision.
    # A literal ends an expression, so a slash right after one divides.
    tail = ""
    # Brace depth at which each open template substitution resumes its literal.
    templates: list[int] = []
    depth = 0

    def flush(upto: int) -> Iterator[tuple[str, int, str]]:
        nonlocal tail, line
        if upto > code_start:
            chunk = text[code_start:upto]
            yield ("code", line, chunk)
            line += chunk.count("\n")
            tail = (tail + chunk)[-40:]

    while True:
        match = _JS_SPECIAL.search(text, i)
        if match is None:
            break
        i = match.start()
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}" and not (templates and templates[-1] == depth):
            depth -= 1
            i += 1
            continue
        if (
            ch == "/"
            and nxt not in "/*"
            and not _regex_allowed(tail + text[code_start:i])
        ):
            i += 1
            continue
        yield from flush(i)
        start = i
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            i = n if end < 0 else end
            kind = "comment"
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            kind = "comment"
        elif ch in "'\"/":
            i += 1
            in_class = False
            while i < n and text[i] != "\n":
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if ch == "/" and c == "[":
                    in_class = True
                elif ch == "/" and c == "]":
                    in_class = False
                elif c == ch and not in_class:
                    i += 1
                    break
                i += 1
            if ch == "/":
                while i < n and (text[i].isalnum() or text[i] == "_"):
                    i += 1
            i = min(i, n)
            kind = "string"
        else:
            if ch == "}":
                templates.pop()
            i += 1
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "`":
                    i += 1
                    break
                if c == "$" and i + 1 < n and text[i + 1] == "{":
                    i += 2
                    templates.append(depth)
                    break
                i += 1
            i = min(i, n)
            kind = "string"
        chunk = text[start:i]
        yield (kind, line, chunk)
        line += chunk.count("\n")
        if kind == "string":
            tail = "x"
        code_start = i
    yield from flush(n)


def js_comments(text: str) -> list[tuple[int, str]]:
    return [(ln, s) for kind, ln, s in js_segments(text) if kind == "comment"]


def js_code_lines(text: str) -> list[tuple[int, str]]:
    """Code with literals replaced by ``""`` and comments by a space, per line."""
    pieces: list[str] = []
    for kind, _, chunk in js_segments(text):
        if kind == "code":
            pieces.append(chunk)
        elif kind == "string":
            pieces.append('""' + "\n" * chunk.count("\n"))
        else:
            pieces.append(" " + "\n" * chunk.count("\n"))
    return list(enumerate("".join(pieces).split("\n"), start=1))
