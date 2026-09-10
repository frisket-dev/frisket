"""The consent quote keeps provider cost neutral and binds only rated fields.

Dispatch recomputes neutral facts while reading the rated projection from the
consent record; tests cover both tampering directions so verification cannot
degrade into comparing the record against itself.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.runner.confirmation_context import (
    ConsentQuote,
    ConsentQuoteRefused,
    quote_facts,
    quoted_usd,
    rate_estimate,
)
from frisket.execution.pricing_policy import (
    IDENTITY_PRICING_POLICY,
    PricingPolicyConflict,
    _reset_pricing_policy_for_tests,
    default_pricing_policy,
    install_pricing_policy,
    PricingPolicy,
    QuoteFacts,
    Rating,
    RatedQuote,
    Unpriceable,
    usd_to_micros,
)
from frisket.server.app import create_app
from http_test_helpers import drain_queue


@pytest.fixture(autouse=True)
def _isolate_installed_pricing_policy():
    """The installed policy is PROCESS-scoped, so it has to be reset around
    every test or one install would silently re-price every later suite.
    Mirrors ``_reset_lease_state_for_tests``, the repo's existing convention
    for process-scoped state."""
    _reset_pricing_policy_for_tests()
    yield
    _reset_pricing_policy_for_tests()


class _CostPlusPolicy:
    """2x the provider's cost — a hosted lane, in miniature.

    An arbitrary but fixed multiplier, shaped like a real cost-plus tariff,
    so the assertions below are about the seam rather than about arithmetic
    base owns.
    """

    policy_id = "test.cost_plus.v1"

    def rate(self, facts: QuoteFacts) -> Rating:
        if facts.provider_cost is None:
            return Unpriceable(reason="no provider cost", policy_id=self.policy_id)
        return RatedQuote(
            billed_cost=facts.provider_cost * 2,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


def test_the_stand_in_policy_satisfies_the_port() -> None:
    assert isinstance(_CostPlusPolicy(), PricingPolicy)
    assert isinstance(IDENTITY_PRICING_POLICY, PricingPolicy)


# The journalist in the product sentence: quoted a provider cost of $0.11,
# billed $0.21 by a hosted deployment (2x here, rounded by the policy).
PROVIDER_COST_USD = 0.11

_ESTIMATE: dict[str, Any] = {
    "rows": 4,
    "cost": PROVIDER_COST_USD,
    "cost_source": "estimated",
    "pricing_key": "openai/gpt-4o-mini.tokens",
    "engine": "openai/gpt-4o-mini",
}


def test_a_markup_never_lands_on_the_legacy_cost_field() -> None:
    """THE wire invariant, as an assertion rather than a sentence.

    If a future policy (or a well-meaning refactor) wrote its billed figure
    back onto ``cost``, every downstream reader of the neutral provider fact
    would silently start reading a marked-up one. The cloud settlement path
    reads ``cost`` and would mark it up AGAIN: $0.11 quoted, $0.42 billed.
    """
    rated = rate_estimate(_ESTIMATE, policy=_CostPlusPolicy())

    assert rated["cost"] == PROVIDER_COST_USD, (
        "the markup reached `cost`; that field is the PROVIDER's number "
        "forever and a consumer that marks it up would double-charge"
    )
    assert rated["billed_cost"] == 220_000
    assert rated["policy_id"] == "test.cost_plus.v1"
    assert {k: v for k, v in rated.items() if k in _ESTIMATE} == _ESTIMATE


def test_the_identity_policy_bills_exactly_the_provider_cost() -> None:
    rated = rate_estimate(_ESTIMATE, policy=IDENTITY_PRICING_POLICY)
    assert rated["cost"] == PROVIDER_COST_USD
    assert rated["billed_cost"] == usd_to_micros(PROVIDER_COST_USD) == 110_000


def test_rate_provenance_survives_policy_and_quote_projection_by_value() -> None:
    estimate = {
        **_ESTIMATE,
        "cost_source": "pricing_data",
        "pricing_key": "openai/gpt-4o-mini.tokens",
    }
    rated = rate_estimate(estimate, policy=_CostPlusPolicy())
    facts = quote_facts(rated)
    quote = ConsentQuote.from_estimate(rated)

    assert facts.cost_source == quote.cost_source == "pricing_data"
    assert facts.pricing_key == quote.pricing_key == "openai/gpt-4o-mini.tokens"
    assert rated["cost_source"] == estimate["cost_source"]
    assert rated["pricing_key"] == estimate["pricing_key"]


def test_the_identity_policy_never_returns_the_cost_plus_lane() -> None:
    """Base owns the neutral policy port; a deployment owns commercial lanes.

    The invariant is one sentence — *no open module computes a markup* — and
    this is the mechanical half of it. A markup added to base's policy would
    have to declare its lane, and declaring it reds here.
    """
    shapes = [
        QuoteFacts(
            provider_cost=cost,
            cost_source=source,
            pricing_key=key,
            engine="e",
            rows=1,
        )
        for cost, source, key in (
            (0, "free_local", None),
            (0, "invalid_engine", None),
            (110_000, "estimated", "openai/gpt-4o-mini.tokens"),
            (30_000, "pricing_data", "test.synthetic.audio_minute"),
            (None, "unknown", None),
        )
    ]
    ratings = [IDENTITY_PRICING_POLICY.rate(facts) for facts in shapes]
    priced = [r for r in ratings if isinstance(r, RatedQuote)]
    lanes = {r.lane for r in priced}
    assert "cost_plus" not in lanes
    assert lanes == {"free", "byok"}
    assert all(r.billed_cost == r.provider_cost for r in priced)


def test_an_unknown_cost_is_unpriceable_not_free() -> None:
    """``Unpriceable`` is a type, not a zero. A run whose cost cannot be
    determined is not a free run — folding it in at 0 is the
    ``cost_actual REAL NOT NULL DEFAULT 0`` defect verbatim."""
    rating = IDENTITY_PRICING_POLICY.rate(quote_facts({"cost": None}))
    assert isinstance(rating, Unpriceable)
    rated = rate_estimate(
        {"cost": None, "cost_source": "unknown"}, policy=_CostPlusPolicy()
    )
    assert rated["billed_cost"] is None
    # ...and it still names a policy, so it is an unpriceable QUOTE rather
    # than an unrated one.
    assert rated["policy_id"] == "test.cost_plus.v1"


def test_micros_go_through_decimal_not_binary_float() -> None:
    """``0.0075 * 1_000_000`` is not exactly 7500 in binary floating point,
    and a money figure that is about to be compared for equality inside a
    consent hash cannot be rounded by whichever way the last bit fell."""
    assert usd_to_micros(0.0075) == 7500
    assert usd_to_micros(0.11) == 110_000
    assert usd_to_micros(0.07) == 70_000
    assert usd_to_micros("0.000001") == 1
    with pytest.raises(ValueError):
        usd_to_micros(float("nan"))


class _StubAdapter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def complete(self, req, client):  # noqa: ANN001
        self.calls.append(req)
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


def _classify_action(sheet_id: int) -> dict:
    """A typed paid map request. Consent is never inside ``params``: the only
    way to authorize it is the top-level ``confirmation`` echo of the quote."""
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["text"],
            "engine": "llm",
            "model": "anthropic/claude-opus-4-8",
            "context": "Classify each row.",
            "fields": [{"name": "relevance", "type": "score", "description": "0-10"}],
        },
        "output_names": {},
        "idempotency_key": "billed-cost-seam@sha256:test",
    }


def _gated_project(tmp_path, monkeypatch, *, name: str):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    adapter = _StubAdapter()
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "text", type="text")
    project.add_rows(sheet_id, [{"text": "A" * 800}], {"text": column_id})
    return adapter, client, project_id, project, sheet_id


def _confirm_and_enqueue(client, project_id: str, action: dict) -> tuple[int, str]:
    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    details = gated.json()["errors"][0]["details"]
    confirmed = deepcopy(action)
    confirmed["confirmation"] = details["promise_set_hash"]
    accepted = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert accepted.status_code == 200, accepted.text
    return int(accepted.json()["run_id"]), accepted.json()["receipt_id"]


def _consent_rows(project) -> list[Any]:
    return project.db.execute(
        "SELECT id, quote_json FROM consents WHERE subject_kind='run'"
    ).fetchall()


def test_the_402_quotes_the_billed_figure_and_persists_it(
    tmp_path, monkeypatch
) -> None:
    """The user is shown a rated quote, and the consent row records it.

    Before this wave only the HASH was persisted and dispatch recomputed the
    whole quote live — which cannot work the moment a rated field enters it,
    because the policy that produced the figure is a deployment's and is not
    wired into dispatch.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Billed quote"
    )
    action = _classify_action(sheet_id)

    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    estimate = gated.json()["errors"][0]["details"]["estimate"]
    # The 402 payload carries the rated figure additively, beside the
    # untouched provider `cost`.
    assert estimate["policy_id"] == IDENTITY_PRICING_POLICY.policy_id
    assert estimate["billed_cost"] == usd_to_micros(estimate["cost"])

    _confirm_and_enqueue(client, project_id, action)

    rows = _consent_rows(project)
    assert len(rows) == 1, "the confirmed 402 must record exactly one consent"
    stored = json.loads(rows[0]["quote_json"])
    quote = ConsentQuote.model_validate(stored)
    assert quote.policy_id == IDENTITY_PRICING_POLICY.policy_id
    assert quote.billed_cost == estimate["billed_cost"]
    assert quote.cost == estimate["cost"]

    drain_queue(client, worker_id="billed-quote")
    assert len(adapter.calls) == 1, "the consented run must still dispatch"


@pytest.mark.parametrize(
    "foreign_principal",
    ["deployment:restored-installation", "deployment:restored-installation:user:42"],
)
def test_queued_consent_from_another_installation_cannot_dispatch(
    tmp_path, monkeypatch, foreign_principal
):
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Foreign queued consent"
    )
    run_id, receipt_id = _confirm_and_enqueue(
        client, project_id, _classify_action(sheet_id)
    )
    # A portable run and its exact consent agree with each other, but their
    # recorded actor belongs to the installation that authored the bundle.
    project.db.execute(
        "UPDATE runs SET consent_principal=? WHERE id=?", (foreign_principal, run_id)
    )
    project.db.execute(
        "UPDATE consents SET actor=? WHERE subject_kind='run' AND subject_id=?",
        (foreign_principal, str(run_id)),
    )
    project.db.commit()

    drain_queue(client, worker_id="foreign-consent")

    assert adapter.calls == []
    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "consent_missing"


def test_severing_the_persisted_quote_refuses_the_dispatch(
    tmp_path, monkeypatch
) -> None:
    """THE fence for the read half of the split.

    Cut the persisted quote out from under a valid consent and dispatch must
    refuse ``consent_missing`` with nothing spent — not fall back to an
    unrated hash, and not admit on the strength of the neutral facts alone.
    An unverifiable consent authorizes nothing.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Severed quote"
    )
    action = _classify_action(sheet_id)
    run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    project.db.execute("UPDATE consents SET quote_json=NULL")
    project.db.commit()

    drain_queue(client, worker_id="severed-quote")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert {
        "run_status": run_row["status"],
        "provider_calls": len(adapter.calls),
        "receipt_status": receipt["status"],
        "error_code": receipt["errors"][0]["code"],
    } == {
        "run_status": "failed",
        "provider_calls": 0,
        "receipt_status": "failed",
        "error_code": "consent_missing",
    }


def test_a_malformed_persisted_quote_refuses_the_dispatch(
    tmp_path, monkeypatch
) -> None:
    """Same modeled refusal for a record that is present but not a quote —
    never a default rating, and never an escaping exception (dispatch's only
    modeled outcome is refuse-and-re-confirm)."""
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Malformed quote"
    )
    action = _classify_action(sheet_id)
    _run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    project.db.execute("UPDATE consents SET quote_json=?", ('{"cost": "banana"}',))
    project.db.commit()

    drain_queue(client, worker_id="malformed-quote")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "consent_missing"
    assert len(adapter.calls) == 0


def test_a_truncated_quote_blob_refuses_instead_of_escaping(
    tmp_path, monkeypatch
) -> None:
    """A corrupt blob is the modeled refusal, not a raw decode error.

    ``RouteStore.consents()`` is called by the dispatch fence BEFORE its
    normalizing try block, so a ``quote_json`` decoded eagerly in the store
    would raise ``JSONDecodeError`` straight out of admission and become a
    generic job retry — a paid run left flapping instead of a durable
    "re-confirm this". The column is therefore handed back as raw text and
    decoded inside the per-candidate guard.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Truncated quote"
    )
    action = _classify_action(sheet_id)
    _run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    (row,) = _consent_rows(project)
    truncated = row["quote_json"][: len(row["quote_json"]) // 2]
    project.db.execute(
        "UPDATE consents SET quote_json=? WHERE id=?", (truncated, row["id"])
    )
    project.db.commit()

    # The store itself must survive reading it — the decode is the reader's.
    from frisket.engine.store.execution_routes import RouteStore

    assert RouteStore.for_run(project, _run_id).consents()[0].quote_json == truncated

    drain_queue(client, worker_id="truncated-quote")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "consent_missing"
    assert len(adapter.calls) == 0


def test_one_corrupt_consent_does_not_block_a_sibling_that_proves(
    tmp_path, monkeypatch
) -> None:
    """Per-candidate, not run-fatal.

    A run can carry more than one consent row. A corrupt quote on one of them
    means THAT row proves nothing; it must not veto a well-formed sibling that
    reproduces the hash, or a single bad blob would strand an otherwise
    properly consented run forever.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Corrupt sibling"
    )
    action = _classify_action(sheet_id)
    run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    (row,) = _consent_rows(project)
    # A corrupt DUPLICATE of the real consent, ordered ahead of it.
    project.db.execute(
        "INSERT INTO consents (id, subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "granted_at, grant_basis, quote_json) "
        "SELECT 'consent_corrupt', subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "'1970-01-01T00:00:00+00:00', grant_basis, '{not json' "
        "FROM consents WHERE id=?",
        (row["id"],),
    )
    project.db.commit()
    assert len(_consent_rows(project)) == 2

    drain_queue(client, worker_id="corrupt-sibling")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "completed", receipt
    assert len(adapter.calls) == 1


def test_a_poisoned_rated_field_does_not_deny_a_valid_sibling(
    tmp_path, monkeypatch
) -> None:
    """A record that decodes but REFUSES at the money projection.

    ``billed_cost: -1`` passes JSON and passes pydantic, then refuses inside
    ``_quote_micros`` — so the failure lands at hash-mint time, not at decode
    time. With the mint outside the per-candidate guard that
    ``ConsentQuoteRefused`` escaped the whole loop, and one poisoned row denied
    a run that its valid sibling consent authorized.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Poisoned rating"
    )
    action = _classify_action(sheet_id)
    _run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    (row,) = _consent_rows(project)
    poisoned = json.loads(row["quote_json"])
    poisoned["billed_cost"] = -1
    project.db.execute(
        "INSERT INTO consents (id, subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "granted_at, grant_basis, quote_json) "
        "SELECT 'consent_poisoned', subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "'1970-01-01T00:00:00+00:00', grant_basis, ? "
        "FROM consents WHERE id=?",
        (json.dumps(poisoned), row["id"]),
    )
    project.db.commit()

    drain_queue(client, worker_id="poisoned-rating")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "completed", receipt
    assert len(adapter.calls) == 1


def test_a_live_estimate_that_cannot_project_still_refuses_by_name(
    tmp_path, monkeypatch
) -> None:
    """The other half of the same guard, and the reason it discriminates.

    A refusal on a RATED key belongs to one record. A refusal on any other key
    is the LIVE estimate failing to project, which is run-fatal and must reach
    the caller so the operator is told WHICH fact broke — swallowing it
    per-candidate would turn a precise diagnosis into "no consent matched".
    """
    from frisket.engine.runner.confirmation_context import (
        BILLED_COST_KEY,
        POLICY_ID_KEY,
        ConsentQuoteRefused,
    )

    assert BILLED_COST_KEY in ("billed_cost",)
    assert POLICY_ID_KEY in ("policy_id",)
    assert ConsentQuoteRefused("cost", "is not finite").key == "cost"
    assert ConsentQuoteRefused(BILLED_COST_KEY, "is negative").key == BILLED_COST_KEY


def test_a_tampered_billed_figure_refuses_the_dispatch(tmp_path, monkeypatch) -> None:
    """The rated half is READ at dispatch, so it had better be bound.

    Raising the persisted billed figure changes the hash the reconstruction
    produces, so it no longer equals the digest the user's approval recorded.
    Without this, a deployment could persist $0.11, dispatch under a rewritten
    $0.21, and the fence would agree.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Tampered bill"
    )
    action = _classify_action(sheet_id)
    _run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    (row,) = _consent_rows(project)
    quote = json.loads(row["quote_json"])
    quote["billed_cost"] = int(quote["billed_cost"] or 0) + 100_000
    project.db.execute(
        "UPDATE consents SET quote_json=? WHERE id=?", (json.dumps(quote), row["id"])
    )
    project.db.commit()

    drain_queue(client, worker_id="tampered-bill")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "consent_missing"
    assert len(adapter.calls) == 0


def test_a_tampered_neutral_fact_in_the_record_is_ignored(
    tmp_path, monkeypatch
) -> None:
    """The split is SURGICAL, from the other side.

    Dispatch reads ``billed_cost`` and ``policy_id`` off the consent and
    RECOMPUTES everything else. So rewriting a neutral field (``cost``) in the
    persisted record changes nothing: the reconstruction never consults it,
    the live facts still match what was approved, and the run dispatches.

    That is the property that keeps the verification from being circular. If
    dispatch read the whole persisted quote instead, it would be comparing the
    record against itself and every drift check would evaporate — the sibling
    regression in ``tests/server/test_horizontal_consent_identity_regressions``
    (edit the source cell, get ``consent_missing``) is the same claim from the
    live-facts side, and both must hold at once.
    """
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Tampered neutral"
    )
    action = _classify_action(sheet_id)
    _run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    (row,) = _consent_rows(project)
    quote = json.loads(row["quote_json"])
    quote["cost"] = (quote["cost"] or 0.0) + 5.0
    quote["rows"] = int(quote["rows"] or 0) + 99
    project.db.execute(
        "UPDATE consents SET quote_json=? WHERE id=?", (json.dumps(quote), row["id"])
    )
    project.db.commit()

    drain_queue(client, worker_id="tampered-neutral")

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()
    assert receipt["status"] == "completed", receipt
    assert len(adapter.calls) == 1


def test_a_declined_price_gates_as_unknown_not_as_the_provider_cost() -> None:
    """THE fence for the seam's worst admission bug.

    A policy that returned ``Unpriceable`` said it cannot bill from this run.
    If the selector fell back to the provider cost there, that run would be
    compared against the threshold using a number the deployment explicitly
    refused to bill from — a $0.50 provider cost would admit as "under the
    $1.00 gate" and execute with no confirmation at all, while the deployment
    had no idea what it was going to charge.

    ``policy_id`` plus an explicitly present ``billed_cost`` is what makes
    "declined" distinguishable from "nobody asked"; that is why both facts
    are required at this boundary.
    """
    declined = {"cost": 0.5, "policy_id": "acme.v1", "billed_cost": None}
    assert quoted_usd(declined) is None

    # ...and it is genuinely a different state from an old-shape envelope
    # beside it. Every current producer rates before a 402; falling back here
    # would turn a provider figure into a billed quote without an authority.
    with pytest.raises(ConsentQuoteRefused, match="policy_id"):
        quoted_usd({"cost": 0.5})
    with pytest.raises(ConsentQuoteRefused, match="billed_cost"):
        quoted_usd({"cost": 0.5, "policy_id": "acme.v1"})
    assert (
        quoted_usd({"cost": 0.5, "policy_id": "acme.v1", "billed_cost": 1_000_000})
        == 1.0
    )


class _DecliningPolicy:
    """A deployment that refuses to price anything — the Unpriceable lane."""

    policy_id = "test.declines.v1"

    def rate(self, facts: QuoteFacts) -> Rating:
        return Unpriceable(reason="tariff unavailable", policy_id=self.policy_id)


def test_a_declining_policy_forces_the_confirm_on_an_otherwise_cheap_run(
    tmp_path, monkeypatch
) -> None:
    """End to end: a run whose provider cost is far under the gate still 402s
    when the installed policy declines to price it, and the copy says the cost
    is unknown rather than quoting a number nobody will bill."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "1000")
    install_pricing_policy(_DecliningPolicy())
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Declining policy"
    )
    gated = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=_classify_action(sheet_id)
    )
    assert gated.status_code == 402, gated.text
    error = gated.json()["errors"][0]
    assert error["details"]["estimate"]["billed_cost"] is None
    assert error["details"]["estimate"]["policy_id"] == "test.declines.v1"
    assert "unknown" in error["message"].lower()
    assert len(adapter.calls) == 0


def test_default_is_identity_until_a_deployment_installs_one() -> None:
    assert default_pricing_policy() is IDENTITY_PRICING_POLICY
    install_pricing_policy(_CostPlusPolicy())
    assert default_pricing_policy().policy_id == "test.cost_plus.v1"


def test_installing_the_same_policy_twice_is_not_a_conflict() -> None:
    """A host that builds several apps in one process is not two components
    fighting over the tariff."""
    install_pricing_policy(_CostPlusPolicy())
    install_pricing_policy(_CostPlusPolicy())
    assert default_pricing_policy().policy_id == "test.cost_plus.v1"


def test_a_second_different_policy_refuses_by_name() -> None:
    """One process, one tariff. Two price lists in one process means the
    figure a user was quoted and the figure a later gate mints come off
    different books, and the consent hash silently stops matching — so the
    conflict is refused at install time, naming both."""
    install_pricing_policy(_CostPlusPolicy())
    with pytest.raises(PricingPolicyConflict) as caught:
        install_pricing_policy(_DecliningPolicy())
    assert "test.cost_plus.v1" in str(caught.value)
    assert "test.declines.v1" in str(caught.value)


def test_a_policy_that_does_not_satisfy_the_port_refuses_at_install() -> None:
    """Fail closed at startup, not at the first 402."""

    class _NotAPolicy:
        pass

    with pytest.raises(TypeError):
        install_pricing_policy(_NotAPolicy())  # type: ignore[arg-type]

    class _Unnamed:
        policy_id = ""

        def rate(self, facts: QuoteFacts) -> Rating:  # pragma: no cover
            raise AssertionError

    with pytest.raises(ValueError):
        install_pricing_policy(_Unnamed())


def test_an_installed_policy_is_what_every_lane_actually_rates_through(
    tmp_path, monkeypatch
) -> None:
    """THE closure proof for the injection seam.

    The echo-gate closure test already reds any family that skips rating. This
    proves the complementary half: that what the rostered mint sites rate
    THROUGH is the installed policy, not a hardcoded identity. Drive one
    RECIPE-lane gate (map.classify, via MapRunner/validate_spec) and rate one
    generic metered unit estimate through the shared ``rate_estimate`` site
    (the FAMILY lane's one rating call) with a 2x policy installed, and
    require both to bill double.

    Without a real injection seam both of these would quietly bill 1x and the
    hosted wave would have nothing to call.
    """
    install_pricing_policy(_CostPlusPolicy())
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Installed policy"
    )

    gated = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=_classify_action(sheet_id)
    )
    assert gated.status_code == 402, gated.text
    recipe_estimate = gated.json()["errors"][0]["details"]["estimate"]
    assert recipe_estimate["policy_id"] == "test.cost_plus.v1"
    assert recipe_estimate["billed_cost"] == usd_to_micros(recipe_estimate["cost"]) * 2
    assert recipe_estimate["cost"] == pytest.approx(recipe_estimate["cost"])

    from frisket.engine.runner.confirmation_context import rate_estimate as _rate
    from frisket.ops.cost_source import cost_estimate

    # The generic unit every family rates through: a real metered estimate
    # (a priced rate over ``rows`` units) built by the shared constructor,
    # not a hand-typed envelope for a family that no longer prices anything
    # (refresh is deterministic and rates nothing).
    family_estimate = _rate(
        cost_estimate(
            cost=0.42,
            cost_source="pricing_data",
            engine="anthropic/claude-haiku-4-5",
            pricing_key="anthropic/claude-haiku-4-5",
            rows=3,
        ),
        policy=default_pricing_policy(),
    )
    assert family_estimate["policy_id"] == "test.cost_plus.v1"
    assert family_estimate["billed_cost"] == 840_000
    assert family_estimate["cost"] == 0.42
    assert family_estimate["cost_source"] == "pricing_data"
    assert len(adapter.calls) == 0


#: The modules that MINT a money confirmation, RENDER one, or HANDLE one for
#: admission. Asserted below to equal the set of files that either call
#: ``action_confirmation(`` / ``recipe_confirmation(`` — the seam's own two
#: money mints — or handle a gate's ``estimate_details`` payload. Rendering a
#: gate for a client and adjudicating a caught gate are both handling; every
#: side is derived, so this is not a list somebody has to remember to extend.
#:
#: Minting alone was too narrow, and the gap was live: the MCP renderer
#: (``server/mcp/backends.py``) mints nothing, and it re-read ``cost`` off the
#: details to fill the ``estimate``/``estimate_known`` fields its own tool
#: docstring tells agents to read — publishing the provider's number under a
#: deployment tariff, and a confident number for a price the policy declined.
#:
#: Derived from the MINT rather than from ``CostGate(``: ``group_summary``
#: gates by building a needs_confirmation ActionResult directly and constructs
#: no ``CostGate`` at all, so a roster derived from the gate constructor would
#: have silently dropped a family that rates money.
#:
#: ``sheet_refresh`` is NOT here: supported refreshes are deterministic and
#: price no historical model work, so the module mints no money quote. Its
#: only gate is the join fan-out cap, a scope confirmation owned by
#: ``joined_tables_read`` (see ``_SCOPE_GATE_MODULES``).
_GATE_MODULES = (
    "src/frisket/engine/runner/validation.py",
    "src/frisket/engine/executor/action_lifecycle.py",
    "src/frisket/engine/executor/action_support.py",
    "src/frisket/engine/executor/find_action.py",
    "src/frisket/engine/executor/group_summary_action.py",
    "src/frisket/engine/executor/run_backfill_action.py",
    "src/frisket/sdk/maprunner.py",
    "src/frisket/server/mcp/backends.py",
    "src/frisket/server/services/action_preview_runs.py",
    "src/frisket/server/services/action_previews.py",
    "src/frisket/engine/runner/preview.py",
)

#: Scope gates — ``derive.join``'s fan-out cap and
#: ``derive.collection_expand``'s preview cap. They mint a confirmation but
#: have NO price at all (``ScopeConfirmationContext`` has no ``quote`` field,
#: precisely so a cost sentinel cannot appear in a gate that was never about
#: cost), so they are correctly outside the roster. Asserted, not assumed.
_SCOPE_GATE_MODULES = (
    "src/frisket/engine/executor/joined_tables_read.py",
    "src/frisket/engine/executor/collection_read.py",
)

_MONEY_MINT = re.compile(r"\b(?:action|recipe)_confirmation\(")
_SCOPE_MINT = re.compile(r"\bscope_confirmation\(")


def _handles_a_money_gate(source: str) -> bool:
    """Does this module handle a money gate's ``estimate_details`` payload?

    That payload is the mapping a family attaches to a needs_confirmation
    result. A renderer reads it to produce the figure a human or agent
    approves; preview admission reads its typed ``ConsentQuote`` projection to
    decide whether a caught gate is the one safe exception. Detected through
    the AST, like the raw-cost read below and for the same reason — the MCP tool
    docstring *describes* ``estimate_details`` in prose, and a roster that reds
    on prose would roster the module that documents the field instead of the
    modules that actually render or handle it.
    """
    import ast

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == "estimate_details":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "estimate_details":
            return True
        if isinstance(node, ast.keyword) and node.arg == "estimate_details":
            return True
        if isinstance(node, ast.arg) and node.arg == "estimate_details":
            return True
        if isinstance(node, ast.Constant) and node.value == "estimate_details":
            return True
    return False


#: A base expression whose ``["cost"]`` is an ESTIMATE's provider cost. Scoped
#: deliberately: ``metadata.get("cost")`` in the plugin family reads a MANIFEST
#: declaration (``{"kind": "external_metered"}``), a different fact entirely,
#: and flagging it would teach the next reader the wrong lesson.
_ESTIMATE_NAME = re.compile(r"(?:^|[._])(?:est|estimate)s?(?:$|[._\[])|[Ee]stimate\b")


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _raw_estimate_cost_reads(source: str) -> list[tuple[int, str]]:
    """Every read of an estimate's raw ``cost``, via AST.

    AST rather than a line regex because the first version of this closure
    matched a COMMENT that quoted the old code — a fence that reds on prose is
    worse than no fence. The tree carries no comments or docstring bodies, so
    what it finds is what actually executes.
    """
    import ast

    tree = ast.parse(source)
    lines = source.splitlines()
    hits: list[tuple[int, str]] = []

    def base_is_estimate(node) -> bool:
        segment = ast.get_source_segment(source, node) or ""
        return bool(_ESTIMATE_NAME.search(segment))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "cost"
            and base_is_estimate(node.value)
        ):
            hits.append((node.lineno, lines[node.lineno - 1].strip()))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "cost"
            and base_is_estimate(node.func.value)
        ):
            hits.append((node.lineno, lines[node.lineno - 1].strip()))
    return hits


def test_the_gate_roster_is_derived_not_remembered() -> None:
    """``_GATE_MODULES`` is the money-gate mint/render/handle set.

    The previous version of this closure carried a hand-written tuple under a
    docstring claiming it was derived. A new family minting a money quote in a
    file nobody added would simply not be checked — the failure mode the
    closure exists to prevent, reintroduced one level up. The version after
    that derived only the MINTS, and a renderer that read the provider cost
    back off the details went unchecked for the same reason.
    """
    root = _repo_root()
    found = set()
    for path in (root / "src" / "frisket").rglob("*.py"):
        # The module that DEFINES the mints is not a caller of them.
        if path.name == "confirmation_context.py":
            continue
        # rule19: closure fence — money-gate roster two-source diff
        source = path.read_text()
        if _MONEY_MINT.search(source) or _handles_a_money_gate(source):
            found.add(str(path.relative_to(root)).replace("\\", "/"))
    assert found == set(_GATE_MODULES), (
        "the money-gate roster drifted from the code: "
        f"unrostered={sorted(found - set(_GATE_MODULES))}, "
        f"stale={sorted(set(_GATE_MODULES) - found)}"
    )


def test_scope_gates_are_excluded_because_they_have_no_price() -> None:
    """WHY the two derive families are outside the roster, asserted.

    They mint a SCOPE confirmation — a cap on how much work runs — and have no
    cost at all. Encoding the reason means that if either ever starts minting a
    money confirmation, this reds and forces the decision rather than leaving a
    silent hole in the roster.
    """
    root = _repo_root()
    for rel in _SCOPE_GATE_MODULES:
        # rule19: closure fence — roster-exclusion honesty
        source = (root / rel).read_text()
        assert not _MONEY_MINT.search(source) and not _handles_a_money_gate(source), (
            f"{rel} now mints, renders, or handles a money confirmation; add it to "
            "_GATE_MODULES (and make it read through quoted_usd)"
        )
        assert _SCOPE_MINT.search(source), (
            f"{rel} no longer mints a scope confirmation, so the reason it is "
            "excluded from the money-gate roster no longer holds"
        )


def test_no_gate_module_reads_the_raw_provider_cost() -> None:
    """The mechanical half of "the gate speaks the BILLED figure".

    Two sites shipped speaking provider cost after the seam landed —
    ``action_lifecycle``'s child-sheet threshold comparison and ``research``'s
    gate message — and both were invisible under the identity policy, where
    the two numbers are equal. Only a deployment with a real tariff would have
    noticed, which is the worst possible time.

    A third shipped in the MCP renderer, which is why the roster now covers
    modules that RENDER a gate as well as those that mint one: it overwrote
    the billed scalar its caller passed with the details' ``cost``, and under
    a declining policy published that figure as a KNOWN estimate.

    So this reds on the SHAPE rather than on a behaviour that identity hides,
    and it reds on the READ rather than on the use: an intermediary variable
    (``quoted = estimate["cost"]``), a ``.get("cost")``, a single-quoted key
    and a renamed threshold all reach a gate decision just as well as a call
    argument does, and the first version of this closure caught only the last
    — which the FIXED code would itself have walked straight past.
    """
    root = _repo_root()
    offenders = [
        f"{rel}:{line_no} {text}"
        for rel in _GATE_MODULES
        # rule19: closure fence — raw provider-cost READ shape invisible under the identity policy
        for line_no, text in _raw_estimate_cost_reads((root / rel).read_text())
    ]
    assert not offenders, (
        "a money-gate module is reading the PROVIDER cost off an estimate; "
        "use confirmation_context.quoted_usd, which is identical under the "
        "identity policy and correct under a cost-plus one:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "shape",
    [
        'gate = CostGate(estimate["cost"], estimate_details=e)',
        'quoted = estimate["cost"]',
        "quoted = estimate.get('cost')",
        "if est['cost'] is None or est['cost'] > threshold_usd:\n    pass",
        'requires = backfill_est["cost"] > _cost_gate_usd()',
        'x = v1Estimate["cost"]',
    ],
)
def test_the_closure_would_notice_each_forbidden_shape(shape: str) -> None:
    """Non-vacuity, one case per shape the closure claims to catch — including
    the intermediary-variable form the FIXED code itself uses, the ``.get``
    form, single-quoted keys and a renamed threshold."""
    assert _raw_estimate_cost_reads(shape), shape


def test_the_gate_handler_detector_reads_code_not_prose() -> None:
    """Non-vacuity for the roster's render/handle half, and the reason
    ``mcp/server.py`` is outside it: that module names ``estimate_details`` in
    the tool docstring an agent reads and never touches the payload."""
    assert _handles_a_money_gate('details["estimate"] = exc.estimate_details')
    assert _handles_a_money_gate("def gate(*, estimate_details=None): pass")
    assert _handles_a_money_gate("estimate_details = dict(payload)")
    assert not _handles_a_money_gate(
        '"""you get {estimate, estimate_known, estimate_details} instead."""'
    )


def test_typed_consent_quote_preserves_each_exact_zero_safety_fact() -> None:
    """The preview allowance can inspect three independent typed facts.

    This guards the non-vacuity of the safe shape: provider zero is not a
    substitute for billed zero, and neither is a substitute for the explicit
    ``free_local`` source. The production handler must project through
    ``ConsentQuote.from_estimate`` and read these attributes, never re-read the
    raw mapping's provider ``cost``.
    """
    quote = ConsentQuote.from_estimate(
        {
            "rows": 1,
            "cost": 0,
            "cost_source": "free_local",
            "billed_cost": 0,
            "policy_id": "frisket.pricing.identity.v1",
        }
    )

    assert quote.cost == 0.0
    assert quote.billed_cost == 0
    assert quote.cost_source == "free_local"
    assert not _raw_estimate_cost_reads(
        "quote = ConsentQuote.from_estimate(estimate)\n"
        "safe = (quote.cost == 0 and quote.billed_cost == 0 "
        'and quote.cost_source == "free_local")'
    )


@pytest.mark.parametrize(
    "shape",
    [
        "quoted = quoted_usd(estimate)",
        "gate = CostGate(quoted, estimate_details=estimate)",
        "billed = estimate.get(BILLED_COST_KEY)",
        # The plugin family's manifest read — a DIFFERENT fact, and flagging it
        # would teach the next reader the wrong lesson.
        'cost = metadata.get("cost")',
    ],
)
def test_the_closure_does_not_flag_a_correct_shape(shape: str) -> None:
    assert not _raw_estimate_cost_reads(shape), shape
