"""Behavioral echo-gate closure: a wrong confirmed-retry hash always refuses.

The declaration-side closure test
(``tests/engine/test_cost_policy_confirmation_invariant.py``) proves every
cost-confirming kind DECLARES the ``confirmed`` +
``consented_promise_set_hash`` pair. It cannot see whether the family ever
runs the comparison — a new family could declare the fields, forget the
comparison, and stay green while ``confirmed=true`` plus any stale hash
bought the run. The comparison itself now lives in ONE helper
(``frisket.engine.runner.confirmation_echo.refuse_unless_exact_echo``); this
test proves the helper is actually wired into every family:

For EVERY rostered action kind, dispatching with ``confirmed=true`` and a
WRONG ``consented_promise_set_hash`` must come back ``needs_confirmation``
(the 402 envelope) with zero writes — the wrong echo must never execute.

Closure mechanics (how a new kind goes red here, without anyone enumerating):
  * The roster is derived from the registry, never hand-listed: a kind is
    rostered when its catalog cost policy ``requires_confirmation``, when its
    cost kind is metered at all (``kind != "none"`` — runtime-only gates like
    run.backfill's), or when it declares a ``*_requires_confirmation`` gate
    error (the deterministic fanout/cap gates, catalog cost "none").
  * Drivers are derived from the executor harness (``executor_harness``
    CASES) — the same seed/action/patch every kind already maintains — with
    a small bespoke-driver table for the kinds that have no harness case or
    whose gate the harness action cannot arm. A new rostered kind with
    neither fails by name.
  * Each driver must first prove its gate is REACHABLE (an unconfirmed
    dispatch 402s and mints a quote hash). A driver whose environment cannot
    arm the gate fails "gate never armed" instead of passing vacuously; the
    ``_ARM`` table holds the per-kind tweak that makes the run paid (remote
    engine / unpriced model / remote embedder) for kinds whose harness
    default is free-local.
  * Rostered kinds with NO echo protocol at all are named in
    ``_EXCLUSIONS``, each with a mechanical guard that expires the exclusion:
    if the kind ever grows the declared pair (or its gate module ever starts
    reading the echo), the guard reds and a driver becomes mandatory.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    install_pricing_policy,
)


class _PaidClosurePolicy:
    policy_id = "test.echo-closure.paid.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=5_000_000,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.fixture(autouse=True)
def _pin_zero_standing_preapproval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Challenge positive quotes; free routes need an explicit paid fixture."""

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


# Valid-format-but-wrong: matches the unified bare-hex spelling, so a
# refusal proves the CONTENT comparison ran, not just a format rejection.
WRONG_HASH = "0" * 64

# The ONE quote-hash spelling: bare-hex sha256, matching the routed
# promise-set content hash (frisket.promise_set.v1 — corpus-pinned bare hex,
# HASH_FAMILIES output "hex"). A family minting "sha256:"-prefixed (or any
# other) spelling reds here by name.
_QUOTE_HASH_SPELLING = re.compile(r"[0-9a-f]{64}")

# The COMPLETE set of quote-hash mints in this codebase. Consent is minted in
# exactly two constructions and a family may use no other:
#
#   * the confirmation ENVELOPE (money gates and scope gates alike, every
#     executor architecture) — engine/runner/confirmation_context.py;
#   * the routed promise-set content hash, for the claims machinery —
#     recorded at ``promise_rows_hash``, which is THE construction both
#     ``promise_set_hash`` and ``consented_set_hash`` delegate to, so the
#     recorder cannot miss a caller by picking the wrong wrapper.
#
# The behavioral closure below records what these two return during each
# kind's unconfirmed dispatch and requires the 402's promise_set_hash to be
# one of those values. A family that hand-rolls its own hash — the state this
# codebase was in before the mints were consolidated, with three private
# payload shapes — offers a hash neither recorder ever produced and reds by
# name. Adding a THIRD legitimate mint is a deliberate two-line edit here,
# not something a new family can do by accident.
_MINTS: tuple[tuple[str, str], ...] = (
    ("frisket.engine.runner.confirmation_context", "mint_confirmation_hash"),
    ("frisket.execution.promises", "promise_rows_hash"),
)


@contextmanager
def _recording_mints(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every quote hash the legitimate mints return.

    Patched at the DEFINING module and at every module holding a reference to
    the function — found by scanning each module's namespace for values that
    ARE the original object, never a hand-listed set of importers, so a family
    added tomorrow is covered the moment it imports one.

    Matching on the object rather than on the canonical NAME is the point: an
    aliasing import (``from ... import mint_confirmation_hash as _mint``)
    binds the same function under a different attribute, and a name-only scan
    would walk straight past it. That direction is conservative — an unpatched
    alias records nothing and FAILS the closure rather than passing a private
    mint — but a fence that reds for the wrong reason teaches the next reader
    the wrong lesson.

    Function-local imports (the runner and the attempt authority both use
    them) re-read the defining module's attribute at call time and are covered
    by the patch on the defining module.
    """
    import sys

    def module_bindings():
        for module in list(sys.modules.values()):
            if module is None:
                continue
            try:
                namespace = list(vars(module).items())
            except Exception:  # noqa: BLE001 — lazy/exotic module objects
                continue
            for name, bound in namespace:
                yield module, name, bound

    minted: list[str] = []
    recorders: list[tuple[Any, Any]] = []
    try:
        with monkeypatch.context() as patches:
            for module_name, attribute in _MINTS:
                original = getattr(import_module(module_name), attribute)

                def _record(
                    *args: Any, _original: Any = original, **kwargs: Any
                ) -> Any:
                    result = _original(*args, **kwargs)
                    minted.append(result)
                    return result

                recorders.append((_record, original))
                for module, name, bound in module_bindings():
                    if bound is original:
                        patches.setattr(module, name, _record, raising=False)
            yield minted
    finally:
        # A lazy import can retain the patched function after the scoped undo.
        # Restore only our recorder objects, including newly imported aliases.
        for module, name, bound in module_bindings():
            for recorder, original in recorders:
                if bound is recorder:
                    setattr(module, name, original)


def test_mint_recorder_restores_alias_imported_during_recording(monkeypatch):
    import sys
    from types import ModuleType

    from frisket.engine.runner import confirmation_context

    original = confirmation_context.mint_confirmation_hash
    context = confirmation_context.scope_confirmation(
        family_kind="test.recorder",
        scope=confirmation_context.ActionScope(action_hash="test"),
        bindings={},
    )
    late_module = ModuleType("test_late_mint_alias")
    with _recording_mints(monkeypatch) as first:
        late_module.alias = confirmation_context.mint_confirmation_hash
        monkeypatch.setitem(sys.modules, late_module.__name__, late_module)
        token = late_module.alias(context)
    assert first == [token]
    assert late_module.alias is original
    with _recording_mints(monkeypatch) as second:
        assert late_module.alias(context) == token
    assert second == [token]
    assert first == [token]
    assert late_module.alias is original


# One fixed cross-family write surface: any executed action of these kinds
# lands in at least one of these tables, so "no writes" is checked the same
# way for every family. effect_checkpoints is included so a wrong-hash
# dispatch whose only durable trace was a paid-effect checkpoint cannot
# evade the assertion.
_COUNT_TABLES = (
    "sheets",
    "columns",
    "rows",
    "cells",
    "ops",
    "runs",
    "results",
    "model_calls",
    "receipts",
    "blobs",
    "effect_checkpoints",
)


def _rostered_kinds() -> set[str]:
    """Every kind that could carry a confirmation gate, derived mechanically."""
    from frisket.actions.system import root_action_catalog

    kinds: set[str] = set()
    for catalog in root_action_catalog().actions:
        policy = catalog.cost_policy
        declares_gate_error = any(
            error.code.endswith("_requires_confirmation") for error in catalog.errors
        )
        if policy.requires_confirmation or policy.kind != "none" or declares_gate_error:
            kinds.add(catalog.kind)
    return kinds


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _COUNT_TABLES
    }


# ---------------------------------------------------------------------------
# Named exclusions: rostered kinds with no echo protocol to get wrong.
# Each exclusion carries a mechanical guard (test_exclusion_guard_still_holds)
# that goes red — forcing a driver — the moment the kind grows one.
# ---------------------------------------------------------------------------

_NO_PAIR_EXCLUSIONS = {
    # Typed embedding lifecycle admission uses durable provider policy (remote
    # opt-in and automatic-refresh ceilings), not an invocation echo protocol.
    "embedding.index_create",
    "embedding.index_refresh",
    "media.enclosure_materialize",
    "source.poll",
    # Typed transcript repetition is semantic Params intent, not paid consent.
    # The historical error suffix still makes the mechanical roster include it.
    "derive.transcript_segments",
    "derive.temporal_segments",
    "temporal.extract_range",
}

_EXCLUDED_KINDS = _NO_PAIR_EXCLUSIONS


# ---------------------------------------------------------------------------
# Harness-derived drivers: reuse each kind's maintained ExecutorCase.
# ---------------------------------------------------------------------------


def _harness_cases() -> dict[str, Any]:
    import executor_harness as eh

    cases: dict[str, Any] = {}
    for case in eh._all_cases():
        cases.setdefault(case.kind, case)
    return cases


def _arm_cluster_values(
    action: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    # The harness primary is the free deterministic fingerprint method; the
    # confirmation gate exists for the remote-embedding semantic path.
    from tests.engine.test_cluster_values_executor import _remote_embedding_router

    assert action["action_id"] == "cluster.values"
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    action["params"]["method"] = "semantic"
    router, _adapter = _remote_embedding_router()
    return {"router": router}


def _arm_map_ner(
    action: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    # gliner/spacy are free-local; the llm engine rides the shared model-cost
    # gate (remote model => paid).
    del monkeypatch
    assert action["action_id"] == "map.ner"
    action["params"]["engine"] = "llm"
    action["params"]["model"] = "anthropic/claude-haiku-4-5"
    return {}


def _arm_reduce_group_summary(
    action: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    # The harness model is priced under the consent threshold; an unpriced
    # model makes the estimate unknown, which always gates.
    del monkeypatch
    action["params"]["model"] = "unknown/frontier"
    return {}


def _arm_hosted_engine(
    action: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    # rapidocr/markitdown are free-local; datalab is the hosted metered engine.
    action["params"]["engine"] = "datalab"
    action["params"].pop("language", None)
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-confirmation-key")
    return {}


# kind -> (mutate the challenge action to make the run paid, extra run kwargs).
# Only for kinds whose harness default is free-local; every other kind's
# harness action gates as-is.
_ARM: dict[str, Callable[[dict[str, Any], pytest.MonkeyPatch], dict[str, Any]]] = {
    "cluster.values": _arm_cluster_values,
    "map.ner": _arm_map_ner,
    "reduce.group_summary": _arm_reduce_group_summary,
    "media.ocr": _arm_hosted_engine,
    "media.to_markdown": _arm_hosted_engine,
}

Driver = Callable[
    [Path, pytest.MonkeyPatch],
    Any,  # context manager yielding (project, challenge_action, run)
]


@contextmanager
def _harness_driver(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    import executor_harness as eh
    from frisket.engine.executor import run_action_spec

    case = _harness_cases()[kind]
    with eh.case_env(case, tmp_path, monkeypatch) as env:
        confirm_gates = [
            gate for gate in case.gates if gate.expected_status == "needs_confirmation"
        ]
        action = copy.deepcopy(
            confirm_gates[0].make_action(env.seeded)
            if confirm_gates
            else case.make_action(env.seeded)
        )
        extra = _ARM.get(kind, lambda _action, _mp: {})(action, monkeypatch)
        run_kwargs = {**env.run_kwargs, **extra}

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(
                env.project,
                spec,
                project_id=env.project_id,
                **run_kwargs,
            )

        yield env.project, action, run


# ---------------------------------------------------------------------------
# Bespoke drivers: rostered kinds with no executor-harness case, or whose
# harness case cannot arm the gate.
# ---------------------------------------------------------------------------


@contextmanager
def _drive_map_api_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor import http_request

    async def never_called(*_args, **_kwargs):
        pytest.fail("wrong-echo closure reached the HTTP provider")

    monkeypatch.setattr(http_request, "safe_request", never_called)

    project = Project.create(tmp_path / "api-call.frisket", name="api-call closure")
    try:
        sheet_id = project.add_sheet("Data")
        columns = {"q": project.add_column(sheet_id, "q", type="text")}
        project.add_rows(sheet_id, [{"q": "alpha"}], columns)
        action = ActionRequest(
            action_id="map.api_call",
            scope={"kind": "sheet_rows", "sheet_id": sheet_id},
            params={
                "request": {"url": "https://api.test.example/items?q={{ q }}"},
            },
            idempotency_key="map_api_call@sha256:echo-closure",
        ).model_dump(mode="json", exclude_none=True)

        def run(spec: dict[str, Any]) -> Any:
            result = run_action_spec(project, spec, project_id="p-echo-closure")
            if result.status == "needs_confirmation":
                estimate = result.errors[0].details["estimate"]
                assert estimate["cost"] is None
                assert estimate["cost_source"] == "unknown"
            return result

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_map_mcp_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    del monkeypatch
    from frisket.engine.executor import run_action_spec

    project = Project.create(
        tmp_path / "mcp-extract.frisket", name="MCP extract closure"
    )
    try:
        sheet_id = project.add_sheet("Data")
        column_id = project.add_column(sheet_id, "company", type="text")
        project.add_rows(sheet_id, [{"company": "Acme"}], {"company": column_id})
        action = {
            "action_id": "map.mcp_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["company"],
                "model": "unknown/frontier",
                "instruction": "Resolve the company.",
                "fields": [{"name": "company_name", "type": "text"}],
                "mcp_server_ids": ["local-crm"],
            },
            "idempotency_key": "mcp_extract@echo-closure",
        }

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(project, spec, project_id="p-echo-closure")

        yield project, action, run
    finally:
        project.close()


def _model_rows_driver(kind: str) -> Driver:
    @contextmanager
    def drive(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
        del monkeypatch
        from action_test_helpers import run_typed_map_request

        project = Project.create(
            tmp_path / f"{kind.replace('.', '-')}.frisket",
            name=f"{kind} closure",
        )
        try:
            sheet_id = project.add_sheet("Data")
            column_id = project.add_column(sheet_id, "body", type="text")
            project.add_rows(
                sheet_id, [{"body": "Ada filed the report."}], {"body": column_id}
            )
            params: dict[str, Any] = {
                "source": ["body"],
                "model": "anthropic/claude-haiku-4-5",
            }
            logical_output: str | None = "summary"
            if kind == "map.ask":
                params["question"] = "Who filed the report?"
                logical_output = "answer"
            elif kind == "map.extract":
                # Extract's outputs are its declared fields; there is no
                # single logical output to name.
                params["instruction"] = "Who filed the report?"
                params["fields"] = [{"name": "person", "type": "text"}]
                logical_output = None
            elif kind == "map.judge":
                seeded = run_typed_map_request(
                    project,
                    {
                        "action_id": "map.template",
                        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                        "params": {"template": {"text": "{{body}}"}},
                        "output_names": {"rendered": "answer"},
                        "idempotency_key": "judge-subject-seed",
                    },
                    project_id="p-echo-closure",
                )
                assert seeded.status == "completed"
                # Use a real generated head, marked as an AI answer for this
                # admission probe, without buying a provider call to seed it.
                project.db.execute(
                    "UPDATE columns SET ai_generated=1 WHERE sheet_id=? AND name='answer'",
                    (sheet_id,),
                )
                project.db.commit()
                params.update(judged_column="answer", guidelines="Be accurate.")
                logical_output = "verdict"
            action = {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": params,
                "output_names": (
                    {} if logical_output is None else {logical_output: logical_output}
                ),
                "idempotency_key": f"{kind}@echo-closure",
            }

            def run(spec: dict[str, Any]) -> Any:
                return run_typed_map_request(project, spec, project_id="p-echo-closure")

            yield project, action, run
        finally:
            project.close()

    return drive


@contextmanager
def _drive_file_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, kind: str = "media.fetch_url"
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    del monkeypatch
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "fetch-url.frisket", name="fetch-url closure")
    try:
        sheet_id = project.add_sheet("Feed")
        columns = {"url": project.add_column(sheet_id, "url", type="link")}
        project.add_rows(sheet_id, [{"url": "https://cdn.example/a.mp3"}], columns)
        action = {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "url"},
            "output_names": {
                "video" if kind == "media.ytdlp_download" else "media": "media"
            },
            "idempotency_key": f"{kind}@sha256:echo-closure",
        }

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(project, spec, project_id="p-echo-closure")

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_map_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    del monkeypatch
    from frisket.contracts.action import ActionError
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.action_support import _failed_result
    from frisket.engine.executor.find_action import prepare_typed_find_admission

    project = Project.create(tmp_path / "map-find.frisket", name="find closure")
    try:
        sheet_id = project.add_sheet("Sources")
        columns = {"body": project.add_column(sheet_id, "body", type="text")}
        project.add_rows(sheet_id, [{"body": "China trade policy."}], columns)
        action = {
            "action_id": "map.find",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "sheet_name": "Findings",
            "params": {
                "source": "body",
                "instruction": "Find every discussion of China.",
                "fields": [],
                "model": "unknown/frontier",
            },
            "idempotency_key": "map_find@sha256:echo-closure",
        }

        def run(spec: dict[str, Any]) -> Any:
            outcome = prepare_typed_find_admission(
                project, typed_action_for_request(spec)
            )
            if isinstance(outcome, ActionError):
                return _failed_result(
                    project_id="p-echo-closure",
                    action_kind="map.find",
                    error=outcome,
                )
            raise AssertionError("confirmation closure passed the Find gate")

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_sheet_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    # Supported refreshes are deterministic and never price historical model
    # work; the ONE refresh gate is the join fan-out cap, re-checked against
    # the CURRENT parents. Materialize a real derived join inside its cap,
    # then grow a parent so the fresh projection exceeds it — the refresh
    # must re-confirm (join_fanout_requires_confirmation, a scope quote with
    # no model estimate), exactly as tests/engine/test_multi_parent_sheet_refresh.py
    # drives it.
    del monkeypatch
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "refresh.frisket", name="refresh closure")
    try:
        left_id = project.add_sheet("L")
        left_cols = {"k": project.add_column(left_id, "k", type="integer")}
        project.add_rows(left_id, [{"k": 1}, {"k": 1}], left_cols)
        right_id = project.add_sheet("R")
        right_cols = {"k": project.add_column(right_id, "k", type="integer")}
        project.add_rows(right_id, [{"k": 1}, {"k": 1}], right_cols)
        project.db.commit()

        # Original run: 2x2 = 4 fan-out rows, cap 4 -> fits (no confirmation).
        join = run_action_spec(
            project,
            {
                "action_id": "derive.join",
                "scope": {"kind": "sheet_rows", "sheet_id": left_id},
                "sheet_name": "Fan",
                "params": {
                    "right": {"sheet_id": right_id},
                    "join_keys": [{"left_column": "k", "right_column": "k"}],
                    "how": "inner",
                    "max_output_rows": 4,
                },
                "idempotency_key": "derive_join@sha256:echo-closure-seed",
            },
            project_id="p-echo-closure",
        )
        assert join.status == "completed", [e.model_dump() for e in join.errors]
        child_id = int(
            project.db.execute(
                "SELECT id FROM sheets WHERE name='Fan' AND hidden=0"
            ).fetchone()["id"]
        )
        assert len(project.visible_row_ids(child_id)) == 4

        # Grow the right side: now 2x3 = 6 > cap 4, so the refresh re-gates.
        project.add_rows(right_id, [{"k": 1}], right_cols)
        project.db.commit()
        action = {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": child_id},
            "idempotency_key": "sheet_refresh@sha256:echo-closure",
        }

        def run(spec: dict[str, Any]) -> Any:
            result = run_action_spec(project, spec, project_id="p-echo-closure")
            if result.status == "needs_confirmation":
                # The refresh gate IS the fan-out cap: a scope quote carrying
                # the projected/allowed row counts and no model estimate.
                error = result.errors[0]
                assert error.code == "join_fanout_requires_confirmation"
                details = error.details or {}
                assert "estimate" not in details
                assert details["estimated_rows"] == 6
                assert details["max_output_rows"] == 4
            return result

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_derive_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    # The harness primary never fans out; reuse the proven fanout seed from
    # the envelope test so the deterministic cap gate arms.
    del monkeypatch
    from test_action_confirmation_envelope import _join_action, _seed_fanout

    from frisket.engine.executor import run_action_spec

    seed = _seed_fanout(tmp_path / "join.frisket")
    project = Project(tmp_path / "join.frisket")
    try:
        action = _join_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            target_sheet_name="Guarded",
            max_output_rows=5,
            idempotency_key="derive_join@sha256:echo-closure",
        )

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(project, spec, project_id="p-echo-closure")

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_derive_collection_expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    from test_derive_collection_expand_executor import (
        _expand_action,
        _fake_channel_provider,
        _seed_source_cell,
    )

    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor import collection_read as family

    # preview_count 150 exceeds default_cap (100): the cap gate arms.
    monkeypatch.setattr(
        family, "_ENUMERATOR_PROVIDER", _fake_channel_provider(150, 150)
    )
    seed = _seed_source_cell(tmp_path / "expand.frisket")
    project = Project(tmp_path / "expand.frisket")
    try:
        action = _expand_action(
            source_sheet_id=seed["sheet_id"],
            source_column_id=seed["column_id"],
            source_row_id=seed["row_id"],
            target_sheet_name="Gated",
            idempotency_key="collection_expand@sha256:echo-closure",
        )

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(project, spec, project_id="p-echo-closure")

        yield project, action, run
    finally:
        project.close()


@contextmanager
def _drive_run_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    """run.backfill's echo gates are the resolution-aware backfill branches of
    engine/runner/validation.py; crib the proven changed-scope flow
    from tests/server/test_run_backfill_route_verification.py: complete an
    original routed run, add a row with new egress scope, and the backfill
    must re-consent."""
    from frisket.contracts.action import ActionResult

    mod = import_module("tests.server.test_run_backfill_route_verification")
    for key, value in {
        "FRISKET_MODELS_URL": "http://models.test",
        "FRISKET_MODELS_TOKEN": "secret",
    }.items():
        monkeypatch.setenv(key, value)

    async def fake_run_engine(
        engine, path, spec, ctx, *, should_cancel=None, transport=None, media=None
    ):
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "stubbed",
                "segments": [],
                "language": None,
                "cost": 0.0,
            },
            model_calls=(),
        )

    from frisket.sdk.ops import transcribe_engines

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_run_engine)
    client = mod._client(tmp_path)
    pid, project, sheet_id = mod._seed_audio_project(client)
    mod._run_original_to_completion(client, pid, project, mod._gateway_spec(sheet_id))
    mod._add_audio_row(project, sheet_id, duration_seconds=40.0)
    action = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "transcript"},
        "idempotency_key": "run_backfill@sha256:echo-closure",
    }

    def run(spec: dict[str, Any]) -> Any:
        response = client.post(f"/api/projects/{pid}/actions/v1/run", json=spec)
        return ActionResult.model_validate(response.json())

    yield project, action, run


@contextmanager
def _drive_export_google_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Project, dict[str, Any], Callable[..., Any]]]:
    del monkeypatch
    from frisket.engine.executor import ExecutorDeps, run_action_spec

    class _NeverCalledGoogleClient:
        def export_tabs(self, **_kwargs: Any) -> Any:
            raise AssertionError("confirmation closure reached the provider")

    project = Project.create(
        tmp_path / "google-sheets.frisket", name="google sheets closure"
    )
    try:
        sheet_id = project.add_sheet("Data")
        column_id = project.add_column(sheet_id, "name", type="text")
        project.add_rows(sheet_id, [{"name": "Ada"}], {"name": column_id})
        action = {
            "action_id": "export.google_sheets",
            "scope": {"kind": "project"},
            "output_names": {},
            "params": {
                "connection_id": "google-closure",
                "source": {"kind": "current_sheet", "sheet_id": sheet_id},
                "destination": {
                    "kind": "google_sheets",
                    "mode": "new_spreadsheet",
                    "spreadsheet_title": "Closure",
                },
                "write_policy": "replace_managed_tabs",
            },
            "idempotency_key": "export_google_sheets@sha256:echo-closure",
        }
        deps = ExecutorDeps(
            google_sheets_client=_NeverCalledGoogleClient(),
            connected_account_resolver=lambda provider, connection_id: (
                {"id": connection_id, "provider": provider}
                if (provider, connection_id) == ("google", "google-closure")
                else None
            ),
        )

        def run(spec: dict[str, Any]) -> Any:
            return run_action_spec(
                project,
                spec,
                project_id="p-echo-closure",
                deps=deps,
            )

        yield project, action, run
    finally:
        project.close()


def _geospatial_driver(kind: str) -> Driver:
    @contextmanager
    def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import httpx
        from frisket.ai.llm import ModelRouter
        from frisket.engine.executor import run_action_spec

        monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
        monkeypatch.setenv("CENSUS_API_KEY", "test-census-key")

        def never_called(_request):
            pytest.fail("wrong-echo closure reached the external provider")

        router = ModelRouter(cache=None, cache_mode="off")
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(never_called))
        project = Project.create(tmp_path / "geospatial.frisket")
        try:
            sheet_id = project.add_sheet("Places")
            census = kind == "enrich.census_demographics"
            column_id = project.add_column(
                sheet_id, "source", type="geo_point" if census else "text"
            )
            project.add_rows(
                sheet_id,
                [{"source": {"lat": 38.9, "lon": -77.03} if census else "Tokyo"}],
                {"source": column_id},
            )
            action = {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": "source",
                    **({} if census else {"engine": "opencage"}),
                },
                "idempotency_key": f"{kind}@echo-closure",
            }

            def run(spec):
                return run_action_spec(
                    project, spec, project_id="p-echo-closure", router=router
                )

            yield project, action, run
        finally:
            project.close()

    return drive


_BESPOKE_DRIVERS: dict[str, Driver] = {
    "enrich.geocode": _geospatial_driver("enrich.geocode"),
    "enrich.census_demographics": _geospatial_driver("enrich.census_demographics"),
    "map.ask": _model_rows_driver("map.ask"),
    # The map.extract executor-harness case still authors the retired v2
    # envelope; drive the typed request directly until that case is ported.
    "map.extract": _model_rows_driver("map.extract"),
    "map.summarize": _model_rows_driver("map.summarize"),
    "map.judge": _model_rows_driver("map.judge"),
    "map.api_call": _drive_map_api_call,
    "map.mcp_extract": _drive_map_mcp_extract,
    "media.fetch_url": _drive_file_acquisition,
    "media.ytdlp_download": lambda path, patch: _drive_file_acquisition(
        path, patch, kind="media.ytdlp_download"
    ),
    "map.find": _drive_map_find,
    "sheet.refresh": _drive_sheet_refresh,
    "derive.join": _drive_derive_join,
    "derive.collection_expand": _drive_derive_collection_expand,
    "export.google_sheets": _drive_export_google_sheets,
    "run.backfill": _drive_run_backfill,
}


def _driver(kind: str) -> Driver | None:
    if kind in _BESPOKE_DRIVERS:
        return _BESPOKE_DRIVERS[kind]
    if kind in _harness_cases():
        return lambda tmp_path, monkeypatch: _harness_driver(
            kind, tmp_path, monkeypatch
        )
    return None


# ---------------------------------------------------------------------------
# The closure
# ---------------------------------------------------------------------------


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "kind" in metafunc.fixturenames:
        metafunc.parametrize("kind", sorted(_rostered_kinds() - _EXCLUDED_KINDS))
    elif "excluded_kind" in metafunc.fixturenames:
        metafunc.parametrize("excluded_kind", sorted(_EXCLUDED_KINDS))


def test_every_rostered_kind_has_a_driver_or_named_exclusion() -> None:
    kinds = _rostered_kinds()
    # Roster sanity: the registry sweep is real, not a vacuous pass over zero.
    assert len(kinds) >= 29, sorted(kinds)
    stale_exclusions = sorted(_EXCLUDED_KINDS - kinds)
    assert not stale_exclusions, (
        f"exclusions for kinds no longer rostered (delete them): {stale_exclusions}"
    )
    overlap = sorted(_EXCLUDED_KINDS & set(_BESPOKE_DRIVERS))
    assert not overlap, f"kinds both driven and excluded: {overlap}"
    missing = sorted(kind for kind in kinds - _EXCLUDED_KINDS if _driver(kind) is None)
    assert not missing, (
        "rostered kinds with no echo-gate closure driver (add an "
        "executor-harness case, a bespoke driver, or — only for a kind with "
        f"no echo protocol — a guarded exclusion): {missing}"
    )


def test_exclusion_guard_still_holds(excluded_kind: str) -> None:
    """Each exclusion expires mechanically the moment its reason stops being
    true — growing an echo protocol makes this red and a driver mandatory."""
    from frisket.actions.system import root_action_catalog

    catalog = next(
        entry for entry in root_action_catalog().actions if entry.kind == excluded_kind
    )
    # No confirmed/echo pair declared: there is no echo protocol to get
    # wrong (the params schema would reject the fields outright).
    properties = set(catalog.input_schema.get("properties") or {})
    assert not catalog.cost_policy.requires_confirmation, (
        f"{excluded_kind} now requires confirmation — remove it "
        "from _NO_PAIR_EXCLUSIONS and add a wrong-echo driver"
    )
    assert not ({"confirmed", "consented_promise_set_hash"} & properties), (
        f"{excluded_kind} now declares a confirmation shape — remove it "
        "from _NO_PAIR_EXCLUSIONS and add a wrong-echo driver"
    )


def test_wrong_echo_refuses_without_executing(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(kind)
    if driver is None:
        pytest.fail(f"{kind} is rostered but has no echo-gate closure driver")
    if kind in {"enrich.census_demographics", "run.backfill"}:
        # Free default routes need a real tariff to exercise explicit consent.
        # Keep this local: other drivers seed work under their normal pricing.
        install_pricing_policy(_PaidClosurePolicy())
    with driver(tmp_path, monkeypatch) as (project, action, run):
        # 1. Arm proof: an unconfirmed dispatch must reach the gate and mint
        #    the quote hash — otherwise the wrong-hash assertion below would
        #    be vacuous for this kind.
        challenge = copy.deepcopy(action)
        if "action_id" in challenge:
            challenge.pop("confirmation", None)
        else:
            challenge["params"]["confirmed"] = False
            challenge["params"].pop("consented_promise_set_hash", None)
        with _recording_mints(monkeypatch) as minted:
            gated = run(challenge)
        assert gated.status == "needs_confirmation", (
            f"{kind}: gate never armed (status={gated.status}, "
            f"errors={[e.code for e in (gated.errors or [])]}) — fix this "
            "kind's driver so the closure test actually reaches its gate"
        )
        quote = (gated.errors[0].details or {}).get("promise_set_hash")
        assert isinstance(quote, str) and _QUOTE_HASH_SPELLING.fullmatch(quote), (
            f"{kind}: quote hash {quote!r} does not use the ONE bare-hex "
            "sha256 spelling every family mints on the 402 envelope"
        )
        assert quote != WRONG_HASH, f"{kind}: probe hash collides with WRONG_HASH"

        # 1b. Mint closure: the hash this kind OFFERS must be one the shared
        #     mints produced. The spelling assertion above only proves the
        #     token looks like a sha256; this proves it came from the one
        #     confirmation envelope (or the routed promise set), so no family
        #     can go back to hashing a private payload of its own.
        assert minted, (
            f"{kind}: its 402 minted no quote hash through any shared mint — "
            f"every confirmation is minted by one of {[name for _m, name in _MINTS]}"
        )
        assert quote in minted, (
            f"{kind}: the promise_set_hash on its 402 envelope was not "
            "produced by a shared mint, so this family is hashing a payload "
            "of its own — mint it through "
            "engine/runner/confirmation_context.mint_confirmation_hash"
        )

        # 2. The defect under test: confirmed=true with a WRONG echo must
        #    refuse without executing.
        wrong = copy.deepcopy(action)
        if "action_id" in wrong:
            wrong["confirmation"] = WRONG_HASH
        else:
            wrong["params"]["confirmed"] = True
            wrong["params"]["consented_promise_set_hash"] = WRONG_HASH
        before = _counts(project)
        refused = run(wrong)
        assert refused.status == "needs_confirmation", (
            f"{kind}: confirmed=true with a WRONG consented_promise_set_hash "
            f"did not refuse (status={refused.status}) — the family's echo "
            "comparison is missing or bypassed"
        )
        assert refused.errors, kind
        assert _counts(project) == before, (
            f"{kind}: the wrong-echo dispatch wrote to the project"
        )
        # The refusal re-offers the real quote, never the wrong echo. The
        # arm probe proved this kind's envelope carries the hash, so its
        # wrong-echo refusal must carry it too (two surfaces, one answer).
        details = refused.errors[0].details or {}
        re_offered = details.get("promise_set_hash")
        assert isinstance(re_offered, str) and _QUOTE_HASH_SPELLING.fullmatch(
            re_offered
        ), (
            f"{kind}: wrong-echo refusal dropped (or mis-spelled) the "
            "promise_set_hash its unconfirmed challenge minted"
        )
        assert re_offered != WRONG_HASH, kind
