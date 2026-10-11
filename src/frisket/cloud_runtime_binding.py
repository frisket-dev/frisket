"""Bind one admitted Cloud effect to its runtime transport and authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionTransportProvider,
    CloudLLMExecutionBinding,
    CloudMediaExecutionBinding,
    CloudPayerSubject,
    cloud_quote_authority_for_row,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.engine.store import Project
from frisket.execution.attempt import AttemptCommitment
from frisket.execution.provider import ExecutionComposition
from frisket.ops.base import RecipeInvocationHalt


@dataclass(frozen=True, slots=True)
class CloudRuntimeBinding:
    """Mutually exclusive media or LLM runtime binding for one effect unit."""

    media_binding: CloudMediaExecutionBinding | None
    llm_coordinator: CloudLLMCheckpointCoordinator | None
    route: Any

    def __post_init__(self) -> None:
        if (self.media_binding is None) == (self.llm_coordinator is None):
            raise ValueError(
                "Cloud runtime binding requires exactly one execution mode"
            )

    @property
    def llm_binding(self) -> CloudLLMExecutionBinding | None:
        if self.llm_coordinator is None:
            return None
        return CloudLLMExecutionBinding(coordinator=self.llm_coordinator)


def cloud_runtime_binding(
    project: Project,
    composition: ExecutionComposition,
    attempt: AttemptCommitment,
    *,
    row_id: int,
    request_id: str,
    group_key: str,
    action_kind: str,
    run_id: int | None,
    claim_token: str | None,
    claimless_direct_effect: bool,
) -> CloudRuntimeBinding | None:
    """Build the runtime binding for the Cloud route admitted on ``attempt``.

    A non-Cloud attempt returns ``None``. Once admission names a Cloud payer,
    every runtime fact is checked against that exact route before any adapter
    receives a transport.
    """

    routed = attempt.routed
    if routed is None:
        return None
    snapshot = routed.route.target_snapshot
    payer = routed.binding.facts.cloud_payer
    transport_kind = snapshot.get("transport")
    if payer is None:
        if transport_kind in {"frisket.cloud.media.v1", "frisket.cloud.llm.v1"}:
            raise RecipeInvocationHalt(
                "promise_violation",
                "Cloud execution is missing its admitted payer",
            )
        return None
    if not isinstance(payer, CloudPayerSubject):
        raise RecipeInvocationHalt(
            "promise_violation", "Cloud execution payer has an invalid type"
        )

    target_id = routed.binding.facts.target_id
    engine = routed.binding.facts.engine
    capability = snapshot.get("capability")
    if (
        snapshot.get("target_id") != target_id
        or routed.route.engine != engine
        or not isinstance(capability, str)
    ):
        raise RecipeInvocationHalt(
            "promise_violation", "Cloud execution differs from its admitted route"
        )
    if (
        composition.cloud_payer_for(
            target_id=target_id, capability=capability, engine=engine
        )
        != payer
    ):
        raise RecipeInvocationHalt(
            "promise_violation", "Cloud execution payer differs from its admission"
        )
    provider = composition.provider
    if not isinstance(provider, CloudExecutionTransportProvider):
        raise RecipeInvocationHalt(
            "promise_violation", "Cloud execution transport is unavailable"
        )
    transport = provider.cloud_execution_transport(target_id)
    if transport is None:
        raise RecipeInvocationHalt(
            "promise_violation", "Cloud execution transport is unavailable"
        )
    quote = cloud_quote_authority_for_row(attempt.cost_basis, row_id=row_id)

    if transport_kind == "frisket.cloud.media.v1" and capability in {
        "ocr",
        "transcribe",
        "document.convert",
    }:
        return CloudRuntimeBinding(
            media_binding=CloudMediaExecutionBinding(
                transport,
                CloudExecutionAttempt(
                    request_id=request_id,
                    payer=payer,
                    quote_authority=quote,
                ),
                capability,
            ),
            llm_coordinator=None,
            route=routed.route,
        )
    if transport_kind == "frisket.cloud.llm.v1" and capability == "llm.complete":
        return CloudRuntimeBinding(
            media_binding=None,
            llm_coordinator=CloudLLMCheckpointCoordinator(
                project,
                transport,
                payer=payer,
                quote_authority=quote,
                group_key=group_key,
                action_kind=action_kind,
                run_id=run_id,
                writer_attempt_id=attempt.attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
            ),
            route=routed.route,
        )
    raise RecipeInvocationHalt(
        "promise_violation", "Cloud execution differs from its admitted route"
    )


__all__ = ["CloudRuntimeBinding", "cloud_runtime_binding"]
