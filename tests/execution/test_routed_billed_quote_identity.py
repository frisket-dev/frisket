"""A routed consent's identity includes the billed quote, not just provider cost."""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from decimal import Decimal
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import validation
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.pricing_policy import QuoteFacts, RatedQuote
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import (
    action_identity_hash,
    consented_set_hash,
    exact_match_consent_covers,
    resolve_for_action,
)
from frisket.execution.resolver import ResolvedExecution
from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)


class _CountingTariff:
    def __init__(self, policy_id: str, multiplier: int) -> None:
        self.policy_id = policy_id
        self.multiplier = multiplier
        self.facts: list[QuoteFacts] = []

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        self.facts.append(facts)
        assert facts.provider_cost is not None
        return RatedQuote(
            billed_cost=facts.provider_cost * self.multiplier,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.fixture()
def routed_audio_project(tmp_path: Path):
    project = Project.create(tmp_path / "routed-audio.frisket")
    sheet_id = project.add_sheet("audio")
    column_id = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="clip.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10.64, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="clip.wav")}],
        {"media": column_id},
    )
    try:
        yield project, sheet_id
    finally:
        project.close()


def _spec(sheet_id: int) -> dict:
    return transcription_spec(sheet_id, engine="openai/whisper-1")


def _cost_promise(resolved: ResolvedExecution):
    return next(
        promise for promise in resolved.promise_set.promises if promise.field == "cost"
    )


def _quote_promise(resolved: ResolvedExecution):
    return next(
        promise
        for promise in resolved.promise_set.promises
        if "consent_quote_json" in (promise.basis or {})
    )


def test_tariff_drift_invalidates_exact_match_without_rewriting_provider_basis(
    routed_audio_project,
) -> None:
    """Cross-seam pin: rating changes consent identity, not settlement facts.

    A deployment can change its tariff while the provider's SKU and cost stay
    identical. The old exact-match consent must then stop covering, while the
    provider basis/value reconstructed by settlement remain byte-for-byte the
    same. Each estimate asks its installed policy exactly once.
    """
    project, sheet_id = routed_audio_project
    spec = _spec(sheet_id)
    router = ModelRouter(keys={"openai": "k"}, cache=None, cache_mode="off")
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    resolved = resolve_for_action(
        project,
        spec,
        transcription_program(spec),
        composition=composition,
        persistence="ephemeral",
    )
    assert isinstance(resolved, ResolvedExecution)
    assert resolved.resolution.target.id == "remote-api:openai"
    coverage = ConsentCoverage(instance_principal(project), Decimal("0"))

    first_policy = _CountingTariff("test.tariff.v1", 2)
    first_estimate = validation.estimate_run(
        project,
        spec,
        composition=composition,
        program=transcription_program(spec),
        pricing_policy=first_policy,
        resolution=resolved,
        consent_coverage=coverage,
    )
    first_bound = validation._bind_rated_quote(resolved, first_estimate)
    first_hash = consented_set_hash(first_bound.promise_set)
    assert first_estimate["promise_set_hash"] == first_hash
    assert len(first_policy.facts) == 1

    RouteStore.for_run(project, 7).record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=first_hash,
        actor=instance_principal(project),
    )
    assert exact_match_consent_covers(project, spec, first_bound.promise_set)

    second_policy = _CountingTariff("test.tariff.v2", 4)
    second_estimate = validation.estimate_run(
        project,
        spec,
        composition=composition,
        program=transcription_program(spec),
        pricing_policy=second_policy,
        resolution=resolved,
        consent_coverage=coverage,
    )
    second_bound = validation._bind_rated_quote(resolved, second_estimate)
    second_hash = consented_set_hash(second_bound.promise_set)

    assert len(second_policy.facts) == 1
    assert first_estimate["cost"] == second_estimate["cost"] == 0.001064
    assert first_estimate["billed_cost"] == 2_128
    assert second_estimate["billed_cost"] == 4_256
    assert first_hash != second_hash
    assert not exact_match_consent_covers(project, spec, second_bound.promise_set)

    original_cost = _cost_promise(resolved)
    first_cost = _cost_promise(first_bound)
    second_cost = _cost_promise(second_bound)
    assert first_cost.basis == second_cost.basis == original_cost.basis
    assert first_cost.value == second_cost.value == original_cost.value

    first_quote_promise = _quote_promise(first_bound)
    second_quote_promise = _quote_promise(second_bound)
    assert first_quote_promise.field == second_quote_promise.field == "egress_class"
    first_quote = json.loads(first_quote_promise.basis["consent_quote_json"])
    second_quote = json.loads(second_quote_promise.basis["consent_quote_json"])
    assert first_quote["policy_id"] == "test.tariff.v1"
    assert second_quote["policy_id"] == "test.tariff.v2"
