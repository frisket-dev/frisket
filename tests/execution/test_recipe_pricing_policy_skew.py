"""A consent minted under one tariff cannot authorize dispatch under another.

The recipe lane's sibling of ``tests/authoring/test_paid_plugin_pricing_policy_skew``.
There the app quotes and a worker claims, two processes, and the envelope
carries the policy id across; here one deployment quotes, persists the consent,
and dispatches later — but the rated half of that consent (``billed_cost``,
``policy_id``) is read BACK off the record at dispatch, because the policy that
produced it belongs to the deployment and is deliberately not wired into
dispatch.

That read is what made the skew invisible. A consent always reproduces its own
persisted rating, so a run quoted at 2x re-verified perfectly against a
deployment that had since installed a 4x tariff — it dispatched, and settlement
billed a figure the journalist never saw. The hash cannot notice: it is stable
by construction. So the fence is an explicit comparison beside it, and the
refusal names both price lists, because "re-run the action" is the remedy and
"your rows changed" is not what happened.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.execution.pricing_policy import (
    QuoteFacts,
    Rating,
    RatedQuote,
    Unpriceable,
    _reset_pricing_policy_for_tests,
    install_pricing_policy,
)
from http_test_helpers import drain_queue
from test_billed_cost_consent_seam import (
    _classify_action,
    _confirm_and_enqueue,
    _consent_rows,
    _gated_project,
)


@pytest.fixture(autouse=True)
def _isolate_installed_pricing_policy():
    _reset_pricing_policy_for_tests()
    yield
    _reset_pricing_policy_for_tests()


class _Tariff:
    """A deployment tariff: ``multiplier`` x the provider's cost."""

    def __init__(self, policy_id: str, multiplier: int) -> None:
        self.policy_id = policy_id
        self.multiplier = multiplier

    def rate(self, facts: QuoteFacts) -> Rating:
        if facts.provider_cost is None:
            return Unpriceable(reason="no provider cost", policy_id=self.policy_id)
        return RatedQuote(
            billed_cost=facts.provider_cost * self.multiplier,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


IDENTITY_POLICY_ID = "frisket.pricing.identity.v1"


def _receipt(client, project_id: str, receipt_id: str) -> dict[str, Any]:
    return client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    ).json()


def _reprice(policy) -> None:
    """What a deployment actually does between the two moments: a release
    rolls out, the composition root installs a different tariff."""
    _reset_pricing_policy_for_tests()
    if policy is not None:
        install_pricing_policy(policy)


def test_a_reinstalled_tariff_refuses_the_dispatch_and_names_both_policies(
    tmp_path, monkeypatch
) -> None:
    """THE demonstrated scenario, end to end.

    Quote and confirm under a 2x tariff, re-price the deployment at 4x, then
    drain the queue. Before the fence this run COMPLETED: the candidate hash is
    rebuilt from the consent's own persisted rating, so it matched, and the
    journalist's $0.22 approval authorized a $0.44 run.
    """
    install_pricing_policy(_Tariff("test.tariff.v1", 2))
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Tariff skew"
    )
    action = _classify_action(sheet_id)
    run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)
    quoted = json.loads(_consent_rows(project)[0]["quote_json"])
    assert quoted["policy_id"] == "test.tariff.v1"

    _reprice(_Tariff("test.tariff.v2", 4))
    drain_queue(client, worker_id="tariff-skew")

    receipt = _receipt(client, project_id, receipt_id)
    error = receipt["errors"][0]
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert {
        "run_status": run_row["status"],
        "provider_calls": len(adapter.calls),
        "receipt_status": receipt["status"],
        "error_code": error["code"],
    } == {
        "run_status": "failed",
        "provider_calls": 0,
        "receipt_status": "failed",
        "error_code": "consent_missing",
    }
    # Both price lists, in the message — the operator cannot fix a tariff skew
    # by re-running the same work, and "your rows changed" would send them to
    # look for a change that never happened.
    assert "test.tariff.v1" in error["message"]
    assert "test.tariff.v2" in error["message"]
    assert "pricing policy" in error["message"]


def test_a_withdrawn_tariff_refuses_too_and_names_the_identity_policy(
    tmp_path, monkeypatch
) -> None:
    """The other direction, and the one a rollback actually produces: quoted
    under a tariff, dispatched by a process that installs none. Failing closed
    matters here specifically — the identity policy bills LESS, so the
    tempting answer is "close enough", and it would mean dispatching work
    whose approved figure nobody in this process can reproduce."""
    install_pricing_policy(_Tariff("test.tariff.v1", 2))
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Withdrawn tariff"
    )
    _run_id, receipt_id = _confirm_and_enqueue(
        client, project_id, _classify_action(sheet_id)
    )

    _reprice(None)
    drain_queue(client, worker_id="withdrawn-tariff")

    error = _receipt(client, project_id, receipt_id)["errors"][0]
    assert error["code"] == "consent_missing"
    assert "test.tariff.v1" in error["message"]
    assert IDENTITY_POLICY_ID in error["message"]
    assert len(adapter.calls) == 0


def test_a_sibling_quoted_under_the_current_policy_still_proves(
    tmp_path, monkeypatch
) -> None:
    """The fence is PER CANDIDATE, like every other unusable-record skip here.

    A run can carry more than one consent row. A stale one quoted under a
    retired tariff proves nothing; it must not veto the sibling that was
    quoted under the policy this process installs, or one leftover row would
    strand a properly consented run forever.
    """
    install_pricing_policy(_Tariff("test.tariff.v1", 2))
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Stale sibling"
    )
    _run_id, receipt_id = _confirm_and_enqueue(
        client, project_id, _classify_action(sheet_id)
    )

    (row,) = _consent_rows(project)
    stale = json.loads(row["quote_json"])
    stale["policy_id"] = "test.tariff.v0"
    # Ordered AHEAD of the real consent, so a run-fatal fence would fire first.
    project.db.execute(
        "INSERT INTO consents (id, subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "granted_at, grant_basis, quote_json) "
        "SELECT 'consent_stale_tariff', subject_kind, subject_id, "
        "action_identity_hash, promise_set_hash, standing_policy, actor, "
        "'1970-01-01T00:00:00+00:00', grant_basis, ? "
        "FROM consents WHERE id=?",
        (json.dumps(stale), row["id"]),
    )
    project.db.commit()
    assert len(_consent_rows(project)) == 2

    drain_queue(client, worker_id="stale-sibling")

    receipt = _receipt(client, project_id, receipt_id)
    assert receipt["status"] == "completed", receipt
    assert len(adapter.calls) == 1


def test_the_same_tariff_still_dispatches(tmp_path, monkeypatch) -> None:
    """Non-vacuous: with the deployment on the tariff it quoted under, the
    fence is silent and the consented run dispatches. Without this, every
    assertion above would pass against a check that refused unconditionally."""
    install_pricing_policy(_Tariff("test.tariff.v1", 2))
    adapter, client, project_id, project, sheet_id = _gated_project(
        tmp_path, monkeypatch, name="Same tariff"
    )
    _run_id, receipt_id = _confirm_and_enqueue(
        client, project_id, _classify_action(sheet_id)
    )

    # A second instance of the SAME price list, as an app and a worker each
    # constructing their own — the id is the identity, so this is not a skew.
    _reprice(_Tariff("test.tariff.v1", 2))
    drain_queue(client, worker_id="same-tariff")

    receipt = _receipt(client, project_id, receipt_id)
    assert receipt["status"] == "completed", receipt
    assert len(adapter.calls) == 1
