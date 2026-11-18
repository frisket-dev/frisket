#!/usr/bin/env python3
"""Known-failures ratchet for the full browser suite: the list only shrinks.

Reads one or more Playwright JSON reports (one per shard, or one for a whole
run) and compares their failures with LIST, which maps each failing test's
spec file (relative to web/tests/e2e) and title (describe titles and the test
title joined by " › ") to a reason class from REASONS. Titles, not line
numbers, key the list, so editing a spec does not churn it.

It fails when
  - a test fails that the list does not name (a new failure);
  - a listed test passes, including on retry (delete it from the list, or run
    with ``--update``, which only ever removes entries);
  - a listed spec file or title no longer exists (rename the entry by hand
    when the test was renamed, delete it when the test was deleted);
  - the reports are incomplete: a shard is missing or repeated, or the run
    reported an error outside any test (a spec that does not load, a server
    that does not start).
A test that passed only on retry counts as passing and is reported as flaky.

Entries with the `seed` reason fail only because the demo projects the seed
setup provides (web/tests/e2e/seed.setup.ts) are missing or unopenable, as on
a runner with no seeded cache. The setup records which projects open in a
`seed-status` annotation; `seed` entries are judged like any other only when
that record says no seeded project opens. When some or all of them open, or
the run has no record (``--no-deps``), they are reported but not judged.
The script never adds an entry: listing a failure is a hand edit that names
its reason class. ``--report-only`` prints the same verdict and exits 0 unless
the reports are incomplete, for runs whose list has not yet been reconciled
with the machine they run on.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LIST = REPO / "scripts/ci/e2e_known_failures.json"
SPEC_ROOT = REPO / "web/tests/e2e"
REASONS = (
    "seed",
    "retired-request",
    "stale-expectation",
    "environment",
    "unclassified",
)
TITLE_JOIN = " › "
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

KnownList = dict[str, dict[str, str]]
Key = tuple[str, str]

# Playwright test outcomes, worst first: one key seen twice keeps the worse.
RANK = {"failed": 0, "flaky": 1, "passed": 2, "skipped": 3}
OUTCOME = {
    "unexpected": "failed",
    "flaky": "flaky",
    "expected": "passed",
    "skipped": "skipped",
}


class ReportError(Exception):
    """The reports cannot support a verdict."""


@dataclass
class Run:
    outcomes: dict[Key, str] = field(default_factory=dict)
    messages: dict[Key, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # seeded project name -> None when it opens, else why not; None if unrecorded
    seed_status: dict[str, str | None] | None = None

    @property
    def seed_absent(self) -> bool:
        """True when the run recorded that no seeded project opens."""
        return bool(self.seed_status) and all(self.seed_status.values())


@dataclass
class Verdict:
    new: list[Key]
    fixed: list[Key]
    stale: list[tuple[Key, str]]
    flaky: list[Key]
    still_failing: list[Key]
    # `seed` entries the run cannot judge, with their outcome
    seed_unjudged: list[tuple[Key, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.new or self.fixed or self.stale)


def label(key: Key) -> str:
    return f"{key[0]}{TITLE_JOIN}{key[1]}"


def _first_line(message: str) -> str:
    for line in ANSI.sub("", message).splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _read_seed_status(test: dict, run: Run) -> None:
    results = test.get("results", [])
    annotations = [
        *test.get("annotations", []),
        *(a for r in results for a in r.get("annotations", [])),
    ]
    for annotation in annotations:
        if annotation.get("type") == "seed-status":
            run.seed_status = json.loads(annotation["description"])


def _walk(suite: dict, describes: tuple[str, ...], run: Run) -> None:
    for spec in suite.get("specs", []):
        key = (spec["file"], TITLE_JOIN.join((*describes, spec["title"])))
        for test in spec.get("tests", []):
            _read_seed_status(test, run)
            outcome = OUTCOME[test["status"]]
            previous = run.outcomes.get(key)
            if previous is None or RANK[outcome] < RANK[previous]:
                run.outcomes[key] = outcome
            for result in test.get("results", []):
                message = (result.get("error") or {}).get("message")
                if message and key not in run.messages:
                    run.messages[key] = _first_line(message)
    for child in suite.get("suites", []):
        _walk(child, (*describes, child["title"]), run)


def _check_shards(shards: list[dict | None]) -> None:
    """Refuse anything but one unsharded report or exactly one per shard."""
    if not shards:
        raise ReportError("no reports given")
    if all(shard is None for shard in shards):
        if len(shards) > 1:
            raise ReportError(
                f"{len(shards)} unsharded reports; give one, or one per shard"
            )
        return
    totals = {shard["total"] for shard in shards if shard}
    if None in shards or len(totals) != 1:
        raise ReportError("the reports mix sharded and unsharded runs")
    (total,) = totals
    seen = Counter(shard["current"] for shard in shards if shard)
    missing = sorted(set(range(1, total + 1)) - set(seen))
    repeated = sorted(n for n, times in seen.items() if times > 1)
    if missing or repeated:
        raise ReportError(
            f"shard reports of {total}: missing {missing or 'none'}, "
            f"repeated {repeated or 'none'}"
        )


def parse_reports(reports: Iterable[dict]) -> Run:
    """Merge Playwright JSON reports, refusing an incomplete shard set."""
    run = Run()
    shards: list[dict | None] = []
    for report in reports:
        shards.append((report.get("config") or {}).get("shard"))
        for error in report.get("errors", []):
            run.errors.append(_first_line(error.get("message", "")) or str(error))
        for suite in report.get("suites", []):
            # A top-level suite is a spec file; nested suites are describes.
            _walk(suite, (), run)
    _check_shards(shards)
    if not run.outcomes:
        raise ReportError("the reports contain no tests")
    return run


def check_list(known: KnownList) -> list[str]:
    """Return the problems that make the list itself unusable."""
    problems = []
    if list(known) != sorted(known):
        problems.append("spec files are not sorted")
    for spec, titles in known.items():
        if list(titles) != sorted(titles):
            problems.append(f"{spec}: titles are not sorted")
        for title, reason in titles.items():
            if reason not in REASONS:
                problems.append(
                    f"{spec}{TITLE_JOIN}{title}: reason {reason!r} is not one of "
                    + ", ".join(REASONS)
                )
    return problems


def classify(known: KnownList, run: Run, spec_root: Path = SPEC_ROOT) -> Verdict:
    entries = {
        (s, t): reason for s, titles in known.items() for t, reason in titles.items()
    }
    present = entries.keys() & run.outcomes.keys()
    unjudged = set()
    if not run.seed_absent:
        unjudged = {key for key in present if entries[key] == "seed"}
    listed = entries.keys() - unjudged
    failed = {key for key, outcome in run.outcomes.items() if outcome == "failed"}
    passing = {
        key for key, outcome in run.outcomes.items() if outcome in {"passed", "flaky"}
    }
    stale = []
    for key in sorted(listed - run.outcomes.keys()):
        if not (spec_root / key[0]).is_file():
            stale.append((key, "its spec file no longer exists"))
        else:
            stale.append((key, "no test in the run has this title"))
    return Verdict(
        new=sorted(failed - entries.keys()),
        fixed=sorted(listed & passing),
        stale=stale,
        flaky=sorted(k for k, o in run.outcomes.items() if o == "flaky"),
        still_failing=sorted(listed & failed),
        seed_unjudged=sorted((key, run.outcomes[key]) for key in unjudged),
    )


def without(known: KnownList, keys: Iterable[Key]) -> KnownList:
    drop = set(keys)
    pruned: KnownList = {}
    for spec, titles in known.items():
        kept = {t: r for t, r in titles.items() if (spec, t) not in drop}
        if kept:
            pruned[spec] = kept
    return pruned


def dump(known: KnownList) -> str:
    return json.dumps(known, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def summary(known: KnownList, run: Run, verdict: Verdict, *, enforcing: bool) -> str:
    """Markdown verdict for a job summary or an issue body."""
    totals = Counter(run.outcomes.values())
    reasons = {
        (spec, title): reason
        for spec, titles in known.items()
        for title, reason in titles.items()
    }
    still = Counter(reasons[key] for key in verdict.still_failing)
    listed = Counter(reasons.values())
    mode = "enforcing" if enforcing else "report-only (the list is not yet enforced)"
    lines = [
        f"### Browser suite known-failures ratchet: {mode}",
        "",
        f"{len(run.outcomes)} tests: {totals['passed']} passed, {totals['failed']} "
        f"failed, {totals['flaky']} flaky, {totals['skipped']} skipped.",
        "",
        "| Reason | Listed | Still failing |",
        "| --- | --- | --- |",
        *(f"| {r} | {listed[r]} | {still[r]} |" for r in REASONS),
        "",
    ]

    def section(title: str, rows: list[str]) -> None:
        lines.append(f"**{title}: {len(rows)}**")
        lines.extend(f"- {row}" for row in rows)
        lines.append("")

    section(
        "New failures (fix them, or list them by hand with a reason)",
        [
            f"`{label(k)}`" + (f": {run.messages[k]}" if run.messages.get(k) else "")
            for k in verdict.new
        ],
    )
    section(
        "Listed but now passing (delete them from the list, or run with --update)",
        [f"`{label(k)}`" for k in verdict.fixed],
    )
    section(
        "Listed but gone (rename the entry if the test was renamed, else delete it)",
        [f"`{label(k)}`: {why}" for k, why in verdict.stale],
    )
    section("Flaky (passed on retry)", [f"`{label(k)}`" for k in verdict.flaky])
    if verdict.seed_unjudged:
        if run.seed_status is None:
            why = "the run has no seed-status record (the seed setup did not run)"
        else:
            usable = sum(1 for reason in run.seed_status.values() if reason is None)
            why = f"{usable} of {len(run.seed_status)} seeded projects open in this run"
        outcomes = Counter(outcome for _, outcome in verdict.seed_unjudged)
        lines.append(
            f"**Listed under `seed`, not judged: {len(verdict.seed_unjudged)}** ({why}; "
            + ", ".join(f"{n} {o}" for o, n in sorted(outcomes.items()))
            + ")"
        )
        lines.append("")
    if run.errors:
        section("Errors outside any test", run.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="Playwright JSON reports")
    parser.add_argument(
        "--update", action="store_true", help="delete listed tests that now pass"
    )
    parser.add_argument(
        "--report-only", action="store_true", help="print the verdict and exit 0"
    )
    parser.add_argument(
        "--summary", type=Path, help="also write the verdict as Markdown"
    )
    parser.add_argument("--list", type=Path, default=LIST, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    known: KnownList = json.loads(args.list.read_text(encoding="utf-8"))
    problems = check_list(known)
    if problems:
        for line in problems:
            print(line)
        print(f"e2e_known_failures: {args.list.name} is malformed; fix it by hand.")
        return 1
    try:
        run = parse_reports(
            json.loads(p.read_text(encoding="utf-8")) for p in args.reports
        )
    except ReportError as exc:
        print(f"e2e_known_failures: cannot judge this run: {exc}.")
        return 1
    verdict = classify(known, run, SPEC_ROOT)
    text = summary(known, run, verdict, enforcing=not args.report_only)
    print(text)
    if args.summary:
        args.summary.write_text(text + "\n", encoding="utf-8")
    if run.errors:
        print(f"e2e_known_failures: {len(run.errors)} error(s) outside any test.")
        return 1
    if args.update and not verdict.new and not verdict.stale and verdict.fixed:
        args.list.write_text(dump(without(known, verdict.fixed)), encoding="utf-8")
        print(f"e2e_known_failures: removed {len(verdict.fixed)} passing test(s).")
        return 0
    if not verdict.failed:
        print(
            f"e2e_known_failures: ok ({len(verdict.still_failing)} listed failures, "
            "no new ones)."
        )
        return 0
    if args.report_only:
        print("e2e_known_failures: differences above are reported, not enforced.")
        return 0
    print(
        f"e2e_known_failures: {len(verdict.new)} new failure(s), {len(verdict.fixed)} "
        f"listed test(s) now passing, {len(verdict.stale)} stale entr(ies). "
        "The list only shrinks: fix new failures rather than listing them."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
