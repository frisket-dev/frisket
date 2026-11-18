"""Pure-logic tests for the browser suite's known-failures ratchet."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

CI = Path(__file__).parents[1] / "scripts/ci"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


kf = _load("e2e_known_failures")


def _test(status: str, *results: str, message: str = "Error: boom") -> dict:
    return {
        "projectName": "chromium",
        "status": status,
        "results": [
            {"status": r, "error": {"message": message} if r == "failed" else None}
            for r in results
        ],
    }


def report(specs: dict[str, dict[str, dict]], shard=None, errors=()) -> dict:
    """{file: {title: test}}; a title "A › b" nests b in describe A."""
    suites = []
    for file, tests in specs.items():
        suite: dict = {"title": file, "file": file, "specs": [], "suites": []}
        for title, test in tests.items():
            *describes, leaf = title.split(" › ")
            parent = suite
            for name in describes:
                child = next((s for s in parent["suites"] if s["title"] == name), None)
                if child is None:
                    child = {"title": name, "file": file, "specs": [], "suites": []}
                    parent["suites"].append(child)
                parent = child
            parent["specs"].append({"title": leaf, "file": file, "tests": [test]})
        suites.append(suite)
    return {
        "config": {"shard": shard},
        "suites": suites,
        "errors": [{"message": m} for m in errors],
    }


PASS = _test("expected", "passed")
FAIL = _test("unexpected", "failed", "failed")
FLAKY = _test("flaky", "failed", "passed")
SKIP = _test("skipped", "skipped")


@pytest.fixture
def spec_root(tmp_path: Path) -> Path:
    root = tmp_path / "e2e"
    (root / "docs").mkdir(parents=True)
    for name in ("a.spec.ts", "b.spec.ts", "docs/c.spec.ts"):
        (root / name).write_text("")
    return root


def verdict(known, *reports, spec_root):
    return kf.classify(known, kf.parse_reports(reports), spec_root)


def test_new_failure_is_named_with_its_first_error_line(spec_root):
    run = kf.parse_reports([report({"a.spec.ts": {"ok": PASS, "Grid › breaks": FAIL}})])
    result = kf.classify({}, run, spec_root)
    assert result.new == [("a.spec.ts", "Grid › breaks")]
    assert result.failed
    text = kf.summary({}, run, result, enforcing=True)
    assert "`a.spec.ts › Grid › breaks`: Error: boom" in text


def test_listed_failure_that_still_fails_passes_the_gate(spec_root):
    known = {"a.spec.ts": {"breaks": "stale-expectation"}}
    result = verdict(
        known, report({"a.spec.ts": {"breaks": FAIL}}), spec_root=spec_root
    )
    assert not result.failed
    assert result.still_failing == [("a.spec.ts", "breaks")]


def test_fixed_test_must_leave_the_list(spec_root):
    known = {"a.spec.ts": {"breaks": "stale-expectation"}}
    result = verdict(
        known, report({"a.spec.ts": {"breaks": PASS}}), spec_root=spec_root
    )
    assert result.fixed == [("a.spec.ts", "breaks")]
    assert result.failed
    assert kf.without(known, result.fixed) == {}


def test_flaky_counts_as_passing_and_is_reported(spec_root):
    known = {"a.spec.ts": {"listed": "unclassified"}}
    result = verdict(
        known,
        report({"a.spec.ts": {"listed": FLAKY, "unlisted": FLAKY}}),
        spec_root=spec_root,
    )
    assert result.new == []
    assert result.fixed == [("a.spec.ts", "listed")]
    assert result.flaky == [("a.spec.ts", "listed"), ("a.spec.ts", "unlisted")]


def test_skipped_listed_test_is_neither_fixed_nor_stale(spec_root):
    known = {"a.spec.ts": {"skips": "environment"}}
    result = verdict(known, report({"a.spec.ts": {"skips": SKIP}}), spec_root=spec_root)
    assert not result.failed


def test_entry_for_a_deleted_spec_file_is_stale(spec_root):
    known = {"gone.spec.ts": {"t": "seed:Local stories"}}
    result = verdict(known, report({"a.spec.ts": {"ok": PASS}}), spec_root=spec_root)
    assert result.stale == [(("gone.spec.ts", "t"), "its spec file no longer exists")]
    assert result.failed


def test_renamed_test_is_a_stale_entry_plus_a_new_failure(spec_root):
    known = {"b.spec.ts": {"old title": "retired-request"}}
    result = verdict(
        known, report({"b.spec.ts": {"new title": FAIL}}), spec_root=spec_root
    )
    assert result.stale == [
        (("b.spec.ts", "old title"), "no test in the run has this title")
    ]
    assert result.new == [("b.spec.ts", "new title")]


def test_sharded_reports_merge_into_one_run(spec_root):
    known = {
        "a.spec.ts": {"one": "stale-expectation"},
        "docs/c.spec.ts": {"two": "stale-expectation"},
    }
    result = verdict(
        known,
        report({"a.spec.ts": {"one": FAIL}}, shard={"current": 1, "total": 2}),
        report({"docs/c.spec.ts": {"two": FAIL}}, shard={"current": 2, "total": 2}),
        spec_root=spec_root,
    )
    assert not result.failed
    assert len(result.still_failing) == 2


@pytest.mark.parametrize(
    ("shards", "message"),
    [
        ([1, 3], "missing [2]"),
        ([1, 1, 2, 3], "repeated [1]"),
        ([None, None], "2 unsharded reports"),
    ],
)
def test_incomplete_shard_sets_are_refused(shards, message):
    reports = [
        report(
            {"a.spec.ts": {f"t{i}": PASS}},
            shard=None if n is None else {"current": n, "total": 3},
        )
        for i, n in enumerate(shards)
    ]
    with pytest.raises(kf.ReportError, match=message.replace("[", r"\[")):
        kf.parse_reports(reports)


def test_list_must_be_sorted_and_use_known_reasons():
    known = {
        "a": {"w": "seed:Local stories,Council audio", "x": "seed", "y": "flaky"},
        "0": {},
    }
    assert kf.check_list(known) == [
        "spec files are not sorted",
        "a › x: a seed entry names the projects its test opens, "
        "seed:<project>[,<project>...]",
        "a › y: reason 'flaky' is not one of seed, retired-request, "
        "stale-expectation, environment, unclassified",
    ]


@pytest.mark.parametrize("reason", ["seed:", "seed:Local stories,", "seed: Local"])
def test_a_seed_entry_names_its_projects_exactly(reason):
    assert kf.check_list({"a": {"x": reason}}) == [
        "a › x: a seed entry names the projects its test opens, "
        "seed:<project>[,<project>...]"
    ]


def _main(tmp_path, known, reports, *flags):
    listed = tmp_path / "list.json"
    listed.write_text(kf.dump(known))
    paths = []
    for i, r in enumerate(reports):
        path = tmp_path / f"r{i}.json"
        path.write_text(json.dumps(r))
        paths.append(str(path))
    code = kf.main([*paths, "--list", str(listed), *flags])
    return code, json.loads(listed.read_text())


def test_update_only_removes_entries(tmp_path, monkeypatch, spec_root):
    monkeypatch.setattr(kf, "SPEC_ROOT", spec_root)
    known = {"a.spec.ts": {"fixed": "stale-expectation", "still": "stale-expectation"}}
    run = report({"a.spec.ts": {"fixed": PASS, "still": FAIL}})
    assert _main(tmp_path, known, [run])[0] == 1
    code, after = _main(tmp_path, known, [run], "--update")
    assert code == 0
    assert after == {"a.spec.ts": {"still": "stale-expectation"}}


def test_update_refuses_while_a_new_failure_is_unlisted(
    tmp_path, monkeypatch, spec_root
):
    monkeypatch.setattr(kf, "SPEC_ROOT", spec_root)
    known = {"a.spec.ts": {"fixed": "stale-expectation"}}
    run = report({"a.spec.ts": {"fixed": PASS, "fresh": FAIL}})
    code, after = _main(tmp_path, known, [run], "--update")
    assert code == 1
    assert after == known


def test_report_only_exits_zero_on_differences_but_not_on_run_errors(
    tmp_path, monkeypatch, spec_root
):
    monkeypatch.setattr(kf, "SPEC_ROOT", spec_root)
    run = report({"a.spec.ts": {"fresh": FAIL}})
    assert _main(tmp_path, {}, [run], "--report-only")[0] == 0
    broken = report({"a.spec.ts": {"ok": PASS}}, errors=["Error: spec failed to load"])
    assert _main(tmp_path, {}, [broken], "--report-only")[0] == 1


def _seed_setup(status: dict | None) -> dict:
    test = _test("expected", "passed")
    if status is not None:
        test["annotations"] = [
            {"type": "seed-status", "description": json.dumps(status)}
        ]
    return test


ABSENT = {"Local stories": "absent", "Council audio": "absent"}
USABLE = {"Local stories": None, "Council audio": None}
PARTIAL = {"Local stories": None, "Council audio": "absent"}


def _seed_run(status, outcome):
    return report(
        {
            "seed.setup.ts": {"seed": _seed_setup(status)},
            "a.spec.ts": {"opens": outcome},
        }
    )


@pytest.mark.parametrize("status", [ABSENT, PARTIAL])
def test_a_seed_entry_is_judged_when_its_project_does_not_open(spec_root, status):
    known = {"a.spec.ts": {"opens": "seed:Council audio"}}
    (spec_root / "seed.setup.ts").write_text("")
    still = verdict(known, _seed_run(status, FAIL), spec_root=spec_root)
    assert still.still_failing == [("a.spec.ts", "opens")]
    assert not still.failed
    fixed = verdict(known, _seed_run(status, PASS), spec_root=spec_root)
    assert fixed.fixed == [("a.spec.ts", "opens")]


def test_a_seed_entry_is_judged_when_any_of_its_projects_does_not_open(spec_root):
    known = {"a.spec.ts": {"opens": "seed:Local stories,Council audio"}}
    (spec_root / "seed.setup.ts").write_text("")
    fixed = verdict(known, _seed_run(PARTIAL, PASS), spec_root=spec_root)
    assert fixed.fixed == [("a.spec.ts", "opens")]


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("seed:Local stories", USABLE),
        ("seed:Local stories", PARTIAL),
        ("seed:Council audio", USABLE),
        ("seed:Council audio", None),
    ],
)
@pytest.mark.parametrize("outcome", [PASS, FAIL])
def test_a_seed_entry_is_reported_not_judged_when_its_projects_open(
    spec_root, reason, status, outcome
):
    known = {"a.spec.ts": {"opens": reason}}
    run = kf.parse_reports([_seed_run(status, outcome)])
    result = kf.classify(known, run, spec_root)
    assert not result.failed
    assert result.seed_unjudged == [
        (("a.spec.ts", "opens"), kf.OUTCOME[outcome["status"]])
    ]
    assert "Listed under `seed`, not judged: 1" in kf.summary(
        known, run, result, enforcing=True
    )


def test_a_seed_entry_naming_an_unrecorded_project_is_stale(spec_root):
    known = {"a.spec.ts": {"opens": "seed:Faces demo"}}
    result = verdict(known, _seed_run(USABLE, FAIL), spec_root=spec_root)
    assert result.stale == [
        (
            ("a.spec.ts", "opens"),
            "names Faces demo, which the seed setup does not record",
        )
    ]
    assert result.failed


def test_seed_entries_count_under_the_seed_reason(spec_root):
    known = {"a.spec.ts": {"opens": "seed:Council audio"}}
    run = kf.parse_reports([_seed_run(ABSENT, FAIL)])
    text = kf.summary(known, run, kf.classify(known, run, spec_root), enforcing=True)
    assert "| seed | 1 | 1 |" in text


def test_a_stale_seed_entry_is_still_stale(spec_root):
    known = {"a.spec.ts": {"renamed": "seed:Local stories"}}
    result = verdict(known, _seed_run(USABLE, PASS), spec_root=spec_root)
    assert result.stale == [
        (("a.spec.ts", "renamed"), "no test in the run has this title")
    ]
