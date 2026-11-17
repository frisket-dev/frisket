#!/usr/bin/env python3
"""A reservoir for code smells noticed outside the task at hand.

  smell.py add --file F [--symbol S] [--line N] --kind K --note "..." [--by WHO]
  smell.py list [--kind K] [--file PREFIX] [--min-seen N] [--json]
  smell.py prune [--dry-run]
  smell.py resolve ID --as promoted|fixed|dismissed --note "why"

Recording a smell costs one command and carries no duty to fix it. A periodic
audit triages the reservoir: promote to a lint or gate, fix, or dismiss. An
entry is keyed by (repo, file, symbol or line bucket, kind); recording the same
key again raises its ``seen`` count and keeps the last few sightings, so a smell
several independent tasks flagged stands out.

The store lives outside the repository (entries may mention private code) and
is shared by every worktree: ``--store``, else ``$FRISKET_SMELL_STORE``, else
``<cache>/frisket/smells`` (``$XDG_CACHE_HOME`` honoured, like suite.py). One
JSON file per entry, ``<id>.json``, with resolved entries under
``resolved/<id>.json``, so concurrent writers never share a file and a store
kept in a git repository merges cleanly. Files are written atomically.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Callable, Iterator, Optional

from suite import cache_dir
from worktree import WorktreeError, git, run

KINDS = {
    "duplicate": "the same logic exists elsewhere",
    "dead": "unreferenced or unreachable code",
    "forwarder": "a wrapper that only forwards to another callable",
    "loose-type": "Any, untyped dicts or casts hiding a real shape",
    "missing-guard": "an unchecked input, error path or invariant",
    "suspicious-behaviour": "behaviour that looks wrong but was not verified",
    "misleading-name": "a name that says something the code does not do",
    "stale-comment": "a comment or docstring that no longer matches the code",
    "test-smell": "a test that cannot fail, over-mocks or mirrors the code",
    "other": "anything else worth a second look",
}
NOTE_MAX = 240
SIGHTINGS_KEPT = 5
DAILY_REMINDER = 5
LINE_BUCKET = 50
LOCK_STALE_SECONDS = 10.0
LOCK_WAIT_SECONDS = 15.0
STORE_ENV = "FRISKET_SMELL_STORE"
BY_ENV = "FRISKET_SMELL_BY"
UNKNOWN_BY = "unknown"

Clock = Callable[[], datetime]


class SmellError(RuntimeError):
    """A refusal with a message meant for the person running the script."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def store_path(flag: Optional[Path]) -> Path:
    if flag:
        return flag.expanduser()
    env = os.environ.get(STORE_ENV)
    return Path(env).expanduser() if env else cache_dir() / "smells"


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def entry_lock(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` while reading and rewriting one entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise SmellError(f"timed out waiting for {lock}") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def entry_id(
    repo: str, file: str, symbol: Optional[str], line: Optional[int], kind: str
) -> str:
    place = symbol or (f"L{line // LINE_BUCKET}" if line is not None else "-")
    return sha1("\0".join((repo, file, place, kind)).encode()).hexdigest()[:10]


def check_note(note: str) -> str:
    text = note.strip()
    if not text:
        raise SmellError("--note is required and cannot be blank")
    if "\n" in text or "\r" in text:
        raise SmellError(
            "--note must be a single line; this is a pointer, not an essay"
        )
    if len(text) > NOTE_MAX:
        raise SmellError(f"--note is {len(text)} chars; the limit is {NOTE_MAX}")
    return text


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise SmellError(f"unknown kind {kind!r}; choose one of: {', '.join(KINDS)}")
    return kind


def resolve_file(root: Path, file: str) -> Path:
    if Path(file).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", file):
        raise SmellError(f"--file must be repo-relative, got {file!r}")
    base = root.resolve()
    target = (base / file).resolve()
    if not target.is_relative_to(base):
        raise SmellError(f"--file {file!r} escapes the repository")
    if not target.is_file():
        raise SmellError(f"--file {file!r} does not exist in {base}")
    return target


def symbol_defined(target: Path, symbol: str) -> bool:
    text = target.read_text(errors="replace")
    if target.suffix == ".py":
        name = symbol.rsplit(".", 1)[-1]
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            return any(isinstance(n, defs) and n.name == name for n in ast.walk(tree))
    return re.search(rf"(?<![\w]){re.escape(symbol)}(?![\w])", text) is not None


def check_place(root: Path, file: str, symbol: Optional[str]) -> None:
    target = resolve_file(root, file)
    if symbol and not symbol_defined(target, symbol):
        raise SmellError(f"no definition of {symbol!r} in {file}")


def default_repo(root: Path, runner=run) -> str:
    try:
        url = git(runner, root, "remote", "get-url", "origin")
    except WorktreeError:
        url = ""
    name = re.split(r"[/:\\]", url.rstrip("/"))[-1] if url else ""
    name = name.removesuffix(".git")
    return name or root.name


def load_active(store: Path) -> list[dict]:
    entries = (read_json(path) for path in sorted(store.glob("*.json")))
    return [entry for entry in entries if entry]


def recorded_today(store: Path, by: str, today: str) -> int:
    return sum(
        1
        for entry in load_active(store)
        for sighting in entry["sightings"]
        if sighting["by"] == by and sighting["at"].startswith(today)
    )


def add_entry(
    store: Path,
    root: Path,
    repo: str,
    *,
    file: str,
    symbol: Optional[str],
    line: Optional[int],
    kind: str,
    note: str,
    by: str,
    now: Clock = utc_now,
) -> list[str]:
    """Record or re-record one smell; return the lines to show the caller."""
    check_kind(kind)
    note = check_note(note)
    check_place(root, file, symbol)
    stamp = now().isoformat(timespec="seconds")
    ident = entry_id(repo, file, symbol, line, kind)
    messages: list[str] = []
    if by != UNKNOWN_BY and recorded_today(store, by, stamp[:10]) >= DAILY_REMINDER:
        messages.append(
            f"reminder: {by} has already recorded {DAILY_REMINDER} smells today; "
            "stay on the task, keep only the ones that matter"
        )
    for past in (read_json(store / "resolved" / f"{ident}.json") or {}).get(
        "resolutions", []
    ):
        if past["as"] == "dismissed":
            messages.append(
                f"note: dismissed before ({past['at'][:10]}): {past['note']}; recording again"
            )
    sighting = {"at": stamp, "by": by, "note": note}
    path = store / f"{ident}.json"
    with entry_lock(path):
        entry = read_json(path)
        if entry is None:
            entry = {
                "id": ident,
                "repo": repo,
                "file": file,
                "symbol": symbol,
                "line": line,
                "kind": kind,
                "seen": 0,
                "first_seen": stamp,
                "sightings": [],
            }
        entry["seen"] += 1
        entry["last_seen"] = stamp
        entry["sightings"] = [*entry["sightings"], sighting][-SIGHTINGS_KEPT:]
        atomic_write_json(path, entry)
    messages.insert(0, f"{ident}  seen {entry['seen']}  {kind}  {describe(entry)}")
    return messages


def describe(entry: dict) -> str:
    where = entry["file"]
    if entry.get("symbol"):
        where += f"::{entry['symbol']}"
    elif entry.get("line") is not None:
        where += f":{entry['line']}"
    return where


def list_entries(
    store: Path,
    *,
    kind: Optional[str] = None,
    file_prefix: Optional[str] = None,
    min_seen: int = 1,
    repo: Optional[str] = None,
) -> list[dict]:
    kept = [
        e
        for e in load_active(store)
        if (kind is None or e["kind"] == kind)
        and (file_prefix is None or e["file"].startswith(file_prefix))
        and (repo is None or e["repo"] == repo)
        and e["seen"] >= min_seen
    ]
    kept.sort(key=lambda e: e["last_seen"], reverse=True)
    kept.sort(key=lambda e: e["seen"], reverse=True)
    return kept


def format_entry(entry: dict) -> str:
    last = entry["sightings"][-1]
    return (
        f"{entry['id']}  x{entry['seen']:<2} {entry['kind']:<20} "
        f"{entry['repo']}:{describe(entry)}  - {last['note']}"
    )


def stale_reason(root: Path, entry: dict) -> Optional[str]:
    try:
        check_place(root, entry["file"], entry.get("symbol"))
    except SmellError as error:
        return str(error)
    return None


def prune_entries(store: Path, root: Path, repo: str, *, dry_run: bool) -> list[str]:
    """Drop entries of ``repo`` whose file or symbol is gone from ``root``."""
    lines = []
    for entry in load_active(store):
        if entry["repo"] != repo:
            continue
        reason = stale_reason(root, entry)
        if reason is None:
            continue
        path = store / f"{entry['id']}.json"
        if not dry_run:
            with entry_lock(path):
                path.unlink(missing_ok=True)
        verb = "would remove" if dry_run else "removed"
        lines.append(f"{verb} {entry['id']}  {describe(entry)}  ({reason})")
    return lines


def resolve_entry(
    store: Path, ident: str, outcome: str, note: str, now: Clock = utc_now
) -> str:
    note = check_note(note)
    path = store / f"{ident}.json"
    done = store / "resolved" / f"{ident}.json"
    with entry_lock(path):
        entry = read_json(path)
        if entry is None:
            raise SmellError(f"no open entry {ident!r} in {store}")
        previous = (read_json(done) or {}).get("resolutions", [])
        resolution = {
            "as": outcome,
            "note": note,
            "at": now().isoformat(timespec="seconds"),
        }
        atomic_write_json(done, {**entry, "resolutions": [*previous, resolution]})
        path.unlink()
    return f"{ident} resolved as {outcome}: {describe(entry)}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--store", type=Path, help="reservoir directory")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="record a smell")
    add.add_argument("--file", required=True, help="repo-relative path")
    add.add_argument("--symbol", help="def/class name, or a word in a non-Python file")
    add.add_argument("--line", type=int, help="advisory line number")
    add.add_argument(
        "--kind",
        required=True,
        help="; ".join(f"{name}: {about}" for name, about in KINDS.items()),
    )
    add.add_argument(
        "--note", required=True, help=f"one sentence, at most {NOTE_MAX} chars"
    )
    add.add_argument("--repo", help="repo name (default: origin remote basename)")
    add.add_argument(
        "--by", help=f"who or which task (default ${BY_ENV} or {UNKNOWN_BY})"
    )
    show = commands.add_parser("list", help="show open smells, most-seen first")
    show.add_argument("--kind")
    show.add_argument("--file", dest="file_prefix", metavar="PREFIX")
    show.add_argument("--min-seen", type=int, default=1)
    show.add_argument("--repo")
    show.add_argument("--json", action="store_true")
    prune = commands.add_parser("prune", help="drop entries whose code is gone")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--repo", help="repo whose entries to check")
    done = commands.add_parser("resolve", help="close an entry, keeping its history")
    done.add_argument("id")
    done.add_argument(
        "--as",
        dest="outcome",
        required=True,
        choices=("promoted", "fixed", "dismissed"),
    )
    done.add_argument("--note", required=True, help="why")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = store_path(args.store)
    try:
        if args.command == "list":
            entries = list_entries(
                store,
                kind=args.kind,
                file_prefix=args.file_prefix,
                min_seen=args.min_seen,
                repo=args.repo,
            )
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                print("\n".join(format_entry(e) for e in entries) or "no open smells")
        elif args.command == "resolve":
            print(resolve_entry(store, args.id, args.outcome, args.note))
        else:
            root = Path(git(run, Path.cwd(), "rev-parse", "--show-toplevel"))
            repo = args.repo or default_repo(root)
            if args.command == "prune":
                lines = prune_entries(store, root, repo, dry_run=args.dry_run)
                print("\n".join(lines) or "nothing to prune")
            else:
                by = args.by or os.environ.get(BY_ENV) or UNKNOWN_BY
                print(
                    "\n".join(
                        add_entry(
                            store,
                            root,
                            repo,
                            file=args.file,
                            symbol=args.symbol,
                            line=args.line,
                            kind=args.kind,
                            note=args.note,
                            by=by,
                        )
                    )
                )
    except (SmellError, WorktreeError) as error:
        print(f"smell.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
