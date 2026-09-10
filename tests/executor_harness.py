"""Shared lifecycle tests for the v1 executor family.

Every migrated ``tests/test_*_executor.py`` file declares a module-level
``CASES: list[ExecutorCase]`` (plain Python literals — no YAML, no plugin) and
registers its module name in ``CASE_MODULES`` below. This module then owns the
five-part skeleton those files used to copy: catalog-entry mirror, validation
gates, run+persist+counts, idempotent replay, reservation lifecycle, and
undo/rerun-reuse. Per-file remainders are only the genuinely unique scenarios.

Extension contract (the mechanical sweep appends rows, never edits tests):
  * new file: define ``CASES`` there, append the module name to
    ``CASE_MODULES`` here — two lines total in this module's history.
  * ``test_catalog_entry`` grows one node per distinct ``kind``; the other
    tests grow one node per case (or per gate) automatically.

Case modules are imported lazily inside ``pytest_generate_tests`` (not at
module top) so pilots may import this module's dataclasses without an import
cycle. Runs go through ``frisket.executor.run_action_spec`` in-process; a
case's ``patch`` hook may stub transports/routers and return extra
``run_action_spec`` kwargs (e.g. ``{"router": ...}``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

import pytest

from frisket.contracts.action import ActionResult
from frisket.engine.store import Project
from action_test_helpers import counts as table_counts


def assert_pre_dispatch_failure_is_durable(
    project: Project,
    before: dict[str, int],
    after: dict[str, int],
) -> int:
    """Assert the canonical post-prepare failure shape.

    Once a direct action has published its run/output family, a fake runner
    failure no longer rewinds history to zero rows. It must leave one
    terminal failed run with no live writer, results, model calls, or receipt.
    This keeps the old tests' no-effect/reservation-cleanup proof while
    recognizing the now-auditable preparation record.
    """

    assert after["runs"] == before["runs"] + 1
    assert after["ops"] == before["ops"] + 1
    for table in ("results", "model_calls", "receipts"):
        if table in before:
            assert after[table] == before[table]
    run = project.db.execute(
        "SELECT id, status, finished_at, current_attempt_id "
        "FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run is not None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert run["current_attempt_id"] is None
    attempts = project.db.execute(
        "SELECT state FROM execution_attempts WHERE run_id=?",
        (int(run["id"]),),
    ).fetchall()
    assert all(row["state"] == "halted" for row in attempts)
    return int(run["id"])


# Append-only registry: one module name per migrated executor test file.
CASE_MODULES: tuple[str, ...] = (
    "tests.engine.test_import_csv_executor",
    "tests.engine.test_map_python_executor",
    "tests.engine.test_cell_edit_executor",
    "tests.engine.test_import_kml_executor",
    "tests.engine.test_import_ndjson_executor",
    "tests.engine.test_import_geojson_executor",
    "tests.engine.test_import_files_executor",
    "tests.ai.test_embedding_index_project_executor",
    "tests.ai.test_embedding_index_cluster_executor",
    "tests.engine.test_embedding_index_export_executor",
    "tests.engine.test_source_create_executor",
    "tests.engine.test_source_check_executor",
    "tests.engine.test_source_update_delete_executor",
    "tests.engine.test_column_patch_executor",
    "tests.engine.test_row_add_executor",
    "tests.engine.test_row_delete_executor",
    "tests.engine.test_import_rows_executor",
    "tests.engine.test_cluster_values_executor",
    "tests.ops.test_map_regex_extract_executor",
    "tests.ai.test_map_ner_executor",
    "tests.engine.test_map_translate_executor",
    "tests.engine.test_map_extract_executor",
    "tests.engine.test_map_classify_executor",
    "tests.ai.test_join_semantic_executor",
    "tests.engine.test_resolve_entities_executor",
    "tests.engine.test_derive_table_from_list_executor",
    "tests.engine.test_column_set_type_executor",
    "tests.engine.test_review_decision_executor",
    "tests.engine.test_derive_link_table_executor",
    "tests.engine.test_derive_join_executor",
    "tests.engine.test_reduce_group_summary_executor",
    "tests.engine.test_media_to_markdown_executor",
    "tests.engine.test_media_ocr_executor",
    "tests.engine.test_media_enclosure_materialize_executor",
    "tests.engine.test_media_transcribe_executor",
    "tests.engine.test_row_file_executor_cases",
    "tests.ops.test_media_youtube_download_executor",
    "tests.engine.test_export_work_log_executor",
    "tests.engine.test_export_sheet_csv_executor",
    "tests.engine.test_export_column_tables_executor",
    "tests.engine.test_research_web_search_executor",
    "tests.engine.test_research_answer_executor",
    "tests.authoring.test_plugin_load_executor",
    "tests.engine.test_operation_undo_redo_executor",
)

Seeded = Any


@dataclass(frozen=True)
class CatalogEntry:
    """Expected catalog payload for one action kind.

    ``side_effects`` is compared exactly; ``error_codes`` is a required
    minimum (the catalog may declare more). Optional fields skip their assert
    when left at the default.
    """

    execution_mode: str
    async_mode: str
    writes_project: bool
    receipt_policy: str
    required_capabilities: tuple[str, ...]
    side_effects: frozenset[str]
    error_codes: frozenset[str]
    cost_policy_kind: str | None = None
    cost_requires_confirmation: bool | None = None
    input_schema_properties: tuple[str, ...] = ()
    output_schema_properties: tuple[str, ...] = ()
    description_contains: str | None = None


@dataclass(frozen=True)
class Gate:
    """One invalid/failing action variant.

    The gate action is always validated AND run: a validate-time gate asserts
    the validation error code, then still runs (a failed ActionResult with the
    same code, no writes); a run-time gate passes validation and fails during
    execution. ``no_writes=False`` skips the counts-unchanged assert for
    kinds whose failed runs persist a failed run/op/receipt trail.
    """

    gate_id: str
    make_action: Callable[[Seeded], dict[str, Any]]
    error_code: str
    field: str | None = None
    after_primary_run: bool = False
    prepare: Callable[[Project, Seeded], None] | None = None
    no_writes: bool = True
    expected_status: Literal["failed", "needs_confirmation"] = "failed"


@dataclass(frozen=True)
class Reservation:
    """Reserved-maprunner idempotency trio: in_progress / conflict / stale.

    ``make_conflict`` reuses the primary idempotency key with different
    params; ``go_stale`` invalidates the completed run's outputs (typically
    hides an output column) so replaying the primary spec is refused.
    """

    make_conflict: Callable[[Seeded], dict[str, Any]]
    go_stale: Callable[[Project, Seeded], None]
    # the error code a stale replay surfaces: "stale_replay" from the strict
    # checker; ops on the light (column-identity-only) checker surface their
    # own map_error_code instead.
    stale_code: str = "stale_replay"


@dataclass(frozen=True)
class UndoRerun:
    """operation.undo of the primary op, then optional rerun under a new key."""

    check_undone: Callable[[Project, Seeded, ActionResult], None]
    rerun_action: Callable[[Seeded], dict[str, Any]] | None = None
    check_rerun: (
        Callable[[Project, Seeded, ActionResult, ActionResult], None] | None
    ) = None


@dataclass(frozen=True)
class ExecutorCase:
    """One executor kind's shared-lifecycle contract.

    ``seed`` receives an open Project plus the test's tmp dir (file-backed
    sources need somewhere to live) and returns opaque state passed to every
    later callback. ``expect_counts`` maps table name -> row delta of one
    primary run; its keys are also the tables checked for no-write gates and
    replay stability.
    """

    kind: str
    catalog: CatalogEntry
    seed: Callable[[Project, Path], Seeded]
    make_action: Callable[[Seeded], dict[str, Any]]
    patch: Callable[[pytest.MonkeyPatch], dict[str, Any] | None] | None = None
    gates: tuple[Gate, ...] = ()
    expect_counts: dict[str, int] = field(default_factory=dict)
    check_state: Callable[[Project, Seeded, ActionResult], None] | None = None
    request_style: Literal["legacy", "typed"] = "legacy"
    replay_kind: Literal["idempotent", "conflict", "n/a"] = "idempotent"
    # Replay rebuilds outputs from the stored receipt, and a receipt ref may
    # not round-trip both identity dimensions. False disables that dimension's
    # replay-identity assert: names for kinds whose receipts store generic
    # output names (the source.* family stores "source" while the live run
    # names the output after the record); kinds for kinds whose receipt ref
    # kind differs from the live output kind (embedding.index_export stores
    # "export_artifact" while the live run emits "export").
    replay_output_names: bool = True
    replay_output_kinds: bool = True
    reservation: Reservation | None = None
    undo: UndoRerun | None = None
    case_id: str | None = None


def operation_action(kind: str, *, key: str, expected_op_id: int) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


@dataclass
class CaseEnv:
    case: ExecutorCase
    project: Project
    seeded: Seeded
    run_kwargs: dict[str, Any]
    project_id: str

    def run_once(self, action: dict[str, Any]) -> ActionResult:
        if self.case.request_style == "typed":
            from action_test_helpers import run_typed_map_request

            return run_typed_map_request(
                self.project,
                action,
                project_id=self.project_id,
                **self.run_kwargs,
            )
        from frisket.engine.executor import run_action_spec

        return run_action_spec(
            self.project,
            action,
            project_id=self.project_id,
            **self.run_kwargs,
        )

    def run(self, action: dict[str, Any]) -> ActionResult:
        """Complete the real two-step 402 protocol for happy-path actions.

        ``confirmed=true`` asks this harness client to confirm, not to bypass
        the gate: it submits once, copies the exact hash from the 402, then
        retries the unchanged action. Gate tests use ``confirmed=false`` (or
        ``run_once``) and continue to observe the first response directly.
        """
        if self.case.request_style == "typed":
            result = self.run_once(action)
            if result.status != "needs_confirmation" or not result.errors:
                return result
            context_hash = result.errors[0].details.get("promise_set_hash")
            if not isinstance(context_hash, str) or not context_hash:
                return result
            retry = copy.deepcopy(action)
            retry["confirmation"] = context_hash
            return self.run_once(retry)
        return run_action_with_confirmation(
            self.project,
            action,
            project_id=self.project_id,
            **self.run_kwargs,
        )

    def run_primary(self) -> ActionResult:
        return self.run(self.case.make_action(self.seeded))

    def counts(self) -> dict[str, int]:
        return table_counts(self.project, tuple(self.case.expect_counts))


def run_action_with_confirmation(
    project: Project,
    action: dict[str, Any],
    **run_kwargs: Any,
) -> ActionResult:
    """Act like a real v1 client: receive the quote, then echo its exact token."""

    from frisket.engine.executor import run_action_spec

    result = run_action_spec(project, action, **run_kwargs)
    if (
        result.status != "needs_confirmation"
        or (
            "action_id" not in action
            and action.get("params", {}).get("confirmed") is not True
        )
        or not result.errors
    ):
        return result
    context_hash = result.errors[0].details.get("promise_set_hash")
    if not isinstance(context_hash, str) or not context_hash:
        return result
    retry = copy.deepcopy(action)
    if "action_id" in retry:
        retry["confirmation"] = context_hash
    else:
        retry["params"]["consented_promise_set_hash"] = context_hash
    return run_action_spec(project, retry, **run_kwargs)


@contextmanager
def case_env(
    case: ExecutorCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CaseEnv]:
    project = Project.create(tmp_path / "harness.frisket", name=f"harness {case.kind}")
    try:
        seeded = case.seed(project, tmp_path)
        run_kwargs = dict(case.patch(monkeypatch) or {}) if case.patch else {}
        yield CaseEnv(
            case=case,
            project=project,
            seeded=seeded,
            run_kwargs=run_kwargs,
            project_id=f"project-{case.kind.replace('.', '-')}",
        )
    finally:
        project.close()


def _all_cases() -> list[ExecutorCase]:
    cases: list[ExecutorCase] = []
    for name in CASE_MODULES:
        cases.extend(import_module(name).CASES)
    return cases


def _case_id(case: ExecutorCase) -> str:
    return case.case_id or case.kind


def _catalog_rows() -> list[tuple[str, CatalogEntry]]:
    rows: dict[str, CatalogEntry] = {}
    for case in _all_cases():
        existing = rows.get(case.kind)
        assert existing is None or existing == case.catalog, (
            f"conflicting CatalogEntry rows for {case.kind}"
        )
        rows[case.kind] = case.catalog
    return sorted(rows.items())


_CASE_SELECTORS: dict[str, Callable[[ExecutorCase], bool]] = {
    "test_run_persists_and_counts": lambda case: True,
    "test_replay": lambda case: case.replay_kind != "n/a",
    "test_reservation_lifecycle": lambda case: case.reservation is not None,
    "test_undo_rerun_reuse": lambda case: case.undo is not None,
}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "gate" in metafunc.fixturenames:
        pairs = [(case, gate) for case in _all_cases() for gate in case.gates]
        metafunc.parametrize(
            ("case", "gate"),
            pairs,
            ids=[f"{_case_id(case)}-{gate.gate_id}" for case, gate in pairs],
        )
    elif "case" in metafunc.fixturenames:
        selector = _CASE_SELECTORS[metafunc.function.__name__]
        cases = [case for case in _all_cases() if selector(case)]
        metafunc.parametrize("case", cases, ids=[_case_id(c) for c in cases])
    elif "kind" in metafunc.fixturenames:
        rows = _catalog_rows()
        metafunc.parametrize(("kind", "expected"), rows, ids=[kind for kind, _ in rows])


def test_catalog_entry(kind: str, expected: CatalogEntry) -> None:
    from frisket.actions.system import root_action_catalog

    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == kind)
    assert entry.execution_mode == expected.execution_mode
    assert entry.async_mode == expected.async_mode
    assert entry.writes_project is expected.writes_project
    assert entry.receipt_policy == expected.receipt_policy
    assert list(entry.required_capabilities) == list(expected.required_capabilities)
    assert set(entry.side_effects) == set(expected.side_effects)
    assert {error.code for error in entry.errors} >= set(expected.error_codes)
    if expected.cost_policy_kind is not None:
        assert entry.cost_policy.kind == expected.cost_policy_kind
    if expected.cost_requires_confirmation is not None:
        assert (
            entry.cost_policy.requires_confirmation
            is expected.cost_requires_confirmation
        )
    for prop in expected.input_schema_properties:
        assert prop in entry.input_schema["properties"]
    for prop in expected.output_schema_properties:
        assert prop in entry.output_schema["properties"]
    if expected.description_contains is not None:
        assert expected.description_contains in entry.description


def test_validation_gates(
    case: ExecutorCase,
    gate: Gate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.contracts.action_validation import validate_action_spec

    with case_env(case, tmp_path, monkeypatch) as env:
        if gate.after_primary_run:
            primary = env.run_primary()
            assert primary.status == "completed", primary.errors
        if gate.prepare is not None:
            gate.prepare(env.project, env.seeded)
        action = gate.make_action(env.seeded)
        if case.request_style == "typed":
            from frisket.actions.system import validate_root_action

            validation = validate_root_action(action)
            if not validation.ok:
                assert validation.error is not None
                assert validation.error.code == gate.error_code
        else:
            validation = validate_action_spec(action)
            if not validation.ok:
                assert validation.error is not None
                assert validation.error.code == gate.error_code
                if gate.field is not None:
                    assert validation.error.field == gate.field
        before = env.counts()
        if case.request_style == "typed":
            from frisket.engine.executor import run_action_spec

            result = run_action_spec(
                env.project, action, project_id=env.project_id, **env.run_kwargs
            )
        else:
            result = env.run(action)
        assert result.status == gate.expected_status
        assert result.errors, gate.gate_id
        assert result.errors[0].code == gate.error_code
        if gate.no_writes:
            assert env.counts() == before


def test_run_persists_and_counts(
    case: ExecutorCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(case, tmp_path, monkeypatch) as env:
        before = env.counts()
        result = env.run_primary()
        assert result.status == "completed", result.errors
        assert result.schema_version == "frisket.action_result.v1"
        assert result.action.kind == case.kind
        assert result.action.action_id.startswith("act_")
        assert result.project_id == env.project_id
        if case.catalog.receipt_policy == "writes_receipt":
            assert result.receipt_id is not None
        assert env.counts() == {
            table: before[table] + delta for table, delta in case.expect_counts.items()
        }
        if case.check_state is not None:
            case.check_state(env.project, env.seeded, result)


def test_replay(
    case: ExecutorCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(case, tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        before = env.counts()
        replay = env.run_primary()
        if case.replay_kind == "idempotent":
            assert replay.status == "completed", replay.errors
            assert replay.receipt_id == first.receipt_id
            assert replay.run_id == first.run_id
            assert replay.op_ids == first.op_ids
            # Replay reconstructs outputs from the stored receipt; ref dicts
            # round-trip with re-sorted keys, so compare identity not bytes.
            if case.replay_output_names:
                assert sorted(o.name for o in replay.outputs) == sorted(
                    o.name for o in first.outputs
                )
            if case.replay_output_kinds:
                assert sorted(o.kind for o in replay.outputs) == sorted(
                    o.kind for o in first.outputs
                )
        else:
            assert replay.status == "failed"
            assert replay.errors[0].code == "idempotency_conflict"
        assert env.counts() == before


def _insert_running_reservation(env: CaseEnv, action: dict[str, Any]) -> str:
    """Simulate a concurrent in-flight run holding this action's idempotency
    reservation (a ``status='running'`` receipt with the same key and params
    hash), without racing a real second executor."""
    from frisket.contracts.action import (
        Receipt,
        ReceiptIO,
    )
    from frisket.contracts.action_validation import validate_action_spec
    from frisket.engine.executor import action_support as action_runtime_support

    if env.case.request_style == "typed":
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import typed_request_hash

        bound = typed_action_for_request(action)
        params_hash = typed_request_hash(bound)
        kind = bound.action.action_id
    else:
        validation = validate_action_spec(action)
        assert validation.ok and validation.action is not None
        params_hash = action_runtime_support._params_hash_without_confirmed(
            validation.action
        )
        kind = validation.action.kind
    kind_us = kind.replace(".", "_")
    receipt = Receipt(
        receipt_id=f"receipt_inflight_{kind_us}",
        project_id=env.project_id,
        action_id=f"act_inflight_{kind_us}",
        action_kind=kind,
        idempotency_key=str(action["idempotency_key"]),
        params_hash=params_hash,
        status="running",
        inputs=[
            ReceiptIO(
                name="idempotency",
                ref={
                    "kind": f"{kind_us}_idempotency_reservation",
                    "params_hash": params_hash,
                },
            )
        ],
    )
    env.project.db.execute(
        "INSERT INTO receipts (id, action_kind, action_id, "
        "idempotency_key, params_hash, status, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.receipt_id,
            receipt.action_kind,
            receipt.action_id,
            receipt.idempotency_key,
            receipt.params_hash,
            receipt.status,
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
        ),
    )
    env.project.db.commit()
    return receipt.receipt_id


def test_reservation_lifecycle(
    case: ExecutorCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert case.reservation is not None
    with case_env(case, tmp_path, monkeypatch) as env:
        action = case.make_action(env.seeded)

        reservation_id = _insert_running_reservation(env, action)
        before = env.counts()
        blocked = env.run(action)
        assert blocked.status == "failed"
        assert blocked.errors[0].code == "idempotency_in_progress"
        assert env.counts() == before

        env.project.db.execute("DELETE FROM receipts WHERE id=?", (reservation_id,))
        env.project.db.commit()
        completed = env.run(action)
        assert completed.status == "completed", completed.errors

        before = env.counts()
        conflict = env.run(case.reservation.make_conflict(env.seeded))
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert env.counts() == before

        case.reservation.go_stale(env.project, env.seeded)
        stale = env.run(action)
        assert stale.status == "failed"
        assert stale.errors[0].code == case.reservation.stale_code


def test_undo_rerun_reuse(
    case: ExecutorCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert case.undo is not None
    with case_env(case, tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        undo = env.run(
            operation_action(
                "operation.undo",
                key=f"operation_undo_{env.case.kind.replace('.', '_')}@sha256:harness",
                expected_op_id=first.op_ids[0],
            )
        )
        assert undo.status == "completed", undo.errors
        case.undo.check_undone(env.project, env.seeded, first)
        if case.undo.rerun_action is not None:
            second = env.run(case.undo.rerun_action(env.seeded))
            assert second.status == "completed", second.errors
            if case.undo.check_rerun is not None:
                case.undo.check_rerun(env.project, env.seeded, first, second)
