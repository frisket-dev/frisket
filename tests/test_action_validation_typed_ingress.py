"""WARM-01's typed-ingress proof (program-spec.md WARM-01 "prove first").

Both registered entry paths — `validate_action_spec` and
`payload_only_action_refusal` — construct and validate `ActionSpec` through
pydantic before any precheck runs. A non-dict `params` (None, a list, or a
scalar, including `bool`) can therefore never reach a cohort precheck: the
envelope refuses first, as `invalid_action_spec` on the full-validation path
and as `None` on the payload-only path.

No first-party kind is registered on the legacy `validate_action_spec` path
any more: every kind is typed and its unknown-key refusal is proven at the
typed `validate_root_action` boundary. The retired `research.project_answer`
(the last legacy kind; its agentic replacement is tracked in #640) is the
control for "a request naming an unknown kind is refused on both paths
without reserving, writing, or calling a model".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.contracts import action_validation as av
from frisket.contracts.action import ACTION_SCHEMA_VERSION

RETIRED_KIND = "research.project_answer"

# Representative kinds: a plain full-validation kind, a mutation kind, an
# ingest kind, and a programmatic-map kind. The envelope refuses a non-dict
# `params` before any kind lookup, so typed-only kinds belong here too.
KINDS = ["plugin.load", "cell.edit", "map.python", "map.ner"]

# None, a list, a scalar, and bool (a scalar too, but bool's isinstance
# quirks with int make it worth calling out explicitly).
BAD_PARAMS: list[Any] = [None, [], [1, 2], "x", 3, 1.5, True]

_WRITE_TABLES = ("runs", "ops", "receipts", "model_calls", "sheets", "rows")


def _table_counts(project: Any) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in _WRITE_TABLES
    }


@pytest.mark.parametrize("bad_params", BAD_PARAMS)
def test_typed_csv_refuses_non_dict_params_before_file_or_project_work(
    tmp_path: Path, bad_params: Any
) -> None:
    from frisket.actions.system import validate_root_action
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    request = {
        "action_id": "import.csv",
        "scope": {"kind": "project"},
        "sheet_name": "Invalid CSV",
        "params": bad_params,
        "idempotency_key": "csv-invalid-params",
    }
    validation = validate_root_action(request)
    assert validation.ok is False
    assert validation.error.code == "invalid_action_request"
    project = Project.create(tmp_path / "invalid-csv.frisket", name="Invalid CSV")
    try:
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
    finally:
        project.close()


def _envelope(kind: str, params: Any) -> dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "kind": kind,
        "params": params,
    }


def _typed_ner_request() -> dict[str, Any]:
    return {
        "action_id": "map.ner",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"source": ["body"], "labels": ["person"], "engine": "gliner"},
        "output_names": {"entities": "entities"},
        "idempotency_key": "typed-ingress-control",
    }


def _retired_params() -> dict[str, Any]:
    return {
        "question": "Who chairs the council?",
        "source_selector": {
            "kind": "selected_rows",
            "sheet_id": 1,
            "row_ids": [1],
        },
        "model": "anthropic/claude-haiku-4-5",
        "confirmed": True,
    }


def _retired_typed_request() -> dict[str, Any]:
    return {
        "action_id": RETIRED_KIND,
        "scope": {"kind": "project"},
        "params": _retired_params(),
        "idempotency_key": "retired-kind-typed",
    }


def _retired_legacy_envelope() -> dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "kind": RETIRED_KIND,
        "capabilities": ["project:write", "model:complete"],
        "params": _retired_params(),
        "idempotency_key": "retired-kind-legacy",
    }


@pytest.fixture
def precheck_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the shared catalog precheck both typed entry paths call."""

    calls: list[str] = []
    real = av._row_scope_refusal

    def spy(action: Any) -> Any:
        calls.append(action.kind)
        return real(action)

    monkeypatch.setattr(av, "_row_scope_refusal", spy)
    return calls


@pytest.fixture
def model_call_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every model completion goes through ``ModelRouter.complete``; a refused
    request must never reach it."""

    from frisket.ai.llm import ModelRouter

    calls: list[Any] = []

    async def complete(self: Any, req: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(req)
        raise AssertionError("a refused action must not call the model router")

    monkeypatch.setattr(ModelRouter, "complete", complete)
    return calls


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bad_params", BAD_PARAMS)
def test_validate_action_spec_refuses_non_dict_params_before_any_precheck(
    kind: str, bad_params: Any, precheck_spy: list[str]
) -> None:
    result = av.validate_action_spec(_envelope(kind, bad_params))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_spec"
    assert result.error.message == "ActionSpec did not validate"
    assert precheck_spy == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bad_params", BAD_PARAMS)
def test_payload_only_action_refusal_refuses_non_dict_params_before_any_precheck(
    kind: str, bad_params: Any, precheck_spy: list[str]
) -> None:
    refusal = av.payload_only_action_refusal(_envelope(kind, bad_params))

    assert refusal is None
    assert precheck_spy == []


def test_typed_ner_unknown_param_is_refused_at_the_typed_boundary() -> None:
    """map.ner left the legacy path: its params model still forbids extras,
    now surfaced as the typed boundary's single `invalid_action_request`."""

    from frisket.actions.system import validate_root_action

    request = _typed_ner_request()
    assert validate_root_action(request).ok is True

    request["params"]["unexpected"] = True
    result = validate_root_action(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
    assert result.error.action_kind == "map.ner"
    assert "unexpected" in result.error.message
    assert "extra_forbidden" in result.error.message


def test_legacy_envelope_for_typed_kind_is_refused_before_any_precheck(
    precheck_spy: list[str],
) -> None:
    """A v2 `kind: map.ner` envelope has no legacy validator; it is refused as
    `unsupported_action_kind` without reaching the shared precheck
    (src/frisket/contracts/action_validation.py: the roster lookup precedes
    `_row_scope_refusal` for unregistered kinds)."""

    result = av.validate_action_spec(
        {
            "schema_version": ACTION_SCHEMA_VERSION,
            "kind": "map.ner",
            "capabilities": ["project:write"],
            "row_scope": {"sheet_id": 1, "selector": {"kind": "all_rows"}},
            "params": {"input_columns": ["body"], "labels": ["person"]},
            "idempotency_key": "typed-ingress-control",
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unsupported_action_kind"
    assert precheck_spy == []


# --------------------------------------------------------------------------- #
# The retired kind: refused on every public path, no model call, no write.
# --------------------------------------------------------------------------- #


def test_retired_kind_is_absent_from_catalog_vocabulary_and_launcher_hints() -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import root_action_catalog
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
    )

    assert RETIRED_KIND not in ACTION_REGISTRY.action_ids
    assert RETIRED_KIND not in ACTION_REGISTRY.action_ids
    assert RETIRED_KIND not in {entry.kind for entry in root_action_catalog().actions}
    served = action_catalog_payload_with_launcher_hints({})
    assert RETIRED_KIND not in {entry["kind"] for entry in served["actions"]}
    assert not any(
        RETIRED_KIND in str(entry.get("ui_hints", {}).get("launcher_hints", {}))
        for entry in served["actions"]
    )


def test_retired_kind_typed_request_is_refused_without_model_calls_or_writes(
    tmp_path: Path, precheck_spy: list[str], model_call_spy: list[Any]
) -> None:
    from frisket.actions.system import validate_root_action
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    validation = validate_root_action(_retired_typed_request())
    assert validation.ok is False
    assert validation.error is not None
    assert validation.error.code == "invalid_action_request"
    assert validation.error.action_kind == RETIRED_KIND

    project = Project.create(tmp_path / "retired-typed.frisket", name="Retired")
    try:
        before = _table_counts(project)
        result = run_action_spec(project, _retired_typed_request(), project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert _table_counts(project) == before
    finally:
        project.close()
    assert precheck_spy == []
    assert model_call_spy == []


def test_retired_kind_legacy_envelope_is_refused_without_model_calls_or_writes(
    tmp_path: Path, precheck_spy: list[str], model_call_spy: list[Any]
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    result = av.validate_action_spec(_retired_legacy_envelope())
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unsupported_action_kind"
    assert result.error.action_kind == RETIRED_KIND
    # The roster lookup precedes the shared precheck for an unregistered kind.
    assert precheck_spy == []
    # The payload-only path runs the catalog row-scope policy from the bare
    # envelope for any kind (by design); it has nothing else to say here.
    assert av.payload_only_action_refusal(_retired_legacy_envelope()) is None

    project = Project.create(tmp_path / "retired-legacy.frisket", name="Retired")
    try:
        before = _table_counts(project)
        ran = run_action_spec(project, _retired_legacy_envelope(), project_id="p")
        assert ran.status == "failed"
        assert ran.errors[0].code == "invalid_action_request"
        assert _table_counts(project) == before
    finally:
        project.close()
    assert model_call_spy == []


def test_retired_kind_is_refused_at_the_actions_route_without_model_calls_or_writes(
    tmp_path: Path, model_call_spy: list[Any]
) -> None:
    from fastapi.testclient import TestClient

    from frisket.ai.llm import ModelRouter
    from frisket.contracts.action import ActionResult
    from frisket.server.app import create_app

    # A configured provider key keeps the generic request-time key gate
    # (`missing_provider_key`, decided from `params.model` alone) out of the
    # way so the refusal proven here is the kind refusal itself.
    app = create_app(
        tmp_path / "workspace",
        router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
    )
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "Retired"}).json()["id"]
        catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
        assert catalog.status_code == 200, catalog.text
        assert RETIRED_KIND not in {
            entry["kind"] for entry in catalog.json()["actions"]
        }

        project = app.state.workspace.get(project_id)
        before = _table_counts(project)
        for body, kind in (
            (_retired_typed_request(), RETIRED_KIND),
            (_retired_legacy_envelope(), "unknown"),
        ):
            response = client.post(
                f"/api/projects/{project_id}/actions/v1/run", json=body
            )
            assert response.status_code == 400, response.text
            result = ActionResult.model_validate(response.json())
            assert result.status == "failed"
            assert result.action.kind == kind
            assert [error.code for error in result.errors] == ["invalid_action_request"]
        assert _table_counts(project) == before
    assert model_call_spy == []
