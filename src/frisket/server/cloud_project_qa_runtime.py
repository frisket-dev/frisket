"""Solo Project Ask binding for an exact account-authorized Cloud model."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.authoring.project_ask import default_project_ask_model
from frisket.cloud_execution import (
    CloudExecutionRefused,
    CloudExecutionTransportProvider,
    CloudQuoteAuthority,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.cloud_targets import CLOUD_LLM_TRANSPORT, cloud_fallback_is_blocked
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.execution.pricing_policy import usd_to_micros
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.targets import CAPABILITY_LLM
from frisket.server.thread_worker import await_thread_worker

if TYPE_CHECKING:
    from frisket.server.workspace import Workspace


class SoloCloudProjectQATurnRuntime:
    """One Ask turn's optional Cloud checkpoint coordinator."""

    def __init__(
        self, coordinator: CloudLLMCheckpointCoordinator | None = None
    ) -> None:
        self.cloud_llm_coordinator = coordinator

    def call_scope(self, _router: Any) -> AbstractContextManager[None]:
        return nullcontext()

    def settle_call(self, *, project: Any, turn_id: str, call_id: str) -> None:
        del project, turn_id, call_id

    async def aclose(self) -> None:
        return None


class SoloCloudProjectQARuntimePort:
    """Resolve an Ask turn through the same composition as other Solo work."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def prepare_turn(
        self,
        *,
        project: Any,
        project_id: str,
        turn_id: str,
        actor: str | None,
    ) -> SoloCloudProjectQATurnRuntime:
        del project_id, actor
        return await await_thread_worker(self._prepare_turn, project, turn_id)

    def _prepare_turn(
        self, project: Any, turn_id: str
    ) -> SoloCloudProjectQATurnRuntime:
        turn = ProjectQAStore(project).get_turn(turn_id)
        router = self._workspace.router_for(project)
        model = turn["model"] or default_project_ask_model(router)
        composition = self._workspace.execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(),
        )
        provider = composition.provider_for_resolution()
        outcome = resolve(
            ResolutionRequest(
                engine=model,
                capability=CAPABILITY_LLM,
                options={
                    "tools": True,
                    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                },
            ),
            provider,
            composition.facts,
        )
        if isinstance(outcome, Refusal):
            if outcome.target_id and (
                outcome.target_id.startswith("frisket-cloud:")
                or cloud_fallback_is_blocked(
                    composition,
                    target_id=outcome.target_id,
                    capability=CAPABILITY_LLM,
                    engine=model,
                )
            ):
                raise CloudExecutionRefused("cloud_route_unavailable", outcome.remedy)
            return SoloCloudProjectQATurnRuntime()

        payer = outcome.facts.cloud_payer
        if payer is None:
            return SoloCloudProjectQATurnRuntime()
        if outcome.support.transport != CLOUD_LLM_TRANSPORT:
            raise CloudExecutionRefused(
                "cloud_route_invalid", "The selected Cloud model route is invalid"
            )
        if not isinstance(provider, CloudExecutionTransportProvider):
            raise CloudExecutionRefused(
                "cloud_transport_unavailable", "Frisket Cloud is unavailable"
            )
        transport = provider.cloud_execution_transport(outcome.target.id)
        if transport is None:
            raise CloudExecutionRefused(
                "cloud_transport_unavailable", "Frisket Cloud is unavailable"
            )
        research = turn.get("research")
        maximum_charge_micro = (
            research["budget_micros"]
            if isinstance(research, dict)
            and isinstance(research.get("budget_micros"), int)
            else usd_to_micros(self._workspace.cost_preapproval_usd)
        )
        coordinator = CloudLLMCheckpointCoordinator(
            project,
            transport,
            payer=payer,
            quote_authority=CloudQuoteAuthority(maximum_charge_micro),
            group_key=f"ask:{turn_id}",
            action_kind="project.ask",
            claimless_direct_effect=True,
        )
        return SoloCloudProjectQATurnRuntime(coordinator)


__all__ = ["SoloCloudProjectQARuntimePort", "SoloCloudProjectQATurnRuntime"]
