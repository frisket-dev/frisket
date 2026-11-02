"""Execution-target provider seam.

The protocol keeps one target inventory and separates static selection from
liveness: enumerate code/registry-owned target rows; report configuration
presence and payer/venue rank without probing; then deref secret connection
material + liveness for ONE selected target by id. ``resolve``
(execution/resolver.py) is pure over this protocol. Recipes and dispatch
receive a provider via deps/ctx and never import an implementation directly.

One OPTIONAL duck-typed enrichment exists beyond the port (a provider MAY
implement it; ``resolve`` degrades gracefully when absent):

- ``liveness_remedy(target_id, capability=None) -> str | None`` — human
  remediation text for a dead target ("set FRISKET_MODELS_URL …", "download
  the pinned Parakeet artifacts …"). Without it, refusals carry a generic
  remedy. ``capability`` is the work the caller was resolving for, so a
  target row adopted by several capabilities (``remote-api:{provider}``
  serves both transcription and VLM OCR) can name the right one; ``resolve``
  inspects the hook's signature and calls a provider that predates the
  argument the one-argument way, so implementing it stays optional.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

from frisket.execution.commercial import (
    CommercialOffering,
    CommercialPresentation,
    commercial_offering_allowed,
)
from frisket.execution.price_book import (
    Funding,
    OperatorBorne,
    PlatformMetered,
    cost_posture_for,
)
from frisket.execution.credential_use import (
    ActionCredentialResolver,
    CredentialUseContext,
)
from frisket.execution.targets import (
    EXECUTION_CAPABILITIES,
    ExecutionTarget,
    TargetEngineSupport,
    require_clean,
)
from frisket.project_identity import ProjectStorageKey

if TYPE_CHECKING:
    from frisket.ai.models.gateway_config import ModelsGatewayConnection
    from frisket.engine.jobs.ports import TrustedJobOrg
    from frisket.execution.resolver import ModelRoute, ResolutionRequest

# Composition edition vocabulary. ``CompositionFacts`` makes operator and
# egress *resolved* facts for both open and hosted editions.
Edition = Literal["open", "hosted"]
TargetConfiguration = Literal["configured", "not_configured"]


class TargetConfigurationError(RuntimeError):
    """Static route configuration could not be read safely."""

    def __init__(self, target_id: str, remedy: str) -> None:
        self.target_id = target_id
        self.remedy = remedy
        super().__init__(remedy)


@dataclass(frozen=True)
class ExecutionLimits:
    """Optional, request-scoped resource limits for expensive media work."""

    max_media_seconds: float | None = None
    max_pdf_pages: int | None = None


class ExecutionLimitExceeded(ValueError):
    """A configured execution limit could not be satisfied before dispatch."""

    def __init__(self, limit: str, detail: str | None = None) -> None:
        self.limit = limit
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"execution limit exceeded: {limit}{suffix}")


def enforce_media_duration_limit(maximum: float | None, measured: Any) -> None:
    """Fail closed on an unavailable duration before media work begins."""

    if maximum is None:
        return
    if (
        isinstance(measured, bool)
        or not isinstance(measured, (int, float))
        or not isfinite(measured)
        or measured < 0
    ):
        raise ExecutionLimitExceeded("max_media_seconds", "measurement_unavailable")
    if measured > maximum:
        raise ExecutionLimitExceeded("max_media_seconds")


def enforce_pdf_page_limit(maximum: int | None, measured: Any) -> None:
    """Fail closed on an unavailable page count before PDF work begins."""

    if maximum is None:
        return
    if isinstance(measured, bool) or not isinstance(measured, int) or measured <= 0:
        raise ExecutionLimitExceeded("max_pdf_pages", "measurement_unavailable")
    if measured > maximum:
        raise ExecutionLimitExceeded("max_pdf_pages")


@dataclass(frozen=True)
class ConnectionConfig:
    """Secret connection/dispatch material for one LIVE target.

    ``None`` from ``ExecutionTargetProvider.connection`` means "not live";
    this object means live, and carries exactly what the existing adapters
    need at dispatch. It is never snapshotted or hashed because route
    snapshots are non-secret by contract:

    - ``base_url`` + ``token``: the models-gateway shape
      (``ops/_sidecar.py``: ``FRISKET_MODELS_URL`` + bearer
      ``FRISKET_MODELS_TOKEN``). ``None`` for targets that do not connect
      over an app-held URL (local process, Modal SDK). For
      ``remote-api:{provider}`` targets ``token`` is the provider API key —
      the same value the router's key broker resolves.
    - ``timeout_seconds`` / ``connect_timeout_seconds``: request budget the
      adapter applies (gateway: FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS
      shape; Modal: FRISKET_MODAL_TIMEOUT shape).
    - ``extra``: non-secret string facts the adapter (and the resolver's
      estimate basis) needs — the Modal definition supplies ``account``,
      ``app_name``, ``function_name``, ``gpu`` (hardware class) and
      ``object_store_bucket`` here. Never key material, and never a
      RATE either: ``unit_rate``/``pricing_key``/``quantity_unit`` left this
      mapping because a connection is dispatch material, not a rate card. The
      downstream ports once mirrored only part of those terms, which made
      cost promises ``unevaluable``. Provider-direct estimates live in the
      neutral price book; a platform commercial rate arrives only as an
      exact :class:`CommercialOffering` on the request composition. ``gpu``
      stays: it is the hardware class a provider-direct estimate rates
      against.
    """

    base_url: str | None = None
    # repr=False: a logged/raised ConnectionConfig must never echo the live
    # bearer/API key.
    token: str | None = field(default=None, repr=False)
    timeout_seconds: float | None = None
    connect_timeout_seconds: float | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CompositionFacts:
    """What the composition knows at resolve time.

    The input that makes credential and cost posture composition-resolved.
    Operator and egress remain declarations of the exact selected target: an
    operator-owned Modal target stays ``third_party_api`` in every edition,
    while a downstream shared or dedicated venue supplies its own target row.

    ``org_id`` is always ``None`` in the open composition; the hosted port
    supplies the organization context it actually reads.

    ``funding`` answers the generic credential/posture question: whose key or
    account bears the provider call.  It never creates a commercial offering
    and never selects a platform SKU.  The open composition is
    operator-borne by construction: a self-hosted install bills nobody for a
    platform venue, while provider-direct own-key estimates remain visible.
    """

    edition: Edition = "open"
    org_id: str | None = None  # hosted org context; None in the open edition
    funding: Funding = field(default_factory=OperatorBorne)

    def __post_init__(self) -> None:
        if self.edition == "open" and not isinstance(self.funding, OperatorBorne):
            raise ValueError(
                "the open edition is operator-borne by construction: a "
                "self-hosted install bills nobody, so "
                f"funding={self.funding!r} is not composable with "
                "edition='open'"
            )


class ExecutionTargetProvider(Protocol):
    """The provider port implemented by static and hosted compositions."""

    def targets(self) -> Sequence[ExecutionTarget]: ...

    def configuration(
        self, target_id: str, capability: str, engine: str
    ) -> TargetConfiguration:
        """Return static configuration presence without probing liveness."""
        ...

    def selection_rank(self, target_id: str, capability: str, engine: str) -> int:
        """Return the request's payer/venue preference; lower wins."""
        ...

    def connection(self, target_id: str) -> ConnectionConfig | None:
        """Secret connection material for a live target; ``None`` = not live.

        This is the ONE liveness probe: callers (resolver, worker binding)
        ask about exactly the target they were mapped/pinned to — never a
        scan. Static selection must not probe unrelated targets.
        """
        ...


def _support_matches(
    support: TargetEngineSupport, *, capability: str, engine: str
) -> bool:
    return support.capability == capability and (
        support.engine == engine
        or support.engine.endswith("/*")
        and engine.startswith(support.engine[:-1])
    )


@dataclass(frozen=True)
class _ResolutionTargetProvider:
    """Request view containing only composition-selected target supports."""

    source: ExecutionTargetProvider
    rows: tuple[ExecutionTarget, ...]
    model_routes: Mapping[str, "ModelRoute"] | None = None
    model_route_default: "ModelRoute | None" = None
    cloud_inventory_known: bool = False

    def targets(self) -> tuple[ExecutionTarget, ...]:
        return self.rows

    def model_route(self, engine: str, capability: str) -> "ModelRoute | None":
        if self.model_routes is None:
            return None
        selected = self.model_routes.get(engine)
        if selected is not None:
            return selected
        from frisket.execution.resolver import (
            cloud_route_eligible,
            model_route_for_target,
        )

        cloud_eligible = cloud_route_eligible(engine, capability)
        candidates = [
            target
            for target in self.rows
            if any(
                _support_matches(
                    support,
                    capability=capability,
                    engine=engine,
                )
                for support in target.engines
            )
        ]
        try:
            configured_candidates = [
                target
                for target in candidates
                if self.configuration(target.id, capability, engine) == "configured"
            ]
        except TargetConfigurationError:
            # The ordinary chooser will turn this same read failure into its
            # typed refusal; route policy must not bypass that error.
            return None
        available_routes = {
            model_route_for_target(self, target, capability, engine)
            for target in candidates
        }
        if any(target.egress_class == "none" for target in configured_candidates):
            return "own"
        if self.model_route_default is not None:
            if self.model_route_default != "cloud" or (
                "cloud" in available_routes
                or cloud_eligible
                and not self.cloud_inventory_known
            ):
                return self.model_route_default
        configured_routes = {
            model_route_for_target(self, target, capability, engine)
            for target in configured_candidates
        }
        if "own" in configured_routes:
            return "own"
        if "cloud" in configured_routes:
            return "cloud"
        if "cloud" in available_routes or (
            cloud_eligible and not self.cloud_inventory_known
        ):
            return "cloud"
        return "own" if cloud_eligible and candidates else None

    def configuration(
        self, target_id: str, capability: str, engine: str
    ) -> TargetConfiguration:
        if not any(target.id == target_id for target in self.rows):
            return "not_configured"
        return self.source.configuration(target_id, capability, engine)

    def selection_rank(self, target_id: str, capability: str, engine: str) -> int:
        if not any(target.id == target_id for target in self.rows):
            return 1
        return self.source.selection_rank(target_id, capability, engine)

    def connection(self, target_id: str) -> ConnectionConfig | None:
        if not any(target.id == target_id for target in self.rows):
            return None
        return self.source.connection(target_id)

    def cloud_payer(self, target_id: str, capability: str, engine: str) -> Any | None:
        if not any(target.id == target_id for target in self.rows):
            return None
        hook = getattr(self.source, "cloud_payer", None)
        return hook(target_id, capability, engine) if callable(hook) else None

    def cloud_execution_transport(self, target_id: str) -> Any | None:
        from frisket.cloud_execution import CloudExecutionTransportProvider

        if not any(target.id == target_id for target in self.rows):
            return None
        if not isinstance(self.source, CloudExecutionTransportProvider):
            return None
        return self.source.cloud_execution_transport(target_id)

    def liveness_remedy(
        self, target_id: str, capability: str | None = None
    ) -> str | None:
        hook = getattr(self.source, "liveness_remedy", None)
        if not callable(hook):
            return None
        import inspect

        try:
            accepts_capability = "capability" in inspect.signature(hook).parameters
        except (TypeError, ValueError):
            accepts_capability = False
        return (
            hook(target_id, capability=capability)
            if accepts_capability
            else hook(target_id)
        )


@dataclass(frozen=True)
class ExactExecutionMatch:
    """One exact target/capability/engine included by the composition."""

    target_id: str
    capability: str
    engine: str

    def __post_init__(self) -> None:
        require_clean("included execution target_id", self.target_id)
        require_clean("included execution capability", self.capability)
        require_clean("included execution engine", self.engine)
        if self.capability not in EXECUTION_CAPABILITIES:
            raise ValueError(
                f"unknown included execution capability {self.capability!r}"
            )
        if "*" in self.engine:
            raise ValueError("included execution matches require an exact engine")


@dataclass(frozen=True)
class ExecutionComposition:
    """One request's composition facts and matching target-provider view.

    The two values travel together because they answer one question at
    resolution: *which deployment/funding posture applies, and which targets
    are live under the effective router selected for that same request?*
    Carrying only :class:`CompositionFacts` let hosted funding account ``F``
    reach the cost posture while ``resolve_for_action`` independently rebuilt
    a provider from storage project ``S``.  On ``S != F`` that could select a
    different credential or refuse a target the effective router could use.

    This is intentionally request-scoped value data, not installed process
    state.  An external composition can inject a factory that returns hosted
    facts plus its provider without base learning that composition's types.
    """

    facts: CompositionFacts
    provider: ExecutionTargetProvider
    credential_use_context: CredentialUseContext
    offerings: tuple[CommercialOffering, ...] = field(default=(), kw_only=True)
    included: tuple[ExactExecutionMatch, ...] = field(default=(), kw_only=True)
    limits: ExecutionLimits = field(default_factory=ExecutionLimits, kw_only=True)
    # ``None`` means this composition does not expose per-model route choice
    # (notably native hosted).  An empty mapping means Solo supports the
    # feature but this project has no explicit choices yet.
    model_routes: Mapping[str, "ModelRoute"] | None = field(default=None, kw_only=True)
    model_route_default: "ModelRoute | None" = field(default=None, kw_only=True)
    # True when this request carries the selected Cloud account's current
    # route catalog, including an authoritative empty catalog.
    cloud_inventory_known: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.credential_use_context, CredentialUseContext):
            raise TypeError(
                "execution composition credential context must be CredentialUseContext"
            )
        expected_posture = cost_posture_for(self.facts.funding)
        if self.credential_use_context.cost_posture != expected_posture:
            raise ValueError(
                "execution composition funding and credential context disagree: "
                f"{expected_posture!r} != "
                f"{self.credential_use_context.cost_posture!r}"
            )
        if not isinstance(self.offerings, tuple):
            raise TypeError(
                "execution composition offerings must be an immutable tuple"
            )
        if not isinstance(self.included, tuple):
            raise TypeError("execution composition included must be an immutable tuple")
        if self.model_routes is not None:
            if not isinstance(self.model_routes, Mapping):
                raise TypeError("execution composition model_routes must be a mapping")
            normalized_routes: dict[str, Any] = {}
            for model_id, route in self.model_routes.items():
                require_clean("model route id", model_id)
                if route not in {"cloud", "own"}:
                    raise ValueError("model route must be cloud or own")
                normalized_routes[model_id] = route
            object.__setattr__(
                self, "model_routes", MappingProxyType(normalized_routes)
            )
        if self.model_route_default not in {None, "cloud", "own"}:
            raise ValueError("model route default must be cloud, own, or None")
        if self.model_routes is None and self.model_route_default is not None:
            raise ValueError("model route default requires model route support")
        if not isinstance(self.cloud_inventory_known, bool):
            raise TypeError("cloud inventory known must be bool")
        targets = {target.id: target for target in self.provider.targets()}

        def validate_match(match: Any, *, kind: str) -> tuple[str, str, str]:
            key = (match.target_id, match.capability, match.engine)
            target = targets.get(match.target_id)
            if target is None:
                raise ValueError(
                    f"{kind} target is not supplied by this composition: "
                    f"{match.target_id!r}"
                )
            if not any(
                _support_matches(
                    support,
                    capability=match.capability,
                    engine=match.engine,
                )
                for support in target.engines
            ):
                raise ValueError(
                    f"{kind} does not match an engine supplied by the "
                    f"composition target: {key!r}"
                )
            return key

        seen: set[tuple[str, str, str]] = set()
        for offering in self.offerings:
            if not isinstance(offering, CommercialOffering):
                raise TypeError(
                    "execution composition offerings must contain "
                    "CommercialOffering values"
                )
            match = offering.match
            if (
                self.facts.edition == "open"
                and self.cloud_payer_for(
                    target_id=match.target_id,
                    capability=match.capability,
                    engine=match.engine,
                )
                is None
            ):
                raise ValueError(
                    "open execution compositions cannot supply commercial "
                    "offerings except for an exact account-authorized Cloud target"
                )
            key = validate_match(match, kind="commercial offering")
            if key in seen:
                raise ValueError(f"duplicate commercial offering match {key!r}")
            seen.add(key)
        for match in self.included:
            if not isinstance(match, ExactExecutionMatch):
                raise TypeError(
                    "execution composition included must contain "
                    "ExactExecutionMatch values"
                )
            key = validate_match(match, kind="included execution")
            if key in seen:
                raise ValueError(
                    f"execution match cannot be both offered and included: {key!r}"
                )
            seen.add(key)

    def offering_for(
        self, *, target_id: str, capability: str, engine: str
    ) -> CommercialOffering | None:
        """Return the exact injected offer for a selected target, if any."""
        return next(
            (
                offering
                for offering in self.offerings
                if offering.match.target_id == target_id
                and offering.match.capability == capability
                and offering.match.engine == engine
            ),
            None,
        )

    def cloud_payer_for(
        self, *, target_id: str, capability: str, engine: str
    ) -> Any | None:
        """Return the account payer attached to one exact Cloud route."""

        hook = getattr(self.provider, "cloud_payer", None)
        return hook(target_id, capability, engine) if callable(hook) else None

    def includes_execution_match(
        self, *, target_id: str, capability: str, engine: str
    ) -> bool:
        """Whether the composition includes this exact execution choice."""

        return any(
            match.target_id == target_id
            and match.capability == capability
            and match.engine == engine
            for match in self.included
        )

    def restricted_to(self, match: ExactExecutionMatch) -> "ExecutionComposition":
        """Return the same request world narrowed to one trusted exact target."""

        source = self.provider_for_resolution()
        rows: list[ExecutionTarget] = []
        for target in source.targets():
            if target.id != match.target_id:
                continue
            supports = tuple(
                (
                    replace(support, engine=match.engine)
                    if support.engine.endswith("/*")
                    else support
                )
                for support in target.engines
                if _support_matches(
                    support,
                    capability=match.capability,
                    engine=match.engine,
                )
                and not (
                    support.engine.endswith("/*")
                    and any(
                        exact.capability == match.capability
                        and exact.engine == match.engine
                        for exact in target.engines
                    )
                )
            )
            if supports:
                rows.append(replace(target, engines=supports))
        provider = _ResolutionTargetProvider(source, tuple(rows))
        offerings = (
            tuple(offering for offering in self.offerings if offering.match == match)
            if rows
            else ()
        )
        included = () if offerings or not rows else (match,)
        return replace(
            self,
            provider=provider,
            offerings=offerings,
            included=included,
            model_routes=None,
            model_route_default=None,
        )

    def resolution_targets(self) -> tuple[ExecutionTarget, ...]:
        """Targets that exist for this composition's resolution world.

        Provider-direct/open routes keep the provider's complete roster unless
        an offering declares a target/capability commercial, in which case
        only exact offered engines remain for that group. A platform-metered
        composition likewise has no implicit catalog: only exact offered or
        included engines and known non-commercial free-public APIs remain.
        Wildcard provider supports are narrowed to those exact matches.
        """

        selected: list[ExecutionTarget] = []
        for target in self.provider.targets():
            supports = []
            for support in target.engines:
                capability_offers = tuple(
                    offering
                    for offering in self.offerings
                    if offering.match.target_id == target.id
                    and offering.match.capability == support.capability
                )
                capability_matches = tuple(
                    offering.match for offering in capability_offers
                ) + tuple(
                    match
                    for match in self.included
                    if match.target_id == target.id
                    and match.capability == support.capability
                )
                matching_matches = tuple(
                    match
                    for match in capability_matches
                    if _support_matches(
                        support,
                        capability=match.capability,
                        engine=match.engine,
                    )
                )
                requires_exact_match = bool(capability_offers) or (
                    isinstance(self.facts.funding, PlatformMetered)
                    and commercial_offering_allowed(
                        target_id=target.id,
                        capability=support.capability,
                        engine=support.engine,
                    )
                )
                if not requires_exact_match:
                    supports.append(support)
                    continue
                if support.engine.endswith("/*"):
                    # A wildcard is provider capability, not commercial
                    # availability.  Materialize one exact support per injected
                    # offer so an unoffered concrete engine is absent even from
                    # the resolver's candidate roster.
                    supports.extend(
                        replace(support, engine=match.engine)
                        for match in matching_matches
                        if not any(
                            exact.capability == match.capability
                            and exact.engine == match.engine
                            for exact in target.engines
                        )
                    )
                elif matching_matches:
                    supports.append(support)
            if supports:
                selected.append(replace(target, engines=tuple(supports)))
        return tuple(selected)

    def provider_for_resolution(self) -> ExecutionTargetProvider:
        """The request provider after applying exact composition matches."""

        return _ResolutionTargetProvider(
            self.provider,
            self.resolution_targets(),
            self.model_routes,
            self.model_route_default,
            self.cloud_inventory_known,
        )

    def resolution_request(
        self,
        engine: str,
        *,
        capability: str,
        options: Mapping[str, Any],
        route_override: "ModelRoute | None" = None,
    ) -> "ResolutionRequest":
        """Build one request with this composition's project route choice."""

        from frisket.execution.resolver import (
            ResolutionRequest,
            canonical_model_route_id,
        )

        route = route_override
        if route is None and self.model_routes is not None:
            model_id = canonical_model_route_id(engine, capability)
            if model_id is not None:
                route = self.provider_for_resolution().model_route(model_id, capability)
        return ResolutionRequest(
            engine=engine,
            capability=capability,
            options=options,
            route=route,
        )

    def supplies_resolution_match(
        self, *, target_id: str, capability: str, engine: str
    ) -> bool:
        """Whether an exact pinned choice still exists in this composition.

        Admission asks this independently from connection liveness: the raw
        provider may still know how to connect to a target that the current
        composition deliberately removed. Provider wildcards
        remain capability declarations for non-commercial own-key routes;
        platform wildcards have already been narrowed to exact offered or
        included engines by :meth:`resolution_targets`.
        """

        return any(
            target.id == target_id
            and any(
                _support_matches(
                    support,
                    capability=capability,
                    engine=engine,
                )
                for support in target.engines
            )
            for target in self.resolution_targets()
        )

    def presentation_for(
        self, *, target_id: str, capability: str, engine: str
    ) -> CommercialPresentation | None:
        """Composition-owned display copy for the exact selected target."""
        offering = self.offering_for(
            target_id=target_id,
            capability=capability,
            engine=engine,
        )
        if offering is not None:
            return offering.presentation
        hook = getattr(self.provider, "cloud_presentation", None)
        if callable(hook):
            presentation = hook(target_id, capability, engine)
            if presentation is not None:
                if not isinstance(presentation, CommercialPresentation):
                    raise TypeError(
                        "execution provider cloud_presentation must return "
                        "CommercialPresentation"
                    )
                return presentation
        return None


@dataclass(frozen=True, slots=True)
class ExecutionCompositionContext:
    """Deployment-neutral facts available while composing one execution."""

    storage_key: ProjectStorageKey | None
    run_id: int | None
    trusted_job_org_id: TrustedJobOrg
    edition_snapshot: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if self.storage_key is not None and not isinstance(
            self.storage_key, ProjectStorageKey
        ):
            raise TypeError("storage_key must be a ProjectStorageKey or None")
        if self.run_id is not None and (
            isinstance(self.run_id, bool)
            or not isinstance(self.run_id, int)
            or self.run_id < 1
        ):
            raise ValueError("run_id must be a positive integer or None")

        # Reuse the worker boundary's one validation rule without making the
        # provider module import the jobs package during module initialization.
        from frisket.engine.jobs.ports import JobHandlerContext

        JobHandlerContext(self.trusted_job_org_id)
        if self.edition_snapshot is not None:
            if not isinstance(self.edition_snapshot, Mapping):
                raise TypeError("edition_snapshot must be a mapping or None")
            # The snapshot is opaque to public code, but it is durable JSON.
            # Detach it from mutable request/row objects at the boundary.
            copied = json.loads(
                json.dumps(dict(self.edition_snapshot), sort_keys=True, allow_nan=False)
            )
            object.__setattr__(self, "edition_snapshot", copied)

    @classmethod
    def direct(cls) -> ExecutionCompositionContext:
        """The explicit Local/Team arm for execution without a claimed job."""
        from frisket.engine.jobs.ports import JobHandlerContext

        return cls(
            storage_key=None,
            run_id=None,
            trusted_job_org_id=JobHandlerContext.without_job_row().trusted_job_org_id,
            edition_snapshot=None,
        )


ExecutionCompositionFactory = Callable[
    [Any, Any, ExecutionCompositionContext], ExecutionComposition
]
ExecutionCompositionOverlay = Callable[
    [ExecutionComposition, Any, Any, ExecutionCompositionContext],
    ExecutionComposition,
]


def open_execution_composition(
    project: Any,
    router: Any,
    context: ExecutionCompositionContext,
    *,
    models_gateway_resolver: Callable[[], ModelsGatewayConnection | None] | None = None,
    include_managed_local_models: bool | None = None,
    action_credential_resolver: ActionCredentialResolver | None = None,
) -> ExecutionComposition:
    """The open-edition production composition for one effective router.

    This is the sole production constructor for :class:`CompositionFacts`.
    ``router`` is handed to the static provider so its remote-api liveness
    probe observes the same credential layer dispatch will use, including
    request-scoped project/org overlays.
    """
    from frisket.execution.definitions import (
        StaticExecutionTargetProvider,
        managed_local_models_configured,
    )

    del context  # Open composition has no edition-owned facts to consume.
    credential_context = CredentialUseContext(
        credential_resolver=action_credential_resolver
    )

    return ExecutionComposition(
        facts=CompositionFacts(
            edition="open",
            org_id=None,
            funding=OperatorBorne(),
        ),
        provider=StaticExecutionTargetProvider(
            secrets=project,
            router=router,
            credential_use_context=credential_context,
            models_gateway_resolver=models_gateway_resolver,
            include_managed_local_models=(
                managed_local_models_configured(os.environ)
                if include_managed_local_models is None
                else include_managed_local_models
            ),
        ),
        credential_use_context=credential_context,
    )
