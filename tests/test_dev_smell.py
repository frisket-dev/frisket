"""Behavioural tests for the smell reservoir in scripts/dev/smell.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

DEV = Path(__file__).parents[1] / "scripts/dev"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, DEV / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worktree = _load("worktree")
suite = _load("suite")
smell = _load("smell")

START = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at: datetime = START):
        self.at = at

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    tree = tmp_path / "checkout"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg/mod.py").write_text(
        "def helper():\n    return 1\n\n\nclass Box:\n    def method(self):\n        pass\n"
    )
    (tree / "pkg/page.ts").write_text("export const widget = 1\n")
    return tree


def record(store: Path, root: Path, clock: Clock | None = None, **fields) -> list[str]:
    values = {
        "file": "pkg/mod.py",
        "symbol": "helper",
        "line": None,
        "kind": "dead",
        "note": "nothing calls it",
        "by": "task-a",
    }
    values.update(fields)
    return smell.add_entry(store, root, "demo", now=clock or Clock(), **values)


def entries(store: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(store.glob("*.json"))]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "ugly"}, "choose one of: duplicate"),
        ({"file": "pkg/nope.py"}, "does not exist"),
        ({"file": "/etc/hosts"}, "repo-relative"),
        ({"file": "../outside.py"}, "escapes"),
        ({"file": "pkg/../../x"}, "escapes"),
        ({"note": "line one\nline two"}, "single line"),
        ({"note": "x" * 241}, "limit is 240"),
        ({"note": "   "}, "required"),
        ({"symbol": "ghost"}, "no definition of 'ghost'"),
        ({"file": "pkg/page.ts", "symbol": "absent"}, "no definition of 'absent'"),
    ],
)
def test_add_refusals_write_nothing(store, root, fields, message):
    with pytest.raises(smell.SmellError, match=message):
        record(store, root, **fields)
    assert not store.exists() or not entries(store)


def test_add_accepts_symbols_and_methods(store, root):
    record(store, root, symbol="Box")
    record(store, root, symbol="Box.method")
    record(store, root, file="pkg/page.ts", symbol="widget")
    record(store, root, symbol=None, line=3)
    assert len(entries(store)) == 4


def test_python_symbol_must_be_a_definition_not_a_word(store, root):
    (root / "pkg/mod.py").write_text("value = 1  # helper\n")
    with pytest.raises(smell.SmellError, match="no definition"):
        record(store, root, symbol="helper")


def test_note_at_the_cap_is_accepted(store, root):
    record(store, root, note="y" * 240)
    assert len(entries(store)[0]["sightings"][0]["note"]) == 240


def test_same_key_increments_instead_of_duplicating(store, root):
    first = record(store, root, note="first")
    second = record(store, root, note="second", by="task-b")
    (entry,) = entries(store)
    assert entry["seen"] == 2
    assert [s["note"] for s in entry["sightings"]] == ["first", "second"]
    assert [s["by"] for s in entry["sightings"]] == ["task-a", "task-b"]
    assert first[0].split()[0] == second[0].split()[0] == entry["id"]
    assert "seen 2" in second[0]


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "duplicate"},
        {"symbol": "Box"},
        {"file": "pkg/page.ts", "symbol": "widget"},
    ],
)
def test_different_key_is_a_separate_entry(store, root, other):
    record(store, root)
    record(store, root, **other)
    assert len(entries(store)) == 2


def test_repo_is_part_of_the_key(store, root):
    record(store, root)
    smell.add_entry(
        store,
        root,
        "other-repo",
        file="pkg/mod.py",
        symbol="helper",
        line=None,
        kind="dead",
        note="n",
        by="x",
    )
    assert len(entries(store)) == 2


def test_line_buckets_group_nearby_lines(store, root):
    record(store, root, symbol=None, line=3)
    record(store, root, symbol=None, line=40)
    record(store, root, symbol=None, line=60)
    seen = sorted(e["seen"] for e in entries(store))
    assert seen == [1, 2]


def test_sightings_keep_only_the_last_five(store, root):
    clock = Clock()
    for n in range(8):
        record(store, root, clock, note=f"note {n}", by=f"task-{n}")
    (entry,) = entries(store)
    assert entry["seen"] == 8
    assert [s["note"] for s in entry["sightings"]] == [f"note {n}" for n in range(3, 8)]


def test_daily_reminder_after_five_by_the_same_task(store, root):
    clock = Clock()
    for n in range(5):
        out = record(store, root, clock, symbol=None, line=n * 100)
        assert len(out) == 1
    out = record(store, root, clock, symbol=None, line=900)
    assert "reminder" in out[1]
    assert len(entries(store)) == 6
    assert len(record(store, root, clock, symbol=None, line=1000, by="task-b")) == 1


def test_daily_reminder_resets_on_a_new_day(store, root):
    clock = Clock()
    for n in range(5):
        record(store, root, clock, symbol=None, line=n * 100)
    clock.at += timedelta(days=1)
    assert len(record(store, root, clock, symbol=None, line=900)) == 1


def test_unknown_by_never_triggers_the_reminder(store, root):
    clock = Clock()
    for n in range(7):
        out = record(store, root, clock, symbol=None, line=n * 100, by=smell.UNKNOWN_BY)
    assert len(out) == 1


def seed(store: Path, root: Path) -> None:
    clock = Clock()
    record(store, root, clock, note="old")
    record(store, root, clock, symbol="Box", kind="duplicate", note="mid")
    record(
        store,
        root,
        clock,
        file="pkg/page.ts",
        symbol="widget",
        kind="other",
        note="new",
    )
    record(store, root, clock, symbol="Box", kind="duplicate", note="mid again")


def test_list_orders_by_seen_then_recency(store, root):
    seed(store, root)
    found = smell.list_entries(store)
    assert [(e["symbol"], e["seen"]) for e in found] == [
        ("Box", 2),
        ("widget", 1),
        ("helper", 1),
    ]


def test_list_filters(store, root):
    seed(store, root)
    assert [e["symbol"] for e in smell.list_entries(store, kind="other")] == ["widget"]
    assert len(smell.list_entries(store, file_prefix="pkg/")) == 3
    assert smell.list_entries(store, file_prefix="pkg/page") != []
    assert smell.list_entries(store, file_prefix="elsewhere/") == []
    assert [e["symbol"] for e in smell.list_entries(store, min_seen=2)] == ["Box"]
    assert smell.list_entries(store, repo="nope") == []


def test_format_entry_is_one_line_with_the_latest_note(store, root):
    seed(store, root)
    line = smell.format_entry(smell.list_entries(store)[0])
    assert "\n" not in line
    assert "x2" in line and "pkg/mod.py::Box" in line and "mid again" in line


def test_list_on_a_missing_store_is_empty(store):
    assert smell.list_entries(store) == []


def test_prune_removes_vanished_files_and_symbols_only(store, root):
    seed(store, root)
    (root / "pkg/page.ts").unlink()
    (root / "pkg/mod.py").write_text("class Box:\n    pass\n")
    dry = smell.prune_entries(store, root, "demo", dry_run=True)
    assert len(dry) == 2 and all(line.startswith("would remove") for line in dry)
    assert len(entries(store)) == 3
    done = smell.prune_entries(store, root, "demo", dry_run=False)
    assert len(done) == 2
    assert [e["symbol"] for e in entries(store)] == ["Box"]


def test_prune_leaves_other_repos_alone(store, root):
    record(store, root)
    (root / "pkg/mod.py").unlink()
    assert smell.prune_entries(store, root, "another", dry_run=False) == []
    assert len(entries(store)) == 1


def test_resolve_moves_entry_to_resolved_log(store, root):
    record(store, root)
    ident = entries(store)[0]["id"]
    message = smell.resolve_entry(store, ident, "fixed", "deleted", Clock())
    assert "resolved as fixed" in message
    assert entries(store) == []
    kept = json.loads((store / "resolved" / f"{ident}.json").read_text())
    assert kept["resolutions"][0]["as"] == "fixed" and kept["seen"] == 1


def test_resolve_unknown_id_refuses(store):
    with pytest.raises(smell.SmellError, match="no open entry"):
        smell.resolve_entry(store, "deadbeef00", "fixed", "x")


def test_re_adding_a_dismissed_key_shows_history_and_records(store, root):
    record(store, root)
    ident = entries(store)[0]["id"]
    smell.resolve_entry(store, ident, "dismissed", "called via getattr", Clock())
    out = record(store, root, note="again")
    assert "dismissed before" in out[1] and "called via getattr" in out[1]
    (entry,) = entries(store)
    assert entry["id"] == ident and entry["seen"] == 1
    smell.resolve_entry(store, ident, "fixed", "gone", Clock())
    history = json.loads((store / "resolved" / f"{ident}.json").read_text())
    assert [r["as"] for r in history["resolutions"]] == ["dismissed", "fixed"]


def test_re_adding_a_fixed_key_is_silent(store, root):
    record(store, root)
    smell.resolve_entry(store, entries(store)[0]["id"], "fixed", "done", Clock())
    assert len(record(store, root)) == 1


def test_atomic_write_replaces_whole_and_leaves_no_temp(tmp_path):
    target = tmp_path / "nested" / "e.json"
    smell.atomic_write_json(target, {"a": 1})
    smell.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["e.json"]


def test_atomic_write_failure_keeps_the_old_file(tmp_path):
    target = tmp_path / "e.json"
    smell.atomic_write_json(target, {"a": 1})
    with pytest.raises(TypeError):
        smell.atomic_write_json(target, {"a": object()})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["e.json"]


def test_stale_lock_is_taken_over(tmp_path):
    target = tmp_path / "e.json"
    lock = tmp_path / "e.json.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    with smell.entry_lock(target):
        assert lock.exists()
    assert not lock.exists()


def test_concurrent_adds_of_one_key_both_count(store, root):
    errors: list[BaseException] = []

    def work(who: str) -> None:
        try:
            record(store, root, by=who)
        except BaseException as error:
            errors.append(error)

    # realtime: the lock is the thing under test
    threads = [threading.Thread(target=work, args=(f"t{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    (entry,) = entries(store)
    assert entry["seen"] == 2 and len(entry["sightings"]) == 2
    assert sorted(p.name for p in store.iterdir()) == [f"{entry['id']}.json"]


def test_store_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv(smell.STORE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert smell.store_path(None) == tmp_path / "xdg/frisket/smells"
    monkeypatch.setenv(smell.STORE_ENV, str(tmp_path / "env"))
    assert smell.store_path(None) == tmp_path / "env"
    assert smell.store_path(tmp_path / "flag") == tmp_path / "flag"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:org/frisket.git", "frisket"),
        ("https://github.com/org/frisket-cloud/", "frisket-cloud"),
        ("", "checkout"),
    ],
)
def test_default_repo_from_remote_or_directory(url, expected, root):
    def runner(args, *, cwd=None, check=True, env=None):
        if not url:
            raise subprocess.CalledProcessError(2, args, "", "no remote")
        return subprocess.CompletedProcess(args, 0, url + "\n", "")

    assert smell.default_repo(root, runner) == expected


def test_cli_list_json_and_resolve(store, root, capsys):
    seed(store, root)
    assert smell.main(["--store", str(store), "list", "--json", "--min-seen", "2"]) == 0
    (listed,) = json.loads(capsys.readouterr().out)
    assert listed["symbol"] == "Box"
    assert smell.main(["--store", str(store), "list", "--kind", "dead"]) == 0
    assert "helper" in capsys.readouterr().out
    argv = [
        "--store",
        str(store),
        "resolve",
        listed["id"],
        "--as",
        "promoted",
        "--note",
        "lint",
    ]
    assert smell.main(argv) == 0
    assert "resolved as promoted" in capsys.readouterr().out
    assert smell.main(argv) == 1
    assert "no open entry" in capsys.readouterr().err


def test_cli_rejects_unknown_resolution(store, capsys):
    with pytest.raises(SystemExit):
        smell.main(
            ["--store", str(store), "resolve", "abc", "--as", "maybe", "--note", "x"]
        )
    assert "invalid choice" in capsys.readouterr().err


def test_help_mentions_every_command(capsys):
    with pytest.raises(SystemExit):
        smell.main(["--help"])
    out = capsys.readouterr().out
    assert all(word in out for word in ("add", "list", "prune", "resolve"))
