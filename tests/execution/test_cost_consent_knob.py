"""FRISKET_COST_CONSENT_USD, the explicit-confirm-above-$X knob, is the
standing-consent MINT input.

The ``cost_under_gate_v1`` standing consent row is the recorded artifact AND
the live authority: coverage (validation and worker alike) reads the
threshold persisted on the head standing consent row
(``consents.policy_params_json``), never the env var. The knob takes effect
through the reconcile-at-open path — ``ensure_standing_cost_consent`` mints
a SUCCESSOR standing consent when the env value differs from the persisted
head — so these tests set env AND reconcile, then assert:

- ``0``   -> always-confirm: even a sub-$1 commercial bound gates with a
             cost claim;
- ``100`` -> a $5 bound is covered by the standing consent (no cost claim;
             the egress claim still gates — the knob is a COST knob only);
- an env change WITHOUT a reconcile changes NOTHING (the persisted
  artifact, not the env, is the authority — F2's reproducibility).

Both directions run through the real ``validate_spec`` gate against a real
project (whose migration minted the standing consent).
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from dataclasses import replace
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import validation
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.price_book import PlatformMetered, cost_posture_for
from frisket.execution.pricing_policy import default_pricing_policy
from frisket.execution.provider import (
    CompositionFacts,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import (
    STANDING_COST_POLICY,
    ConsentRegistry,
    ensure_standing_cost_consent,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)


@pytest.fixture()
def audio_project(tmp_path: Path, monkeypatch):
    # The project is created (and the standing consent minted) at the
    # documented default threshold.
    monkeypatch.delenv("FRISKET_COST_CONSENT_USD", raising=False)
    project = Project.create(tmp_path / "p.frisket")
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt knob",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"media": col},
    )
    try:
        yield project, sheet_id
    finally:
        project.close()


@pytest.fixture()
def gateway_env(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")


def _spec(sheet_id: int, **overrides):
    return transcription_spec(
        sheet_id, **{"engine": "parakeet-tdt", "diarize": True, **overrides}
    )


def _gate_claim_fields(project, spec, *, unit_rate: str = "0.000164") -> list[str]:
    router = ModelRouter()
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    funding = PlatformMetered()
    offering = CommercialOffering(
        match=CommercialOfferingMatch(
            target_id="models-gateway",
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.gateway.audio_second",
            terms_version="test.gateway.terms.v1",
            unit_rate=unit_rate,
            quantity_unit="audio_second",
            rounding=CommercialQuoteRounding("half_even", 3),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="1",
            ceiling_mode="none",
            row_settlement_mode="all_metered",
        ),
        charge_authority="test.gateway.authority",
        presentation=CommercialPresentation(
            venue_label="Test gateway",
            billing_label="Test usage billing",
        ),
    )
    composition = replace(
        composition,
        facts=CompositionFacts(edition="hosted", funding=funding),
        credential_use_context=CredentialUseContext(
            cost_posture=cost_posture_for(funding)
        ),
        offerings=(offering,),
    )
    try:
        validation.validate_spec(
            project,
            router,
            RunResultStore(project),
            spec,
            program=transcription_program(spec),
            confirmed=False,
            resume_run_id=None,
            pricing_policy=default_pricing_policy(),
            composition=composition,
        )
    except ClaimsGate as exc:
        return sorted(claim["field"] for claim in exc.claims)
    return []


def test_standing_consent_artifact_exists_with_recorded_threshold(audio_project):
    project, _sheet_id = audio_project
    standing = ConsentRegistry(project).standing_consents()
    assert [c.standing_policy for c in standing] == [STANDING_COST_POLICY]
    assert standing[0].policy_params["threshold_usd"] == "2"
    assert standing[0].policy_params["currency"] == "USD"


def test_default_threshold_covers_the_sub_dollar_bound(audio_project, gateway_env):
    """The default $2 preapproval covers the quoted call and its egress."""
    project, sheet_id = audio_project
    assert _gate_claim_fields(project, _spec(sheet_id)) == []


def test_knob_zero_gates_after_reconcile(audio_project, gateway_env, monkeypatch):
    """0 = always-confirm, effective through the reconcile-at-open mint:
    the successor standing consent records threshold 0, and the same
    sub-$1 bound becomes an uncovered user claim."""
    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    ensure_standing_cost_consent(project)  # what project open runs
    assert _gate_claim_fields(project, _spec(sheet_id)) == [
        "cost",
        "egress_class",
    ]


def test_knob_100_covers_a_five_dollar_bound_after_reconcile(
    audio_project, gateway_env, monkeypatch
):
    """A $5 call gates at $2 and is preapproved at $100."""
    project, sheet_id = audio_project
    assert _gate_claim_fields(project, _spec(sheet_id), unit_rate="2.5") == [
        "cost",
        "egress_class",
    ], "a $5 bound must gate at the default $2 threshold"

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    ensure_standing_cost_consent(project)
    assert _gate_claim_fields(project, _spec(sheet_id), unit_rate="2.5") == []
    # Auditable trail: the $1 generation is history, not overwritten.
    thresholds = [
        c.policy_params["threshold_usd"]
        for c in ConsentRegistry(project).standing_consents()
    ]
    assert sorted(thresholds) == ["100", "2"]


def test_new_request_uses_changed_preapproval_without_project_reconcile(
    audio_project, gateway_env, monkeypatch
):
    """New request coverage follows the effective installation preference."""
    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    assert _gate_claim_fields(project, _spec(sheet_id), unit_rate="2.5") == []


def test_mint_input_helper_reads_the_live_knob(monkeypatch):
    """The legacy (non-resolution) gates and the mint read the knob
    per-call; the documented default and 0-floor semantics hold."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "3")
    assert validation._cost_gate_usd() == 3.0
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    assert validation._cost_gate_usd() == 0.0
    monkeypatch.delenv("FRISKET_COST_CONSENT_USD")
    assert validation._cost_gate_usd() == 2.0
