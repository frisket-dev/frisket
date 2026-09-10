#!/usr/bin/env python3
"""Print a lightweight local development timeline.

The timeline combines three local signals:

- eval.py timing envelopes from .frisket/eval-timings.sqlite
- direct check envelopes from .frisket/check-runs.jsonl
- local git commits from the same time window

It intentionally infers "coding/review" gaps from silence between observed
events. Those gaps are useful for orientation, but they are not proof of what
the agent was doing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / ".frisket" / "eval-timings.sqlite"
DEFAULT_CHECKLOG = ROOT / ".frisket" / "check-runs.jsonl"

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}

PASS_OUTCOMES = {"PASS", "HUMAN", "HUMAN✓", "JUDGE✓"}


@dataclass
class Event:
    kind: str
    start: datetime
    finish: datetime
    title: str
    outcome: str | None = None
    seconds: float = 0.0
    detail: str | None = None
    children: list["Event"] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Color:
    enabled: bool

    def apply(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(ANSI[style] for style in styles if style in ANSI)
        if not prefix:
            return text
        return f"{prefix}{text}{ANSI['reset']}"


def color_enabled(choice: str) -> bool:
    if choice == "always":
        return True
    if choice == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def style_outcome(outcome: str | None, color: Color) -> str | None:
    if not outcome:
        return None
    if outcome == "FAIL":
        return color.apply(outcome, "red", "bold")
    if outcome == "WARN":
        return color.apply(outcome, "yellow", "bold")
    if outcome in PASS_OUTCOMES:
        return color.apply(outcome, "green", "bold")
    if outcome.startswith("SKIP"):
        return color.apply(outcome, "dim")
    return color.apply(outcome, "magenta", "bold")


def style_commit_cadence(seconds: float, color: Color) -> str:
    label = f"(+{fmt_duration(seconds)})"
    return color.apply(label, "yellow")


def style_commit_loc(event: Event, color: Color) -> str | None:
    added = event.payload.get("lines_added")
    deleted = event.payload.get("lines_deleted")
    if not isinstance(added, int) or not isinstance(deleted, int):
        return None
    label = f"LOC +{added}/-{deleted}"
    binary_files = event.payload.get("binary_files")
    if isinstance(binary_files, int) and binary_files:
        label = f"{label} ({binary_files} binary)"
    return color.apply(label, "magenta")


def parse_eval_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        if re.search(r"[+-]\d{4}$", value):
            fixed = f"{value[:-5]}{value[-5:-2]}:{value[-2:]}"
            return datetime.fromisoformat(fixed)
        raise


def parse_since(value: str, *, now: datetime) -> datetime:
    raw = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", raw)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        multiplier = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 24 * 60 * 60,
        }[unit]
        return now - timedelta(seconds=amount * multiplier)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--since must be relative like 4h/30m or an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def fmt_clock(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def load_eval_events(
    db_path: Path,
    *,
    since: datetime,
    include_skips: bool,
    detail_limit: int,
) -> list[Event]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, batch_id, task_id, check_kind, command_key, outcome,
                   elapsed_seconds, started_at, finished_at, source
            FROM eval_timing_events
            WHERE finished_at >= ? OR started_at >= ?
            ORDER BY started_at, id
            """,
            (since.strftime("%Y-%m-%dT%H:%M:%S%z"),) * 2,
        ).fetchall()
    finally:
        conn.close()

    by_batch: dict[str, list[sqlite3.Row]] = defaultdict(list)
    batch_rows: dict[str, sqlite3.Row] = {}
    top_rows: list[sqlite3.Row] = []
    for row in rows:
        if not include_skips and str(row["outcome"]).startswith("SKIP"):
            continue
        batch_id = row["batch_id"]
        source = row["source"]
        if batch_id and source == "batch":
            batch_rows[str(batch_id)] = row
            top_rows.append(row)
        elif batch_id and source == "batch-task":
            by_batch[str(batch_id)].append(row)
        elif source != "batch-task":
            top_rows.append(row)

    events: list[Event] = []
    for row in top_rows:
        start = parse_eval_time(str(row["started_at"]))
        finish = parse_eval_time(str(row["finished_at"]))
        outcome = str(row["outcome"])
        check_kind = str(row["check_kind"])
        source = str(row["source"])
        batch_id = row["batch_id"]
        task_id = row["task_id"]
        if source == "batch":
            children = [
                Event(
                    kind="eval-task",
                    start=parse_eval_time(str(child["started_at"])),
                    finish=parse_eval_time(str(child["finished_at"])),
                    title=str(child["task_id"] or child["command_key"]),
                    outcome=str(child["outcome"]),
                    seconds=float(child["elapsed_seconds"]),
                )
                for child in by_batch.get(str(batch_id), [])
            ]
            children.sort(key=lambda item: item.seconds, reverse=True)
            title = f"running {check_kind} batch"
            detail = f"{len(children)} task(s)"
        elif task_id:
            children = []
            title = f"running {check_kind}: {task_id}"
            detail = None
        else:
            children = []
            title = f"running {check_kind}"
            detail = source if source != "task" else None
        event = Event(
            kind="eval",
            start=start,
            finish=finish,
            title=title,
            outcome=outcome,
            seconds=float(row["elapsed_seconds"]),
            detail=detail,
            children=children[:detail_limit],
            payload={"batch_id": batch_id, "source": source},
        )
        events.append(event)
    return events


def load_checklog_events(log_path: Path, *, since: datetime) -> list[Event]:
    if not log_path.exists():
        return []
    events: list[Event] = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            start = parse_eval_time(str(raw["started_at"]))
            finish = parse_eval_time(str(raw["finished_at"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if finish < since and start < since:
            continue
        kind = str(raw.get("kind") or raw.get("source") or "check")
        label = str(raw.get("label") or kind)
        outcome = str(raw.get("outcome") or "")
        events.append(
            Event(
                kind="check",
                start=start,
                finish=finish,
                title=f"running {kind}: {label}",
                outcome=outcome or None,
                seconds=float(
                    raw.get("elapsed_seconds") or (finish - start).total_seconds()
                ),
                payload={
                    "source": raw.get("source"),
                    "returncode": raw.get("returncode"),
                    "metadata": raw.get("metadata"),
                },
            )
        )
    events.sort(key=lambda item: item.start)
    return events


def load_git_commits(*, since: datetime, cwd: Path) -> list[Event]:
    cmd = [
        "git",
        "log",
        f"--since={since.isoformat()}",
        "--pretty=format:%ct%x00%H%x00%h%x00%s",
    ]
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return []
    events: list[Event] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ts_raw, commit_hash, short_hash, subject = line.split("\x00", 3)
            when = datetime.fromtimestamp(int(ts_raw), tz=since.tzinfo)
        except ValueError:
            continue
        line_stats = load_commit_line_stats(commit_hash, cwd=cwd)
        events.append(
            Event(
                kind="commit",
                start=when,
                finish=when,
                title=f"commit {short_hash} {subject}",
                seconds=0.0,
                payload={
                    "commit_hash": commit_hash,
                    "short_hash": short_hash,
                    **line_stats,
                },
            )
        )
    events.sort(key=lambda item: item.start)
    previous_commit_time = None
    if events:
        first_hash = events[0].payload.get("commit_hash")
        if isinstance(first_hash, str):
            previous_commit_time = load_parent_commit_time(
                first_hash,
                cwd=cwd,
                tz=since.tzinfo,
            )
    for event in events:
        if previous_commit_time is not None:
            event.payload["seconds_since_previous_commit"] = max(
                0.0,
                (event.start - previous_commit_time).total_seconds(),
            )
        previous_commit_time = event.start
    return events


def load_commit_line_stats(commit_hash: str, *, cwd: Path) -> dict[str, int]:
    proc = subprocess.run(
        ["git", "show", "--numstat", "--format=", "--find-renames", commit_hash],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    added = 0
    deleted = 0
    binary_files = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[0] == "-" or parts[1] == "-":
            binary_files += 1
            continue
        try:
            added += int(parts[0])
            deleted += int(parts[1])
        except ValueError:
            continue
    return {
        "lines_added": added,
        "lines_deleted": deleted,
        "binary_files": binary_files,
    }


def load_parent_commit_time(
    commit_hash: str,
    *,
    cwd: Path,
    tz: Any,
) -> datetime | None:
    proc = subprocess.run(
        ["git", "show", "-s", "--pretty=format:%ct", f"{commit_hash}^"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return datetime.fromtimestamp(int(proc.stdout.strip()), tz=tz)
    except ValueError:
        return None


def gap_label(previous: Event, current: Event) -> str:
    if current.kind == "commit":
        return "inferred coding/review before commit"
    if previous.outcome == "FAIL":
        return "inferred coding/review after failed check"
    if previous.outcome and previous.outcome not in {
        "PASS",
        "HUMAN",
        "HUMAN✓",
        "JUDGE✓",
    }:
        return "inferred coding/review after non-passing check"
    return "inferred coding/review gap"


def print_text(
    events: list[Event],
    *,
    since: datetime,
    gap_seconds: float,
    color: Color,
) -> None:
    print(color.apply(f"Timeline since {since.isoformat(timespec='seconds')}", "bold"))
    if not events:
        print(color.apply("no eval timing rows or commits in this window", "dim"))
        return

    previous: Event | None = None
    for event in sorted(events, key=lambda item: (item.start, item.kind)):
        if previous is not None:
            gap = (event.start - previous.finish).total_seconds()
            if gap >= gap_seconds:
                print(
                    "  ".join(
                        [
                            color.apply(fmt_clock(previous.finish), "dim"),
                            color.apply(fmt_duration(gap), "yellow"),
                            color.apply(gap_label(previous, event), "dim"),
                        ]
                    )
                )
        if event.kind == "commit":
            since_previous = event.payload.get("seconds_since_previous_commit")
            cadence = (
                style_commit_cadence(float(since_previous), color)
                if isinstance(since_previous, (int, float))
                else None
            )
            pieces = [
                color.apply(fmt_clock(event.start), "dim"),
                color.apply(event.title, "cyan", "bold"),
            ]
            if cadence:
                pieces.append(cadence)
            loc = style_commit_loc(event, color)
            if loc:
                pieces.append(loc)
            print("  ".join(pieces))
        else:
            outcome = style_outcome(event.outcome, color)
            pieces = [
                color.apply(fmt_clock(event.start), "dim"),
                color.apply(
                    f"{fmt_duration((event.finish - event.start).total_seconds())}",
                    "yellow" if event.outcome == "FAIL" else "blue",
                ),
                color.apply(event.title, "blue", "bold"),
            ]
            if outcome:
                pieces.append(outcome)
            if event.detail:
                pieces.append(color.apply(f"({event.detail})", "dim"))
            print("  ".join(pieces))
            for child in event.children:
                child_outcome = style_outcome(child.outcome, color)
                print(
                    "  - "
                    + " ".join(
                        [
                            color.apply(fmt_duration(child.seconds), "blue"),
                            child.title,
                            child_outcome or "",
                        ]
                    ).rstrip()
                )
        if previous is None or event.finish > previous.finish:
            previous = event


def compact_events(
    events: list[Event],
    *,
    cluster_gap_seconds: float,
    detail_limit: int,
) -> list[Event]:
    compacted: list[Event] = []
    current: list[Event] = []

    def flush_current() -> None:
        if not current:
            return
        if len(current) == 1:
            compacted.append(current[0])
            current.clear()
            return
        start = min(event.start for event in current)
        finish = max(event.finish for event in current)
        outcomes = {event.outcome for event in current if event.outcome}
        if any(outcome == "FAIL" for outcome in outcomes):
            outcome = "FAIL"
        elif any(outcome == "WARN" for outcome in outcomes):
            outcome = "WARN"
        elif outcomes and outcomes <= {"PASS", "HUMAN", "HUMAN✓", "JUDGE✓"}:
            outcome = "PASS"
        else:
            outcome = ", ".join(sorted(outcomes)) if outcomes else None
        children = sorted(
            current,
            key=lambda item: (item.finish - item.start).total_seconds(),
            reverse=True,
        )[:detail_limit]
        compacted.append(
            Event(
                kind="eval-cluster",
                start=start,
                finish=finish,
                title="running checks",
                outcome=outcome,
                seconds=(finish - start).total_seconds(),
                detail=f"{len(current)} event(s)",
                children=children,
            )
        )
        current.clear()

    for event in sorted(events, key=lambda item: (item.start, item.kind)):
        if event.kind not in {"eval", "check"}:
            flush_current()
            compacted.append(event)
            continue
        if not current:
            current.append(event)
            continue
        previous = max(current, key=lambda item: item.finish)
        gap = (event.start - previous.finish).total_seconds()
        if gap <= cluster_gap_seconds:
            current.append(event)
        else:
            flush_current()
            current.append(event)
    flush_current()
    return compacted


def event_to_json(event: Event) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "start": event.start.isoformat(),
        "finish": event.finish.isoformat(),
        "title": event.title,
        "outcome": event.outcome,
        "seconds": event.seconds,
        "detail": event.detail,
        "children": [event_to_json(child) for child in event.children],
        "payload": event.payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="4h",
        help="Window to show, e.g. 30m, 4h, 1d, or an ISO timestamp. Default: 4h.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="eval timing SQLite DB. Default: .frisket/eval-timings.sqlite",
    )
    parser.add_argument(
        "--checklog",
        type=Path,
        default=DEFAULT_CHECKLOG,
        help="direct check JSONL log. Default: .frisket/check-runs.jsonl",
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=60.0,
        help="Print inferred coding/review gaps at or above this many seconds.",
    )
    parser.add_argument(
        "--details",
        type=int,
        default=0,
        help=(
            "Number of slowest batch task rows to show under each batch. Default: 0."
        ),
    )
    parser.add_argument(
        "--cluster-gap-seconds",
        type=float,
        default=15.0,
        help="In compact mode, merge adjacent eval rows separated by at most this many seconds.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show every eval timing row instead of compact check blocks.",
    )
    parser.add_argument(
        "--include-skips",
        action="store_true",
        help="Include skipped live/advisory checks. Hidden by default.",
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Colorize text output. Default: auto.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON events.")
    args = parser.parse_args(argv)

    now = datetime.now().astimezone()
    since = parse_since(args.since, now=now)
    events = [
        *load_eval_events(
            args.db,
            since=since,
            include_skips=args.include_skips,
            detail_limit=max(args.details, 0),
        ),
        *load_checklog_events(args.checklog, since=since),
        *load_git_commits(since=since, cwd=ROOT),
    ]
    events.sort(key=lambda item: (item.start, item.kind))
    display_events = (
        events
        if args.full
        else compact_events(
            events,
            cluster_gap_seconds=args.cluster_gap_seconds,
            detail_limit=max(args.details, 0),
        )
    )

    if args.json:
        print(json.dumps([event_to_json(event) for event in display_events], indent=2))
    else:
        print_text(
            display_events,
            since=since,
            gap_seconds=args.gap_seconds,
            color=Color(color_enabled(args.color)),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
