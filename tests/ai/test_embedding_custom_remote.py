"""custom remote / OpenRouter embedding models as a gated product path.

A custom REMOTE model (OpenRouter, or any configured OpenAI-compat remote) has no
curated dimension. Creating an index over it must DISCOVER the dimension with a
single short probe embed through the gateway — but only AFTER an egress
confirmation gate (allow_remote), and the minted space must carry the EXACT
discovered dimension (never a hardcoded/faked value). The create receipt records
the requested model + discovered dimension + probe metadata. A non-remote
uncurated id is unaffected (still embedding_model_unsupported). A probe failure is
a typed error that writes no space.

All tests inject a FAKE gateway/router — no real network. The fake returns a
known-width vector so the probe discovers a specific dimension (1024 here).
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import EmbeddingStore, build_batch_result
from frisket.ai.embeddings.capabilities import resolve_embedding_capability
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.server.embedding_catalog import embedding_provider_catalog_payload
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.team.security.secrets import encrypt_secret
from helpers import write_claimless_test_model_calls

PROJECT_ID = "project-custom-remote"
_PROBE_DIM = 1024
_OPENROUTER_MODEL = "openai/text-embedding-3-large"


class FakeProbeGateway:
    """Deterministic remote embedder. Records calls so a blocked create can assert
    ZERO egress. Returns a fixed-width vector so the probe discovers _PROBE_DIM,
    and reports an actual_model_id distinct from the requested one (as a real
    OpenRouter response would echo)."""

    def __init__(
        self,
        *,
        dim: int = _PROBE_DIM,
        fail: bool = False,
        credential_source: str = "local",
        provider_cost_usd: float | None = None,
    ):
        self.dim = dim
        self.fail = fail
        self.credential_source = credential_source
        self.provider_cost_usd = provider_cost_usd
        self.calls: list[tuple] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        if self.fail:
            from frisket.ai.embeddings.gateway import EmbeddingProviderError

            raise EmbeddingProviderError("provider exploded")
        vectors = [[0.1] * self.dim for _ in texts]
        return build_batch_result(
            vectors,
            provider_id=provider or "openrouter",
            provider_kind="platform_api",
            requested_model=model,
            actual_model_id=f"resolved/{model}",
            modality=modality,
            usage={"input_count": len(texts), "input_tokens": 3},
            credential_source=self.credential_source,
            provider_reported_cost_usd=self.provider_cost_usd,
            provider_cost_usd=self.provider_cost_usd,
            cost_source=(
                "provider_reported" if self.provider_cost_usd is not None else "unknown"
            ),
        )


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "cr.frisket", name="cr")
    sheet = p.add_sheet("data")
    col = p.add_column(sheet, "headline")
    p.add_rows(sheet, [{"headline": f"story {i}"} for i in range(3)], {"headline": col})
    p._sheet_id = sheet  # type: ignore[attr-defined]
    yield p
    p.close()


class _OpenRouterRouter:
    """Router with OpenRouter configured (key present)."""

    def providers(self):
        return ["openrouter"]


class _NoKeyRouter:
    def providers(self):
        return []


def _create_action(
    sheet_id,
    *,
    provider="openrouter",
    model=_OPENROUTER_MODEL,
    key="cr_create@1",
    allow_remote=True,
):
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": provider,
            "model": model,
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": allow_remote},
        },
        "idempotency_key": key,
    }


def _run(project, data, *, gateway=None):
    deps = ExecutorDeps(embedding_gateway=gateway) if gateway is not None else None
    return run_action_spec(project, data, project_id=PROJECT_ID, deps=deps)


def _counts(project):
    n_idx = project.db.execute(
        "SELECT COUNT(*) AS n FROM embedding_indexes"
    ).fetchone()["n"]
    n_sp = project.db.execute("SELECT COUNT(*) AS n FROM embedding_spaces").fetchone()[
        "n"
    ]
    return n_idx, n_sp


def _accrue_project_key_spend(
    project: Project,
    *,
    provider: str,
    cost_usd: float,
) -> None:
    """Charge a prior call through the durable ledger, never the counter."""
    row_id = int(project.visible_row_ids(project._sheet_id)[0])
    column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='headline'",
            (project._sheet_id,),
        ).fetchone()["id"]
    )
    op_id = project.append_op(
        "map", {"recipe": "prior_provider_spend"}, label="prior spend"
    )
    run_id = RunResultStore(project).start_run(
        op_id, project._sheet_id, "test.prior_provider_spend"
    )
    write_claimless_test_model_calls(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.embed",
                        "engine": f"{provider}/{_OPENROUTER_MODEL}",
                        "provider": provider,
                        "provider_kind": "platform_api",
                        "credential_source": "project_key",
                        "provider_reported_cost_usd": cost_usd,
                        "provider_cost_usd": cost_usd,
                        "cost_source": "provider_reported",
                        "units": {"input_count": 1, "input_tokens": 3},
                    }
                ],
            }
        ],
    )
    project.db.commit()


# --------------------------------------------------------------------------
# capability fallback
# --------------------------------------------------------------------------


def test_resolve_synthesizes_remote_custom_capability_when_configured():
    cap = resolve_embedding_capability(
        modality="text",
        provider="openrouter",
        model=_OPENROUTER_MODEL,
        router=_OpenRouterRouter(),
    )
    assert cap is not None
    assert cap["dimension_discovery_required"] is True
    assert cap["dimensions"] is None
    assert cap["available"] is True
    assert cap["local"] is False
    assert cap["provider_kind"] == "platform_api"
    assert cap["recommended"] is False


def test_resolve_remote_custom_disabled_when_not_configured():
    cap = resolve_embedding_capability(
        modality="text",
        provider="openrouter",
        model=_OPENROUTER_MODEL,
        router=_NoKeyRouter(),
    )
    assert cap is not None
    assert cap["dimension_discovery_required"] is True
    assert cap["available"] is False
    assert cap["error"]


def test_non_remote_uncurated_id_still_none():
    # This behavior must not regress: a non-remote, non-fastembed id resolves
    # to None (the executor surfaces embedding_model_unsupported).
    cap = resolve_embedding_capability(
        modality="text",
        provider=None,
        model="totally/not-a-real-model",
        router=_NoKeyRouter(),
        local_available=False,
    )
    assert cap is None


# --------------------------------------------------------------------------
# create-time probe + gate + receipt
# --------------------------------------------------------------------------


def test_create_probes_once_and_mints_discovered_dimension(project):
    gw = FakeProbeGateway(dim=_PROBE_DIM)
    res = _run(project, _create_action(project._sheet_id), gateway=gw)
    assert res.status == "completed", res.errors
    # exactly ONE probe call, one short text
    assert len(gw.calls) == 1
    texts, provider, model, modality = gw.calls[0]
    assert provider == "openrouter"
    assert model == _OPENROUTER_MODEL
    assert modality == "text"
    assert len(texts) == 1

    store = EmbeddingStore(project)
    index = store.get_index(res.outputs[0].ref["index_id"])
    space = store.get_space(index["space_id"])
    assert space["dimension"] == _PROBE_DIM
    assert res.outputs[0].ref["dimension"] == _PROBE_DIM


def test_create_without_allow_remote_blocks_before_probe(project):
    gw = FakeProbeGateway(dim=_PROBE_DIM)
    res = _run(
        project,
        _create_action(project._sheet_id, allow_remote=False, key="cr_block@1"),
        gateway=gw,
    )
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_remote_confirmation_required"
    # NO egress: the gate fires before any probe call.
    assert gw.calls == []
    # NO space/index written.
    assert _counts(project) == (0, 0)


def test_create_string_allow_remote_never_probes(project):
    gw = FakeProbeGateway(dim=_PROBE_DIM)
    result = _run(
        project,
        _create_action(project._sheet_id, allow_remote="false", key="cr_string@1"),
        gateway=gw,
    )
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_remote_confirmation_required"
    assert gw.calls == []


def test_create_dimension_probe_refuses_over_cap_project_key_before_egress(project):
    """A paid create-time probe is subject to the same project-key cap."""
    project.set_provider_key(
        provider="openrouter",
        encrypted=encrypt_secret("sk-project-openrouter"),
        hint="sk-p***",
        spend_cap_micro=500,
    )
    _accrue_project_key_spend(project, provider="openrouter", cost_usd=0.001)
    assert project.provider_spend_state("openrouter").over_cap

    gw = FakeProbeGateway(
        dim=_PROBE_DIM,
        credential_source="project_key",
        provider_cost_usd=0.0007,
    )
    res = _run(
        project,
        _create_action(project._sheet_id, key="cr_over_cap_probe@1"),
        gateway=gw,
    )

    assert res.status == "failed"
    assert res.errors[0].code == "provider_spend_cap_exceeded"
    assert gw.calls == []
    assert _counts(project) == (0, 0)


def test_paid_dimension_probe_is_metered_and_receipt_names_real_cost(project):
    """An admitted remote probe cannot masquerade as a free local metadata op."""
    probe_cost = 0.0017
    project.set_provider_key(
        provider="openrouter",
        encrypted=encrypt_secret("sk-project-openrouter"),
        hint="sk-p***",
        spend_cap_micro=1_000_000,
    )
    gw = FakeProbeGateway(
        dim=_PROBE_DIM,
        credential_source="project_key",
        provider_cost_usd=probe_cost,
    )

    res = _run(
        project,
        _create_action(project._sheet_id, key="cr_paid_probe@1"),
        gateway=gw,
    )
    assert res.status == "completed", res.errors
    assert len(gw.calls) == 1

    receipt = ReceiptStore(project).parsed_by_id(str(res.receipt_id))
    assert receipt is not None
    [provider_use] = receipt.provider_use
    assert provider_use["provider"] == "openrouter"
    assert provider_use["external_api"] is True
    assert float(provider_use["cost_actual"]) == pytest.approx(probe_cost)

    assert res.run_id is not None, (
        "the paid probe needs a durable run/model_calls accounting envelope"
    )
    assert receipt.run_id == res.run_id
    run = RunResultStore(project).get_run(int(res.run_id))
    assert run is not None
    assert float(run["cost_actual"]) == pytest.approx(probe_cost)
    [fact] = RunResultStore(project).model_calls(int(res.run_id))
    assert fact["capability"] == "llm.embed"
    assert fact["provider"] == "openrouter"
    assert fact["credential_source"] == "project_key"
    assert float(fact["provider_cost_usd"]) == pytest.approx(probe_cost)
    assert isinstance(fact["attempt_id"], str) and fact["attempt_id"]
    attempt = project.db.execute(
        "SELECT run_id, state FROM execution_attempts WHERE id=?",
        (fact["attempt_id"],),
    ).fetchone()
    assert attempt is not None
    assert (attempt["run_id"], attempt["state"]) == (res.run_id, "effected")
    assert run["current_attempt_id"] is None
    assert any(
        evidence.ref.get("fact_version") == "frisket.model-call-fact.v1"
        and evidence.ref.get("credential_source") == "project_key"
        for evidence in receipt.evidence
    )
    spend = project.provider_spend_state("openrouter")
    assert spend is not None
    assert spend.spent_micro == 1_700


def test_paid_probe_retry_after_metadata_failure_does_not_call_provider_twice(
    project, monkeypatch
):
    """An exact retry reconciles the paid probe instead of buying it again."""
    probe_cost = 0.0017
    project.set_provider_key(
        provider="openrouter",
        encrypted=encrypt_secret("sk-project-openrouter"),
        hint="sk-p***",
        spend_cap_micro=1_000_000,
    )
    gw = FakeProbeGateway(
        dim=_PROBE_DIM,
        credential_source="project_key",
        provider_cost_usd=probe_cost,
    )
    original_create_space = EmbeddingStore.create_space
    failed_once = False

    def fail_first_metadata_write(self, *args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("injected metadata write failure after paid probe")
        return original_create_space(self, *args, **kwargs)

    monkeypatch.setattr(EmbeddingStore, "create_space", fail_first_metadata_write)
    action = _create_action(project._sheet_id, key="cr_probe_reconcile@1")

    first = _run(project, action, gateway=gw)
    assert first.status == "failed"
    assert first.errors[0].code == "project_write_failed"
    assert len(gw.calls) == 1
    [first_fact] = RunResultStore(project).model_calls()
    first_run_id = int(first_fact["run_id"])
    assert float(first_fact["provider_cost_usd"]) == pytest.approx(probe_cost)
    assert project.provider_spend_state("openrouter").spent_micro == 1_700
    assert _counts(project) == (0, 0)
    checkpoint_row = ReceiptStore(project).find_by_idempotency_key(
        "cr_probe_reconcile@1"
    )
    assert checkpoint_row is not None
    assert checkpoint_row.status == "running"
    checkpoint_receipt = checkpoint_row.parsed()
    assert checkpoint_receipt.run_id == first_run_id
    assert any(
        item.ref.get("kind") == "embedding_dimension_probe_checkpoint"
        for item in checkpoint_receipt.evidence
    )
    assert float(checkpoint_receipt.provider_use[0]["cost_actual"]) == pytest.approx(
        probe_cost
    )
    assert RunResultStore(project).get_run(first_run_id)["status"] == "failed"

    retried = _run(project, action, gateway=gw)
    assert retried.status == "completed", retried.errors
    assert retried.run_id == first_run_id
    assert retried.receipt_id == checkpoint_receipt.receipt_id
    assert len(gw.calls) == 1, "the durable first probe must be reused"
    assert len(RunResultStore(project).model_calls()) == 1
    assert project.provider_spend_state("openrouter").spent_micro == 1_700
    assert _counts(project) == (1, 1)
    receipt = ReceiptStore(project).parsed_by_id(str(retried.receipt_id))
    assert receipt is not None
    assert receipt.status == "completed"
    assert receipt.run_id == first_run_id
    assert float(receipt.provider_use[0]["cost_actual"]) == pytest.approx(probe_cost)


def test_create_receipt_records_requested_model_and_probe_facts(project):
    gw = FakeProbeGateway(dim=_PROBE_DIM)
    res = _run(project, _create_action(project._sheet_id), gateway=gw)
    assert res.status == "completed", res.errors
    ref = res.outputs[0].ref
    assert ref["requested_model"] == _OPENROUTER_MODEL
    assert ref["dimension"] == _PROBE_DIM
    assert ref["probe_used"] is True
    assert ref["probe_input_count"] == 1
    # actual_model_id comes from the probe meta, not the requested alias.
    assert ref["actual_model_id"] == f"resolved/{_OPENROUTER_MODEL}"

    # The persisted receipt carries the same probe facts.
    row = project.db.execute(
        "SELECT body FROM receipts WHERE action_kind='embedding.index_create'"
    ).fetchone()
    import json

    body = json.loads(row["body"])
    out_ref = body["outputs"][0]["ref"]
    assert out_ref["requested_model"] == _OPENROUTER_MODEL
    assert out_ref["probe_used"] is True


def test_non_remote_uncurated_id_unsupported(project):
    gw = FakeProbeGateway()
    res = _run(
        project,
        _create_action(
            project._sheet_id,
            provider=None,
            model="totally/not-a-real-model",
            key="cr_unsup@1",
        ),
        gateway=gw,
    )
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_model_unsupported"
    assert gw.calls == []
    assert _counts(project) == (0, 0)


def test_probe_failure_is_typed_and_writes_no_space(project):
    gw = FakeProbeGateway(fail=True)
    action = _create_action(project._sheet_id, key="cr_fail@1")
    res = _run(
        project,
        action,
        gateway=gw,
    )
    assert res.status == "failed"
    assert res.errors[0].code == "embedding_probe_failed"
    assert res.errors[0].details["reconciliation_required"] is True
    # the probe WAS attempted (gate passed), but no space/index persisted.
    assert len(gw.calls) == 1
    assert _counts(project) == (0, 0)

    # EmbeddingProviderError is post-dispatch ambiguous: the provider may have
    # accepted and charged the request before its response failed.  Preserve the
    # direct-effect authority and reservation so an exact retry cannot buy it
    # again, even after the generic running-receipt stale window.
    reservation = ReceiptStore(project).find_by_idempotency_key("cr_fail@1")
    assert reservation is not None
    assert reservation.status == "running"
    assert reservation.run_id is not None
    run = RunResultStore(project).get_run(reservation.run_id)
    assert run is not None
    assert run["status"] == "running"
    attempt = project.db.execute(
        "SELECT state FROM execution_attempts WHERE id=?",
        (run["current_attempt_id"],),
    ).fetchone()
    assert attempt is not None
    assert attempt["state"] == "dispatching"

    project.db.execute(
        "UPDATE receipts SET created_at=datetime('now', '-2 hours') WHERE id=?",
        (reservation.id,),
    )
    project.db.commit()
    retried = _run(project, action, gateway=gw)
    assert retried.status == "failed"
    assert retried.errors[0].code == "external_effect_reconciliation_required"
    assert retried.errors[0].details["reconciliation_required"] is True
    assert len(gw.calls) == 1
    assert _counts(project) == (0, 0)


# --------------------------------------------------------------------------
# provider catalog honesty
# --------------------------------------------------------------------------


def test_catalog_marks_openrouter_available_when_configured():
    payload = embedding_provider_catalog_payload(
        router=_OpenRouterRouter(), env={}, local_available=True, modality="text"
    )
    orouter = [p for p in payload["providers"] if p["provider_id"] == "openrouter"]
    assert orouter
    assert any(p["available"] for p in orouter)
    # discovery-required entries do NOT invent a dimension.
    for p in orouter:
        assert p["dimensions"] is None


def test_catalog_marks_openrouter_disabled_when_not_configured():
    payload = embedding_provider_catalog_payload(
        router=_NoKeyRouter(), env={}, local_available=True, modality="text"
    )
    orouter = [p for p in payload["providers"] if p["provider_id"] == "openrouter"]
    assert orouter
    assert all(not p["available"] for p in orouter)
    assert all(p["disabled_reason"] for p in orouter)


def test_refresh_resends_requested_model_and_stores_probed_width(project):
    # Review coverage gap: prove the discovery-minted index REFRESHES with the SAME
    # model it was probed/minted against (refresh re-sends space.actual_model_id), and
    # the stored vectors are the probed width (refresh's width check accepts them).
    gw = FakeProbeGateway(dim=_PROBE_DIM)
    create = _run(
        project, _create_action(project._sheet_id, key="cr_rf_create@1"), gateway=gw
    )
    assert create.status == "completed", create.errors
    index_id = create.outputs[0].ref["index_id"]
    refresh = _run(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "cr_rf_refresh@1",
        },
        gateway=gw,
    )
    assert refresh.status == "completed", refresh.errors
    # the refresh embed calls (everything after the one-shot dimension probe) re-sent
    # the requested OpenRouter model verbatim
    refresh_calls = [c for c in gw.calls if c[0] != ["dimension probe"]]
    assert refresh_calls, "refresh should have embedded the rows"
    for _texts, provider, model, _modality in refresh_calls:
        assert provider == "openrouter"
        assert model == _OPENROUTER_MODEL
    # all 3 rows are ready -> the probed-width vectors passed the space width check
    # (embedding_items lives in the sidecar VectorBackend db, not project.db)
    from frisket.ai.embeddings import VectorBackend

    backend = VectorBackend(project)
    try:
        ready = backend.db.execute(
            "SELECT COUNT(*) AS n FROM embedding_items WHERE status='ready'"
        ).fetchone()["n"]
    finally:
        backend.close()
    assert ready == 3


def test_paid_refresh_receipt_names_real_provider_cost(project):
    """A metered remote refresh receipt must agree with its provider fact."""
    refresh_cost = 0.0023
    gw = FakeProbeGateway(dim=_PROBE_DIM, provider_cost_usd=refresh_cost)
    create = _run(
        project,
        _create_action(project._sheet_id, key="cr_cost_create@1"),
        gateway=gw,
    )
    assert create.status == "completed", create.errors

    refresh = _run(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {
                "index_id": create.outputs[0].ref["index_id"],
                "mode": "full",
            },
            "idempotency_key": "cr_cost_refresh@1",
        },
        gateway=gw,
    )
    assert refresh.status == "completed", refresh.errors

    receipt = ReceiptStore(project).parsed_by_id(str(refresh.receipt_id))
    assert receipt is not None
    [provider_use] = receipt.provider_use
    assert provider_use["external_api"] is True
    assert float(provider_use["cost_actual"]) == pytest.approx(refresh_cost)
    assert refresh.run_id is not None
    run = RunResultStore(project).get_run(int(refresh.run_id))
    assert run is not None
    assert float(run["cost_actual"]) == pytest.approx(refresh_cost)
