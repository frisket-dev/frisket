"""Token lints over test sources, driven by one rule table.

Each rule scans every ``*.py`` under tests/ and sidecar/tests/ for offending
lines. A rule's line marker on the offending line or the line above suppresses
that line; its file marker anywhere in the file suppresses the whole file.
Heuristic and false-negative-tolerant by design: a token inside a string
literal counts, which is what the markers are for.

Allowlists live in lint_test_tokens_allowlist.json, keyed by rule then file,
and only shrink. A counted rule maps a file to the number of offending lines
it may keep: more fails, fewer fails until the entry is ratcheted down. A
reasoned rule maps a file to the reason all its lines are exempt. Either way,
an entry for a clean or missing file fails until it is deleted.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIRS = ("tests", "sidecar/tests")
ALLOWLIST = Path(__file__).with_name("lint_test_tokens_allowlist.json")

Candidates = Callable[[list[str]], Iterator[int]]


def token_lines(token: re.Pattern[str]) -> Candidates:
    def scan(lines: list[str]) -> Iterator[int]:
        return (idx for idx, line in enumerate(lines) if token.search(line))

    return scan


# Source-tree path literals and build manifests.
SOURCE_PATH = re.compile(
    r"""["'](?:\.{0,2}/)*(?:src|web|scripts|\.github)["'/]"""
    r"""|["'][^"']*(?:Dockerfile|docker-compose)"""
)
# Expressions that name a module's file. ``getsourcefile``/``getfile`` answer
# the same question as ``__file__``.
MODULE_FILE = re.compile(
    r"\b[A-Za-z_]\w*\s*\.\s*__file__\b|\binspect\.(?:getsourcefile|getfile)\("
)
# Retrieving source text through the import system is a read in itself.
SOURCE_TEXT = re.compile(r"(?:\binspect\.|(?<![\w.]))getsource(?:lines)?\(")
READ_CALL = re.compile(r"\.read_text\(|\.read_bytes\(|(?<![\w.])open\(")
# ``ast.parse`` counts only when its line carries a source origin or a tainted
# name, so parsing a literal or an untainted parameter does not.
AST_PARSE = re.compile(r"\bast\.parse\(")
RECEIVER = re.compile(
    r"([A-Za-z_]\w*)\s*\.\s*read_(?:text|bytes)\(|open\(\s*([A-Za-z_]\w*)"
)
ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
FOR_TARGET = re.compile(r"^\s*(?:async\s+)?for\s+([A-Za-z_]\w*)\s+in\s+(.*)")
NAME = re.compile(r"[A-Za-z_]\w*")


def source_origin(text: str) -> bool:
    return bool(
        SOURCE_PATH.search(text) or MODULE_FILE.search(text) or SOURCE_TEXT.search(text)
    )


def source_read_lines(lines: list[str]) -> Iterator[int]:
    """Reads, ``getsource`` calls and ``ast.parse`` calls reaching repo source.

    A name is tainted once assigned from, or looped over, an expression with a
    source origin or an already tainted name. Taint is file-wide by name.
    """
    tainted: set[str] = set()
    for idx, line in enumerate(lines):
        sourcey = source_origin(line)
        assign = ASSIGN.match(line)
        if assign:
            # Join balanced continuations, with a bounded scan.
            rhs = line.split("=", 1)[1]
            depth = rhs.count("(") + rhs.count("[") - rhs.count(")") - rhs.count("]")
            for cont in lines[idx + 1 : idx + 8]:
                if depth <= 0:
                    break
                rhs += " " + cont
                depth += cont.count("(") + cont.count("[")
                depth -= cont.count(")") + cont.count("]")
            if source_origin(rhs) or set(NAME.findall(rhs)) & tainted:
                tainted.add(assign.group(1))
        loop = FOR_TARGET.match(line)
        if loop:
            iterable = loop.group(2)
            if source_origin(iterable) or set(NAME.findall(iterable)) & tainted:
                tainted.add(loop.group(1))
        used_names = set(NAME.findall(line))
        hit = bool(SOURCE_TEXT.search(line))
        if READ_CALL.search(line):
            receivers = {m.group(1) or m.group(2) for m in RECEIVER.finditer(line)}
            receivers.discard(None)
            # Complex receiver: fall back to tainted names on the line.
            hit = hit or sourcey or bool((receivers or used_names) & tainted)
        if AST_PARSE.search(line):
            hit = hit or sourcey or bool(used_names & tainted)
        if hit:
            yield idx


@dataclass(frozen=True)
class Rule:
    name: str
    candidates: Candidates
    line_marker: re.Pattern[str]
    file_marker: re.Pattern[str] | None
    exempt: frozenset[str]
    counted: bool
    noun: str
    remedy: str
    summary: str


RULES = (
    Rule(
        name="source-reads",
        candidates=source_read_lines,
        line_marker=re.compile(r"#\s*rule19:\s*\S"),
        file_marker=None,
        exempt=frozenset(),
        counted=True,
        noun="source-read",
        remedy=(
            "assert on runtime behavior instead; a legitimate survivor (executes "
            "the artifact, or diffs two independently maintained sources) "
            "annotates `# rule19: <reason>` on the line or the line above"
        ),
        summary="tests never read repository source as text",
    ),
    Rule(
        name="deterministic-time",
        candidates=token_lines(
            re.compile(r"\btime\.sleep\(|\btime\.monotonic\(|\bthreading\.Thread\(")
        ),
        line_marker=re.compile(r"#\s*realtime:\s*\S"),
        file_marker=re.compile(r"pytest\.mark\.realtime"),
        # The controlled_time harness is the one module allowed real time.
        exempt=frozenset({"tests/deterministic_time.py"}),
        counted=True,
        noun="real-time",
        remedy=(
            "migrate to tests/deterministic_time.controlled_time, mark the file "
            "pytest.mark.realtime (positive assertions only), or annotate the "
            "line `# realtime: <reason>`"
        ),
        summary=(
            "tests must use controlled time; real-clock tests are limited to "
            "positive latency/bound assertions"
        ),
    ),
    Rule(
        name="subprocess-spawns",
        candidates=token_lines(re.compile(r"\bsys\.executable\b")),
        line_marker=re.compile(r"#\s*subprocess-boundary:\s*\S"),
        file_marker=None,
        exempt=frozenset(),
        # An allowlist reason names the process property under test: signals or
        # crash exit codes, child liveness, env across exec, sandbox isolation,
        # cold-import semantics, entry-point wiring, or cross-process visibility.
        counted=False,
        noun="sys.executable",
        remedy=(
            "drive the CLI in-process via tests/helpers.py:run_cli, or (only for "
            "a genuine process-boundary assertion) annotate `# subprocess-boundary: "
            "<reason>`"
        ),
        summary="tests/helpers.py run_cli is the in-process route",
    ),
)


def offending_lines(rule: Rule, text: str) -> list[tuple[int, str]]:
    if rule.file_marker and rule.file_marker.search(text):
        return []
    lines = text.splitlines()
    marker = rule.line_marker
    return [
        (idx + 1, lines[idx].strip())
        for idx in rule.candidates(lines)
        if not (
            marker.search(lines[idx]) or (idx > 0 and marker.search(lines[idx - 1]))
        )
    ]


def rule_failures(
    rule: Rule, sources: dict[str, str], allowlist: dict[str, int | str]
) -> list[str]:
    found: dict[str, list[tuple[int, str]]] = {}
    for rel, text in sources.items():
        if rel in rule.exempt:
            continue
        offenders = offending_lines(rule, text)
        if offenders:
            found[rel] = offenders

    failures: list[str] = []
    for rel, offenders in sorted(found.items()):
        if not rule.counted and rel in allowlist:
            continue
        allowed = int(allowlist.get(rel, 0)) if rule.counted else 0
        if len(offenders) > allowed:
            for lineno, line in offenders:
                print(f"{rel}:{lineno}: {line}")
            grant = f", allowlist grants {allowed}" if rule.counted else ""
            failures.append(
                f"{rel}: {len(offenders)} {rule.noun} line(s){grant} — {rule.remedy}"
            )
        elif len(offenders) < allowed:
            failures.append(
                f"{rel}: allowlist grants {allowed} but only {len(offenders)} "
                f"remain — ratchet the entry down to {len(offenders)}"
            )
    for rel in sorted(set(allowlist) - set(found)):
        failures.append(f"{rel}: allowlisted but clean (or gone) — delete its entry")
    return failures


def main() -> int:
    allowlists = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    names = {rule.name for rule in RULES}
    if set(allowlists) != names:
        print(
            f"{ALLOWLIST.name} must key exactly the rules {sorted(names)}",
            file=sys.stderr,
        )
        return 1
    sources: dict[str, str] = {}
    for test_dir in TEST_DIRS:
        root = REPO / test_dir
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                sources[path.relative_to(REPO).as_posix()] = path.read_text(
                    encoding="utf-8"
                )

    status = 0
    for rule in RULES:
        failures = rule_failures(rule, sources, allowlists[rule.name])
        if failures:
            print(f"\n{rule.name} lint failed: {rule.summary}:", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
