"""Action-preview effect fence: classify before any in-memory dispatch.

The durable runner owns effect checkpoints.  Preview owns none, so its only
safe effectful cases are statically non-egressing work whose typed rated quote
is exactly free, with an allowlisted venue whenever resolution is consumed.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import preview as preview_module
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.runner.network_policy import (
    row_effect_cannot_egress,
    row_effect_spends_or_meters,
)
from frisket.engine.store import Project as StoreProject
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import model_plan
from frisket.ops.ocr_engines import OcrEngines


_MESSAGE = (
    "This action can cause an external or metered effect, so it cannot run as "
    "a preview. Run the action instead."
)
_SAFE_QUOTE = {
    "cost": 0.0,
    "cost_source": "free_local",
    "billed_cost": 0,
    "policy_id": "test.identity.v1",
    "rows": 1,
}


class _Project:
    def visible_row_ids(self, _sheet_id: int) -> list[int]:
        return [1]


class _Recipe:
    name = "effect-probe"

    def __init__(self, *, consumes_resolution: bool = False) -> None:
        self.consumes_resolution = consumes_resolution
        self.scope_calls = 0

    def is_llm(self, _spec: dict[str, Any]) -> bool:
        return False

    def cost_class_for(self, _spec: dict[str, Any]) -> str:
        return "metered"

    def requires_row_effect_checkpoint(self, _spec: dict[str, Any]) -> bool:
        return False

    def output_fields(self, _spec: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def source_columns(self, _spec: dict[str, Any]) -> list[str]:
        return []

    @asynccontextmanager
    async def execution_scope(self, *_args: Any, **_kwargs: Any):
        self.scope_calls += 1
        yield


class _LlmRecipe(_Recipe):
    def is_llm(self, _spec: dict[str, Any]) -> bool:
        return True


class _BatchRecipe(_Recipe):
    def __init__(self, *, consumes_resolution: bool = True) -> None:
        super().__init__(consumes_resolution=consumes_resolution)
        self.batch_calls = 0

    async def execute_batch(
        self,
        values_by_row: dict[int, Any],
        _spec: dict[str, Any],
        _ctx: Any,
    ) -> dict[int, dict[str, Any]]:
        self.batch_calls += 1
        return {row_id: {} for row_id in values_by_row}


def _resolution(egress_class: Any) -> Any:
    return SimpleNamespace(
        resolution=SimpleNamespace(
            facts=SimpleNamespace(egress_class=egress_class),
        )
    )


def _validated(recipe: _Recipe, estimate: Any, resolved: Any) -> Any:
    return SimpleNamespace(
        recipe=recipe,
        sheet_id=1,
        columns=[],
        col_map={},
        row_ids=[1],
        output_fields=[],
        est=estimate,
        resolved_execution=resolved,
    )


def _install_validation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recipe: _Recipe,
    effectful: bool,
    cannot_egress: bool,
    estimate: Any = _SAFE_QUOTE,
    resolved: Any = None,
    raised: BaseException | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(validation, "recipe_for_spec", lambda _spec: recipe)
    monkeypatch.setattr(
        preview_module,
        "row_effect_spends_or_meters",
        lambda candidate, _spec, _router: candidate is recipe and effectful,
        raising=False,
    )
    monkeypatch.setattr(
        preview_module,
        "row_effect_cannot_egress",
        lambda candidate, _spec: candidate is recipe and cannot_egress,
        raising=False,
    )

    def validate_spec(*args: Any, **kwargs: Any) -> Any:
        call = dict(kwargs)
        call["spec"] = deepcopy(args[3])
        calls.append(call)
        if raised is not None:
            raise raised
        return _validated(recipe, estimate, resolved)

    monkeypatch.setattr(validation, "validate_spec", validate_spec)

    def no_second_resolution(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preview resolved separately from its one validate_spec call")

    monkeypatch.setattr(validation, "resolve_for_action", no_second_resolution)
    monkeypatch.setattr(
        preview_module, "resolve_for_action", no_second_resolution, raising=False
    )
    return calls


def _preview_validated(spec: dict[str, Any] | None = None) -> Any:
    return preview_module._preview_validated(
        _Project(),
        SimpleNamespace(client=None),
        object(),
        spec or {"row_ids": [1]},
        pricing_policy=object(),
        composition=SimpleNamespace(credential_use_context=None),
    )


def _assert_requires_run(exc: BaseException) -> None:
    # Runtime-name assertion keeps red-first collection honest before the new
    # exception exists.  A missing import is not evidence; the old behavior
    # must execute and fail this assertion.
    assert type(exc).__name__ == "PreviewEffectRequiresRun"
    assert str(exc) == _MESSAGE


def _catch(call) -> BaseException:  # noqa: ANN001
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - assertion surface
        return exc
    pytest.fail("unsafe action preview was admitted")


def _census_plan(sheet_id=1, row_ids=None, *, project=None):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        build_typed_map_rows_plan,
    )

    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("enrich.census_demographics"),
        ActionRequest(
            action_id="enrich.census_demographics",
            scope=SheetRows(sheet_id=sheet_id, row_ids=row_ids),
            params={"source": "point"},
            idempotency_key="census-preview-fence",
        ),
    )
    return (
        _typed_map_rows_plan(bound)
        if project is None
        else build_typed_map_rows_plan(project, bound)
    )


def _api_plan(sheet_id=1, row_ids=None, *, project=None):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        build_typed_map_rows_plan,
    )

    bound = typed_action_for_request(
        {
            "action_id": "map.api_call",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {
                "request": {
                    "url": "https://api.test.example/items?q={{ q }}",
                },
            },
            "idempotency_key": "api-preview-fence",
        }
    )
    return (
        _typed_map_rows_plan(bound)
        if project is None
        else build_typed_map_rows_plan(project, bound)
    )


@pytest.mark.parametrize(
    ("kind", "extra", "spends", "cannot_egress"),
    [
        ("map.api_call", {}, True, False),
        ("enrich.census_demographics", {}, True, False),
        ("map.python", {}, False, True),
        ("map.ner", {"engine": "spacy"}, False, True),
        ("map.translate", {"engine": "opus_mt"}, True, True),
        ("map.translate", {"engine": "hy_mt2"}, True, True),
        ("media.ocr", {"engine": "rapidocr"}, True, True),
        ("media.ocr", {"engine": "dots.mocr"}, True, True),
        ("media.transcribe", {"engine": "faster_whisper"}, True, True),
        # A composition may price even a local engine. Actual zero-cost routes
        # still preview without consent; keep the generic recovery checkpoint.
        ("media.to_markdown", {"engine": "markitdown"}, True, True),
    ],
)
def test_static_effect_matrix_uses_existing_predicates(
    kind: str,
    extra: dict[str, Any],
    spends: bool,
    cannot_egress: bool,
) -> None:
    if kind == "map.api_call":
        plan = _api_plan()
        recipe, spec = plan.program, plan.spec_dict()
    elif kind == "enrich.census_demographics":
        plan = _census_plan()
        recipe, spec = plan.program, plan.spec_dict()
    elif kind in {"media.to_markdown", "media.ocr", "media.transcribe"}:
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

        plan = _typed_map_rows_plan(
            typed_action_for_request(
                {
                    "action_id": kind,
                    "scope": {"kind": "sheet_rows", "sheet_id": 1},
                    "params": {"source": "doc", **extra},
                    "output_names": {"markdown": "converted"}
                    if kind == "media.to_markdown"
                    else {"text": "read_text", "blocks": "read_blocks"}
                    if kind == "media.ocr"
                    else {
                        "text": "transcript",
                        "segments": "segments",
                        "detected_language": "language",
                    },
                    "idempotency_key": "markdown-preview-fence",
                }
            )
        )
        recipe, spec = plan.program, plan.spec_dict()
    elif kind == "map.python":
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
        from http_test_helpers import (
            queued_python_run_spec,
            v1_action_from_canonical_run_spec,
        )

        plan = _typed_map_rows_plan(
            typed_action_for_request(
                v1_action_from_canonical_run_spec(
                    queued_python_run_spec(1, "text", "copied")
                )
            )
        )
        recipe, spec = plan.program, plan.spec_dict()
    else:
        plan = model_plan(
            {
                "action_kind": kind,
                "input_columns": ["text"],
                **extra,
                **({"language": ["en"]} if extra.get("engine") == "opus_mt" else {}),
                **({"labels": ["person"]} if kind == "map.ner" else {}),
            }
        )
        recipe, spec = plan.program, plan.spec_dict()
    router = SimpleNamespace(cache_mode="off", cache=None)

    assert row_effect_spends_or_meters(recipe, spec, router) is spends
    assert row_effect_cannot_egress(recipe, spec) is cannot_egress


def test_strict_replay_requires_an_attached_cache_and_cannot_hide_web_effect() -> None:
    summarize = _LlmRecipe()
    summary_spec = {
        "action_kind": "map.ask",
        "model": "anthropic/claude-haiku-4-5",
    }
    cacheless = SimpleNamespace(cache_mode="replay_strict", cache=None)
    cached = SimpleNamespace(cache_mode="replay_strict", cache=object())
    assert row_effect_spends_or_meters(summarize, summary_spec, cacheless) is True
    assert row_effect_spends_or_meters(summarize, summary_spec, cached) is False

    plan = model_plan(
        {
            "action_kind": "research.answer",
            "model": "anthropic/claude-haiku-4-5",
            "question": {"text": "Who runs {{text}}?"},
        }
    )
    assert row_effect_spends_or_meters(plan.program, plan.spec_dict(), cached) is True


@pytest.mark.parametrize(
    ("label", "estimate", "cannot_egress", "consumes_resolution", "resolved"),
    [
        ("provider-positive", {**_SAFE_QUOTE, "cost": 0.01}, True, False, None),
        ("provider-overflow", {**_SAFE_QUOTE, "cost": 10**10000}, True, False, None),
        ("billed-positive", {**_SAFE_QUOTE, "billed_cost": 1}, True, False, None),
        ("wrong-source", {**_SAFE_QUOTE, "cost_source": "provider"}, True, False, None),
        ("provider-null", {**_SAFE_QUOTE, "cost": None}, True, False, None),
        ("billed-null", {**_SAFE_QUOTE, "billed_cost": None}, True, False, None),
        (
            "missing-policy",
            {k: v for k, v in _SAFE_QUOTE.items() if k != "policy_id"},
            True,
            False,
            None,
        ),
        ("malformed-cost", {**_SAFE_QUOTE, "cost": "zero"}, True, False, None),
        ("non-mapping-estimate", [], True, False, None),
        ("unknown-money-fact", {**_SAFE_QUOTE, "surcharge": 0}, True, False, None),
        ("static-egress", _SAFE_QUOTE, False, False, None),
        ("resolution-required", _SAFE_QUOTE, True, True, None),
        ("third-party", _SAFE_QUOTE, True, True, _resolution("third_party_api")),
        ("shared", _SAFE_QUOTE, True, True, _resolution("shared_service")),
        ("dedicated", _SAFE_QUOTE, True, True, _resolution("dedicated")),
        ("unknown-egress", _SAFE_QUOTE, True, True, _resolution("mystery")),
        ("malformed-egress", _SAFE_QUOTE, True, True, _resolution([])),
        ("malformed-resolution", _SAFE_QUOTE, True, True, SimpleNamespace()),
        ("opaque-resolution", _SAFE_QUOTE, True, True, object()),
        (
            "missing-resolution-facts",
            _SAFE_QUOTE,
            True,
            True,
            SimpleNamespace(resolution=SimpleNamespace()),
        ),
    ],
)
def test_every_money_and_venue_conjunct_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    estimate: Any,
    cannot_egress: bool,
    consumes_resolution: bool,
    resolved: Any,
) -> None:
    del label
    recipe = _Recipe(consumes_resolution=consumes_resolution)
    calls = _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=cannot_egress,
        estimate=estimate,
        resolved=resolved,
    )

    exc = _catch(_preview_validated)

    _assert_requires_run(exc)
    assert len(calls) == 1
    assert calls[0]["persistence"] == "ephemeral"


@pytest.mark.parametrize(
    ("label", "consumes_resolution", "resolved"),
    [
        ("opus_mt-no-resolution", False, None),
        ("rapidocr-none", True, _resolution("none")),
        ("dots.mocr-operator-lan", True, _resolution("operator_lan")),
    ],
)
def test_exact_free_local_allowance_has_the_required_resolution_shape(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    consumes_resolution: bool,
    resolved: Any,
) -> None:
    del label
    recipe = _Recipe(consumes_resolution=consumes_resolution)
    calls = _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=True,
        resolved=resolved,
    )
    spec = {"row_ids": [1], "confirmed": False}
    original = deepcopy(spec)

    admitted = _preview_validated(spec)

    assert admitted.recipe is recipe
    assert len(calls) == 1
    assert calls[0]["persistence"] == "ephemeral"
    assert calls[0]["spec"] == original
    assert calls[0]["confirmed"] is False
    assert "consented_promise_set_hash" not in calls[0]["spec"]
    assert spec == original


@pytest.mark.parametrize(
    "refusal", [validation.CostGate(0.0), validation.MissingProviderKey("anthropic")]
)
def test_non_effectful_candidates_retain_existing_refusals(
    monkeypatch: pytest.MonkeyPatch,
    refusal: BaseException,
) -> None:
    recipe = _Recipe()
    _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=False,
        cannot_egress=True,
        raised=refusal,
    )

    assert _catch(_preview_validated) is refusal


def _claims_gate(
    estimate: dict[str, Any] = _SAFE_QUOTE,
    *,
    claims: list[dict[str, str]] | None = None,
) -> validation.ClaimsGate:
    return validation.ClaimsGate(
        0.0,
        claims=claims
        if claims is not None
        else [{"field": "egress_class", "display": "Uses your LAN sidecar."}],
        promise_set_hash="a" * 64,
        estimate_details=estimate,
    )


def test_free_operator_lan_claims_gate_is_rethrown_unchanged_then_retry_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _Recipe(consumes_resolution=True)
    gate = _claims_gate()
    outcomes: list[Any] = [
        gate,
        _validated(recipe, _SAFE_QUOTE, _resolution("operator_lan")),
    ]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(validation, "recipe_for_spec", lambda _spec: recipe)
    monkeypatch.setattr(
        preview_module,
        "row_effect_spends_or_meters",
        lambda *_args: True,
        raising=False,
    )
    monkeypatch.setattr(
        preview_module,
        "row_effect_cannot_egress",
        lambda *_args: True,
        raising=False,
    )

    def validate_spec(*_args: Any, **kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(validation, "validate_spec", validate_spec)

    assert _catch(_preview_validated) is gate
    admitted = _preview_validated()

    assert admitted.resolved_execution.resolution.facts.egress_class == "operator_lan"
    assert len(calls) == 2
    assert all(call["persistence"] == "ephemeral" for call in calls)


@pytest.mark.parametrize(
    ("gate", "cannot_egress"),
    [
        (validation.CostGate(0.0, estimate_details=_SAFE_QUOTE), True),
        (
            _claims_gate(
                claims=[
                    {"field": "egress_class", "display": "Uses your LAN sidecar."},
                    {"field": "cost", "display": "May bill."},
                ]
            ),
            True,
        ),
        (_claims_gate({**_SAFE_QUOTE, "cost": 0.01}), True),
        (_claims_gate({**_SAFE_QUOTE, "billed_cost": 1}), True),
        (_claims_gate({**_SAFE_QUOTE, "cost_source": "estimated"}), True),
        (_claims_gate({**_SAFE_QUOTE, "cost": "zero"}), True),
        (
            validation.ClaimsGate(
                0.0,
                claims=[{"field": "egress_class", "display": "Uses your LAN sidecar."}],
                promise_set_hash="b" * 64,
                estimate_details=None,
            ),
            True,
        ),
        (_claims_gate(), False),
    ],
)
def test_unsafe_gate_is_reclassified_instead_of_rendered(
    monkeypatch: pytest.MonkeyPatch,
    gate: validation.CostGate,
    cannot_egress: bool,
) -> None:
    recipe = _Recipe(consumes_resolution=True)
    _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=cannot_egress,
        raised=gate,
    )

    _assert_requires_run(_catch(_preview_validated))


def test_malformed_claims_shape_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    recipe = _Recipe(consumes_resolution=True)
    gate = _claims_gate()
    gate.claims = None  # type: ignore[assignment] - adversarial runtime shape
    _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=True,
        raised=gate,
    )

    _assert_requires_run(_catch(_preview_validated))


def test_provider_key_refusal_is_reclassified_for_effectful_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = _Recipe()
    _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=False,
        raised=validation.MissingProviderKey("anthropic"),
    )

    _assert_requires_run(_catch(_preview_validated))


@pytest.mark.parametrize("entrypoint", ["validated", "precheck", "batch", "row"])
def test_all_entrypoints_refuse_before_batch_scope_or_row(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    recipe: _Recipe = _BatchRecipe() if entrypoint == "batch" else _Recipe()
    calls = _install_validation(
        monkeypatch,
        recipe=recipe,
        effectful=True,
        cannot_egress=False,
        estimate={**_SAFE_QUOTE, "cost_source": "unknown", "cost": None},
    )
    row_calls = 0

    async def execute_row(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal row_calls
        row_calls += 1
        return {}

    monkeypatch.setattr(preview_module, "execute_row", execute_row)
    runner_shape = SimpleNamespace(
        project=_Project(),
        router=SimpleNamespace(client=None),
        run_store=object(),
        pricing_policy=object(),
        execution_composition=SimpleNamespace(credential_use_context=None),
    )
    spec = {"row_ids": [1], "action_kind": "map.api_call"}

    if entrypoint == "validated":
        exc = _catch(lambda: _preview_validated(spec))
    elif entrypoint == "precheck":
        exc = _catch(lambda: MapRunner.preview_precheck(runner_shape, spec))
    else:
        exc = _catch(
            lambda: asyncio.run(
                preview_module.run_preview(
                    runner_shape.project,
                    runner_shape.router,
                    runner_shape.run_store,
                    object(),
                    {},
                    lambda _recipe, _spec: 1,
                    spec,
                    pricing_policy=runner_shape.pricing_policy,
                    composition=runner_shape.execution_composition,
                )
            )
        )

    _assert_requires_run(exc)
    assert len(calls) == 1
    assert recipe.scope_calls == 0
    assert getattr(recipe, "batch_calls", 0) == 0
    assert row_calls == 0


# ---------------------------------------------------------------------------
# Real recipe/validation paths. The stubs sit at the final engine/effect seam;
# recipe lookup, static classification, validation, rating, and resolution all
# run as production does.


_ZERO_WRITE_TABLES = {
    "cells",
    "columns",
    "consents",
    "effect_checkpoints",
    "execution_attempts",
    "model_calls",
    "ops",
    "output_column_claims",
    "promise_sets",
    "receipts",
    "results",
    "routes",
    "run_rows",
    "run_scopes",
    "runs",
}


def _database_snapshot(project: StoreProject) -> tuple[int, dict[str, int]]:
    """SQLite mutation counter plus every named durable application table."""
    tables = [
        str(row[0])
        for row in project.db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    counts = {
        table: int(project.db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }
    assert _ZERO_WRITE_TABLES <= set(counts), (
        "the all-table zero-write snapshot lost a named effect table: "
        f"{sorted(_ZERO_WRITE_TABLES - set(counts))}"
    )
    return project.db.total_changes, counts


def _exact_ephemeral_gate(
    runner: MapRunner, spec: dict[str, Any], *, program=None
) -> validation.ClaimsGate:
    """Mint the validator's real exact echo below the preview-owned fence."""
    assert runner.execution_composition is not None
    with pytest.raises(validation.ClaimsGate) as gated:
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            spec,
            confirmed=False,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
            persistence="ephemeral",
            program=program,
        )
    gate = gated.value
    assert isinstance(gate.promise_set_hash, str)
    assert len(gate.promise_set_hash) == 64
    return gate


def _exact_retry(spec: dict[str, Any], gate: validation.ClaimsGate) -> dict[str, Any]:
    return {
        **deepcopy(spec),
        "confirmed": True,
        "consented_promise_set_hash": gate.promise_set_hash,
    }


def test_real_api_call_exact_echo_refuses_before_row_effect(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor.http_request import AdmittedHttpRequester

    project = StoreProject.create(tmp_path / "preview-api-call.frisket")
    try:
        sheet_id = project.add_sheet("Data")
        source = project.add_column(sheet_id, "q", type="text")
        [row_id] = project.add_rows(sheet_id, [{"q": "alpha"}], {"q": source})
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        plan = _api_plan(sheet_id, [row_id], project=project)
        spec = plan.spec_dict()
        execute_calls: list[dict[str, Any]] = []

        async def counted_execute(
            _self: Any,
            request: Any,
            _row: Any,
        ) -> dict[str, Any]:
            execute_calls.append(request.model_dump(mode="json"))
            return {"unexpected": True}

        monkeypatch.setattr(AdmittedHttpRequester, "request_json", counted_execute)
        before = _database_snapshot(project)
        gate = _exact_ephemeral_gate(runner, spec, program=plan.program)
        assert gate.claims == []
        assert gate.estimate_details["cost"] is None
        assert gate.estimate_details["billed_cost"] is None

        exc = _catch(
            lambda: asyncio.run(
                runner.preview(_exact_retry(spec, gate), program=plan.program)
            )
        )

        _assert_requires_run(exc)
        assert execute_calls == []
        assert _database_snapshot(project) == before
    finally:
        project.close()


def test_real_census_preview_refuses_before_execute_batch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CENSUS_API_KEY", "test-census-key")
    project = StoreProject.create(tmp_path / "preview-census.frisket")
    try:
        sheet_id = project.add_sheet("Places")
        source = project.add_column(sheet_id, "point", type="geo_point")
        [row_id] = project.add_rows(
            sheet_id,
            [{"point": {"lat": 38.9, "lon": -77.03}}],
            {"point": source},
        )
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        plan = _census_plan(sheet_id, [row_id], project=project)
        spec = plan.spec_dict()
        batch_calls: list[dict[int, dict[str, Any]]] = []

        async def counted_batch(
            _self: Any,
            rows: dict[int, dict[str, Any]],
            _spec: dict[str, Any],
            _ctx: Any,
        ) -> dict[int, dict[str, Any]]:
            batch_calls.append(deepcopy(rows))
            return {row: {} for row in rows}

        monkeypatch.setattr(type(plan.program), "execute_batch", counted_batch)
        before = _database_snapshot(project)
        exc = _catch(lambda: asyncio.run(runner.preview(spec, program=plan.program)))

        _assert_requires_run(exc)
        assert batch_calls == []
        assert _database_snapshot(project) == before
    finally:
        project.close()


def _seed_ocr_project(tmp_path: Any, filename: str) -> tuple[StoreProject, int, int]:
    project = StoreProject.create(tmp_path / filename)
    sheet_id = project.add_sheet("Scans")
    source = project.add_column(sheet_id, "page", type="image")
    blob = project.add_blob(
        b"\x89PNG\r\n\x1a\n preview-page",
        filename="scan.png",
        mime="image/png",
        metadata=owned_media_metadata_document(
            probe={"kind": "image", "width": 100, "height": 100}
        ),
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"page": media_cell(blob, mime="image/png", filename="scan.png")}],
        {"page": source},
    )
    return project, sheet_id, row_id


def test_real_dots_mocr_runs_once_at_operator_lan_without_writes(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "test-token")
    project, sheet_id, row_id = _seed_ocr_project(tmp_path, "preview-dots.mocr.frisket")
    try:
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(
                {
                    "action_id": "media.ocr",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [row_id],
                    },
                    "params": {"source": "page", "engine": "dots.mocr"},
                    "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
                    "idempotency_key": "dots-preview",
                }
            ),
        )
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        spec = plan.spec_dict()
        engine_calls: list[tuple[str, int]] = []

        async def fake_pages(
            _self: OcrEngines,
            engine: str,
            page_paths: list[Any],
            _ctx: Any,
            **_kwargs: Any,
        ) -> list[dict[str, Any]]:
            engine_calls.append((engine, len(page_paths)))
            return [
                {
                    "text": "FROM OPERATOR LAN",
                    "blocks": [{"text": "FROM OPERATOR LAN"}],
                }
                for _path in page_paths
            ]

        monkeypatch.setattr(OcrEngines, "run_engine_on_pages", fake_pages)
        real_resolve = validation.resolve_for_action
        resolutions: list[Any] = []

        def counted_resolution(*args: Any, **kwargs: Any) -> Any:
            resolved = real_resolve(*args, **kwargs)
            resolutions.append(resolved)
            return resolved

        monkeypatch.setattr(validation, "resolve_for_action", counted_resolution)
        before = _database_snapshot(project)
        result = asyncio.run(runner.preview(spec, program=plan.program))

        assert len(resolutions) == 1
        assert resolutions[0].resolution.facts.egress_class == "operator_lan"
        assert resolutions[0].persistence == "ephemeral"
        assert engine_calls == [("dots.mocr", 1)]
        assert result.values[row_id]["ocr_text"]["value"] == "FROM OPERATOR LAN"
        assert _database_snapshot(project) == before
    finally:
        project.close()


def test_real_opus_mt_exact_free_local_preview_uses_actual_validation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ops.integrations import opus_mt

    project = StoreProject.create(tmp_path / "preview-opus-mt.frisket")
    try:
        sheet_id = project.add_sheet("Text")
        source = project.add_column(sheet_id, "statement", type="text")
        [row_id] = project.add_rows(
            sheet_id, [{"statement": "Hello world."}], {"statement": source}
        )
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(
                {
                    "action_id": "map.translate",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [row_id],
                    },
                    "params": {
                        "source": ["statement"],
                        "engine": "opus_mt",
                        "language": ["en"],
                        "target_language": "Spanish",
                    },
                    "output_names": {"translation": "es"},
                    "idempotency_key": "opus-preview-fence",
                }
            ),
        )
        spec = plan.spec_dict()
        translate_calls: list[tuple[str, str, list[str]]] = []
        monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)

        def fake_translate(
            source_code: str,
            target_code: str,
            texts: list[str],
            *,
            cache_root: Any = None,
        ) -> list[str]:
            del cache_root
            translate_calls.append((source_code, target_code, list(texts)))
            return ["Hola mundo."]

        monkeypatch.setattr(opus_mt, "translate_texts", fake_translate)
        real_validate = validation.validate_spec
        validated: list[Any] = []

        def captured_validation(*args: Any, **kwargs: Any) -> Any:
            result = real_validate(*args, **kwargs)
            validated.append(result)
            return result

        monkeypatch.setattr(validation, "validate_spec", captured_validation)
        before = _database_snapshot(project)

        result = asyncio.run(runner.preview(spec, program=plan.program))

        assert len(validated) == 1
        assert validated[0].recipe.consumes_resolution is True
        assert validated[0].resolved_execution.resolution.facts.egress_class == "none"
        assert validated[0].est["cost_source"] == "free_local"
        assert validated[0].est["cost"] == 0.0
        assert validated[0].est["billed_cost"] == 0
        assert translate_calls == [("en", "es", ["Hello world."])]
        assert result.values[row_id]["es"]["value"] == "Hola mundo."
        assert _database_snapshot(project) == before
    finally:
        project.close()
