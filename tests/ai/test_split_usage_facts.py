from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from runner_test_helpers import run_action_with_exact_confirmation

pytestmark = pytest.mark.gap

# Private settlement code lives in the separate private composition distribution.
# Keep this rooted at the package that is actually installed by the test
# command; the deleted src/private composition path must not be treated as a seam.

TARIFF_FACT_KEYS = {"credit_charge_usd", "billable", "billing_owner"}
# Enum-like provenance value: short lowercase token (org-byok / project-key /
# platform / configured_key / cache ...), never a sentence, path, or key blob.
ENUMISH_PROVENANCE = re.compile(r"^[a-z][a-z0-9_.:\-]{0,31}$")
SECRETISH_FIELD_NAME = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|credential_value|private_key)"
)
KEY_MATERIAL_VALUE = re.compile(
    r"(?i)\b(sk|key|token|secret|bearer|pat)[-_][A-Za-z0-9+/=]{16,}"
)
LONG_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9+/=_\-]{65,}$")


# --------------------------------------------------------------------------
# Stub-run fixture technique (mirrors tests/test_map_classify_executor.py:
# a real map.classify action through the executor + map-runner path with a
# stub adapter injected into ModelRouter; no network, no keys).
# --------------------------------------------------------------------------


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    async def complete(self, req, client):  # noqa: ANN001
        from frisket.ai.llm import LLMResponse

        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=61,
            tokens_out=23,
            cost=0.004,
            model=req.model,
        )


def _stub_router():
    from frisket.ai.llm import ModelRouter

    reply = {
        "beat": "accountability",
        "beat_justification": "The story involves a no-bid contract.",
        "risk_score": 8,
        "risk_score_justification": "Public spending deserves scrutiny.",
        "beat_confidence": 0.91,
    }
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _StubAdapter(reply)  # noqa: SLF001
    return router


def _seed_project(project_path: Path) -> int:
    from frisket.engine.store import Project

    project = Project.create(project_path, name="Usage Facts")
    try:
        sheet_id = project.add_sheet("Stories")
        columns = {
            "story": project.add_column(sheet_id, "story", type="text"),
        }
        project.add_rows(
            sheet_id,
            [
                {"story": "City hall awarded a no-bid contract"},
                {"story": "Routine road work finished early"},
            ],
            columns,
        )
        return sheet_id
    finally:
        project.close()


def _map_classify_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify city news stories for an accountability desk.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                    "description": "Primary reporting beat.",
                },
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": "map_classify@sha256:usage-facts",
    }


def _run_stub_action(tmp_path: Path):
    """Execute one stubbed map.classify run; return (project, run_id)."""
    from frisket.engine.store import Project

    project_path = tmp_path / "usage-facts.frisket"
    sheet_id = _seed_project(project_path)
    project = Project(project_path)
    result = run_action_with_exact_confirmation(
        project,
        _map_classify_action(sheet_id),
        project_id="project-usage-facts",
        router=_stub_router(),
    )
    assert result.status == "completed", (
        f"stub map.classify run failed: {[e.__dict__ for e in result.errors]}"
    )
    assert result.run_id is not None
    return project, result.run_id


# --------------------------------------------------------------------------
# 1. Tariff symbols leave open fact producers
# --------------------------------------------------------------------------


def test_tariff_symbols_leave_open_fact_producers() -> None:
    metadata = importlib.import_module("frisket.ai.models.metadata")
    assert not hasattr(metadata, "MARKUP_MULTIPLIER"), (
        "frisket.ai.models.metadata still defines MARKUP_MULTIPLIER — the tariff "
        "multiplier must move to the private settlement consumer; open fact "
        "producers record provider facts only"
    )

    for name, obj in vars(metadata).items():
        if not (inspect.isclass(obj) and obj.__module__ == metadata.__name__):
            continue
        fields = set(getattr(obj, "__dataclass_fields__", {}) or {})
        fields |= set(getattr(obj, "__annotations__", {}) or {})
        leaked = fields & TARIFF_FACT_KEYS
        assert not leaked, (
            f"open fact class frisket.models.metadata.{name} still carries "
            f"tariff/billability fields {sorted(leaked)} — customer "
            "billability, tariffs and credit charges are private settlement "
            "outputs, not open facts"
        )


# --------------------------------------------------------------------------
# 2 + 3. Neutral fact rows with credential provenance, provider-cost-only
# accumulation, neutral schema
# --------------------------------------------------------------------------


def test_model_calls_schema_is_neutral(tmp_path: Path) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "schema-cut.frisket", name="Schema Cut")
    try:
        columns = {
            row["name"] for row in project.db.execute("PRAGMA table_info(model_calls)")
        }
    finally:
        project.close()

    leaked = columns & TARIFF_FACT_KEYS
    assert not leaked, (
        f"model_calls (src/frisket/engine/store/schema.py) still has tariff columns "
        f"{sorted(leaked)} — this is a fresh schema cut: the open fact table "
        "carries neutral provider facts only, with no legacy billing columns"
    )
    assert "provider" in columns and "provider_kind" in columns
    assert columns & {"provider_cost_usd", "provider_reported_cost_usd"}, (
        "model_calls must keep neutral provider cost fact columns"
    )
    assert "units" in columns and "cost_source" in columns
    provenance_cols = {
        c for c in columns if c == "credential_source" or "provenance" in c.lower()
    }
    assert provenance_cols, (
        "model_calls must carry a per-call credential provenance column "
        "(credential_source or a successor) — billing launch gate 7"
    )


def test_run_fact_rows_are_neutral_and_carry_credential_provenance(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.runs import RunResultStore

    project, run_id = _run_stub_action(tmp_path)
    try:
        rows = RunResultStore(project).model_calls(run_id)
        assert len(rows) == 2

        for row in rows:
            keys = set(row.keys())

            leaked = keys & TARIFF_FACT_KEYS
            assert not leaked, (
                f"persisted model-call fact still carries tariff/billability "
                f"fields {sorted(leaked)} — open code records neutral facts; "
                "billability and credit charges are computed by the private "
                "settlement consumer"
            )

            # Neutral facts stay present.
            assert row["provider"] == "anthropic"
            cost_key = (
                "provider_cost_usd"
                if "provider_cost_usd" in keys
                else "provider_reported_cost_usd"
            )
            assert row[cost_key] == pytest.approx(0.004)
            units = json.loads(row["units"])
            assert units.get("tokens_in") == 61
            assert units.get("tokens_out") == 23

            # Gate 7: enum-like credential provenance, populated on a real
            # fact produced through the map-runner stub path.
            provenance_keys = [
                k for k in keys if k == "credential_source" or "provenance" in k.lower()
            ]
            assert provenance_keys, "fact row has no credential provenance field"
            provenance = next((row[k] for k in provenance_keys if row[k]), None)
            assert isinstance(provenance, str) and provenance, (
                "credential provenance must be populated on every model-call fact"
            )
            assert provenance not in {"none", "unknown"}, (
                f"credential provenance must be a definite source, got {provenance!r}"
            )
            assert ENUMISH_PROVENANCE.match(provenance), (
                f"credential provenance must be a small enum-like token, got "
                f"{provenance!r}"
            )

            # No secret material rides on the fact row.
            for k in keys:
                assert not SECRETISH_FIELD_NAME.search(k), (
                    f"fact row field name {k!r} suggests secret material"
                )
                value = row[k]
                if isinstance(value, str):
                    assert not KEY_MATERIAL_VALUE.search(value), (
                        f"fact row field {k} carries key-like material"
                    )
                    assert not LONG_OPAQUE_VALUE.match(value.strip()), (
                        f"fact row field {k} carries a long opaque blob "
                        "(possible secret)"
                    )

        # runs.cost_actual accumulates PROVIDER cost only.
        run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        assert run is not None
        assert run["cost_actual"] == pytest.approx(0.008), (
            "runs.cost_actual must equal the sum of per-call provider cost "
            "(2 stub calls x 0.004), never a marked-up customer price"
        )
    finally:
        project.close()


# --------------------------------------------------------------------------
# 5. Private settlement fails closed on unpriceable facts (gates 8/11)
# --------------------------------------------------------------------------

_PRICER_NAME = re.compile(r"(?i)price|settle|charge|bill|reconcile|quarantine")
_FACT_PARAM = re.compile(r"(?i)fact|model_call|usage")


def _neutral_fact(**overrides: Any) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "id": "fact-0001",
        "capability": "llm.complete",
        "engine": "anthropic/claude-haiku-4-5",
        "provider": "anthropic",
        "provider_kind": "chat_api",
        "model_ids": ["claude-haiku-4-5"],
        "credential_source": "platform",
        "provider_reported_cost_usd": 0.004,
        "provider_cost_usd": 0.004,
        "cost_source": "provider_reported",
        "units": {"tokens_in": 61, "tokens_out": 23},
        "cache": {},
        "request_id": "req-0001",
        "warnings": [],
    }
    fact.update(overrides)
    return fact


def _filler_for(name: str) -> Any:
    return 1 if name.endswith("_id") or name in {"org_id", "run_id"} else MagicMock()


def _assert_fails_closed(fn, fact_param: str, fact: dict[str, Any], label: str):
    sig = inspect.signature(fn)
    kwargs: dict[str, Any] = {}
    for name, p in sig.parameters.items():
        if name == fact_param:
            kwargs[name] = fact
        elif p.default is inspect.Parameter.empty and p.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            kwargs[name] = _filler_for(name)
    try:
        sig.bind(**kwargs)
        invoke = lambda: fn(**kwargs)  # noqa: E731
    except TypeError:
        invoke = lambda: fn(fact)  # noqa: E731

    try:
        result = invoke()
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
    except Exception:
        return  # raised = refused = fail closed

    assert result is not None, (
        f"{label}: settlement returned None for an unpriceable fact — it "
        "must raise or return an explicit rejected/quarantined outcome; "
        "silence is silent unmetering (gate 11)"
    )
    assert isinstance(result, Mapping), (
        f"{label}: settlement returned {result!r} for an unpriceable fact — "
        "it must raise or return an explicit rejected/quarantined outcome"
    )
    for k, v in result.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and re.search(
            r"(?i)charge|credit|price|amount|micro", str(k)
        ):
            assert v <= 0, (
                f"{label}: unpriceable fact produced a positive charge "
                f"{k}={v} — missing provenance / version skew must NEVER "
                "silently price as billable"
            )
    assert not result.get("billable"), f"{label}: unpriceable fact was marked billable"
    blob = " ".join(f"{k}={v}" for k, v in result.items()).lower()
    assert re.search(
        r"reject|quarantin|refus|invalid|unpriceable|missing|unsupported|"
        r"unrecognized|skew|error|fail",
        blob,
    ), (
        f"{label}: settlement result {dict(result)!r} carries no explicit "
        "rejection/quarantine signal for an unpriceable fact"
    )
