"""Request-scoped Cloud targets composed with Solo's existing local targets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from frisket.cloud_connection import (
    CloudConnectionClient,
    CloudConnectionStore,
    CloudConnectionStoreError,
)
from frisket.cloud_execution import CloudExecutionTransport, CloudPayerSubject
from frisket.contracts.http.cloud_connection import (
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudLLMOptionSupport,
    CloudTokenPrice,
    CloudUnitPrice,
)
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    find_engine,
    to_markdown_engine_table,
)
from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.provider import (
    ConnectionConfig,
    ExactExecutionMatch,
    ExecutionComposition,
    ExecutionTargetProvider,
    TargetConfiguration,
)
from frisket.execution.targets import (
    CAPABILITY_LLM,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    ExecutionTarget,
    LLMOptionSupport,
    LOCAL_MODELS_TARGET_ID,
    OcrOptionSupport,
    TargetEngineSupport,
    ToMarkdownOptionSupport,
)
from frisket.project_settings import read_project_settings

CLOUD_MEDIA_TRANSPORT = "frisket.cloud.media.v1"
CLOUD_LLM_TRANSPORT = "frisket.cloud.llm.v1"


class _AccountIdentity(Protocol):
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class _UnavailableAccount:
    id: int
    name: str = "Selected Cloud account"


def cloud_target_id(account_id: int, lane: str, provider: str) -> str:
    return f"frisket-cloud:{account_id}:{lane}:{provider}"


class CloudExecutionTargetProvider:
    """One composition snapshot combining ordinary and Cloud targets."""

    def __init__(
        self,
        source: ExecutionTargetProvider,
        cloud_targets: Sequence[ExecutionTarget],
        payers: dict[tuple[str, str, str], CloudPayerSubject],
        token_prices: dict[tuple[str, str, str], CloudTokenPrice],
        presentations: dict[tuple[str, str, str], CommercialPresentation],
        *,
        preference: str,
        execution_transport: CloudExecutionTransport | None = None,
    ) -> None:
        self._source = source
        self._cloud_targets = tuple(cloud_targets)
        self._cloud_by_id = {target.id: target for target in self._cloud_targets}
        self._payers = dict(payers)
        self._token_prices = dict(token_prices)
        self._presentations = dict(presentations)
        self._execution_transport = execution_transport
        self._preference = preference
        self._source_targets = tuple(source.targets())
        self._source_by_id = {target.id: target for target in self._source_targets}
        self._targets = self._source_targets + self._cloud_targets

    def targets(self) -> tuple[ExecutionTarget, ...]:
        return self._targets

    def configuration(
        self, target_id: str, capability: str, engine: str
    ) -> TargetConfiguration:
        target = self._cloud_by_id.get(target_id)
        if target is not None:
            return (
                "not_configured"
                if any(
                    support.capability == capability
                    and support.engine == engine
                    and support.unavailable_reason == "account_key_required"
                    for support in target.engines
                )
                else "configured"
            )
        return self._source.configuration(target_id, capability, engine)

    def selection_rank(self, target_id: str, capability: str, engine: str) -> int:
        if (
            target_id == LOCAL_MODELS_TARGET_ID
            and target_id in self._source_by_id
            and self.configuration(target_id, capability, engine) == "configured"
        ):
            # The installed, app-managed local runtime is the preferred own
            # venue regardless of who should pay when Cloud is also usable.
            return 0
        if target_id in self._cloud_by_id:
            return 1 if self._preference == "cloud_first" else 2
        return 2 if self._preference == "cloud_first" else 1

    def connection(self, target_id: str) -> ConnectionConfig | None:
        target = self._cloud_by_id.get(target_id)
        if target is not None:
            # The installation bearer and origin remain inside the dedicated
            # Cloud transport. This marker is dispatch identity, not a key.
            return ConnectionConfig(extra={"cloud_transport": CLOUD_MEDIA_TRANSPORT})
        return self._source.connection(target_id)

    def cloud_payer(
        self, target_id: str, capability: str, engine: str
    ) -> CloudPayerSubject | None:
        return self._payers.get((target_id, capability, engine))

    def cloud_execution_transport(
        self, target_id: str
    ) -> CloudExecutionTransport | None:
        return self._execution_transport if target_id in self._cloud_by_id else None

    def cloud_token_price(
        self, target_id: str, capability: str, engine: str
    ) -> CloudTokenPrice | None:
        return self._token_prices.get((target_id, capability, engine))

    def cloud_presentation(
        self, target_id: str, capability: str, engine: str
    ) -> CommercialPresentation | None:
        return self._presentations.get((target_id, capability, engine))

    def liveness_remedy(
        self, target_id: str, capability: str | None = None
    ) -> str | None:
        if target_id in self._cloud_by_id:
            target = self._cloud_by_id[target_id]
            reasons = {
                support.unavailable_reason
                for support in target.engines
                if support.unavailable_reason is not None
                and (capability is None or support.capability == capability)
            }
            return next(iter(reasons), "Reconnect Frisket Cloud and retry.")
        hook = getattr(self._source, "liveness_remedy", None)
        if not callable(hook):
            return None
        try:
            return hook(target_id, capability=capability)
        except TypeError:
            return hook(target_id)


def compose_cloud_targets(
    base: ExecutionComposition,
    *,
    user_id: int,
    account: _AccountIdentity,
    capabilities: CloudCapabilitiesResponse,
    preference: str,
    execution_transport: CloudExecutionTransport | None = None,
    router: Any | None = None,
) -> ExecutionComposition:
    """Attach one selected account's authoritative execution routes to ``base``."""

    # Even an empty response is authoritative for inherited/default routing.
    # Explicit per-model choices still remain selected and refuse honestly if
    # their Cloud target is absent.
    base = replace(base, cloud_inventory_known=True)
    grouped: dict[str, list[TargetEngineSupport]] = defaultdict(list)
    payers: dict[tuple[str, str, str], CloudPayerSubject] = {}
    token_prices: dict[tuple[str, str, str], CloudTokenPrice] = {}
    presentations: dict[tuple[str, str, str], CommercialPresentation] = {}
    offerings: list[CommercialOffering] = []
    included: list[ExactExecutionMatch] = []
    for route in capabilities.routes:
        capability = _media_capability(route)
        if route.capability == CAPABILITY_LLM:
            capability = CAPABILITY_LLM
        if capability is None:
            continue
        if _explicitly_local_engine(capability, route.engine):
            continue
        target_id = cloud_target_id(account.id, route.lane, route.provider)
        support = _support(route, capability)
        grouped[target_id].append(support)
        payer = CloudPayerSubject(
            user_id=user_id,
            account_id=account.id,
            lane=route.lane,
            provider=route.provider,
            engine=route.engine,
            route_revision=route.route_revision,
        )
        payers[(target_id, capability, route.engine)] = payer
        presentations[(target_id, capability, route.engine)] = CommercialPresentation(
            venue_label="Frisket Cloud",
            billing_label=(
                f"{account.name} provider account"
                if route.lane == "account_key"
                else f"{account.name} Cloud credits"
            ),
        )
        if isinstance(route.price, CloudTokenPrice):
            token_prices[(target_id, capability, route.engine)] = route.price
        offering = _offering(route, capability, target_id, account)
        if offering is None:
            included.append(
                ExactExecutionMatch(
                    target_id=target_id,
                    capability=capability,
                    engine=route.engine,
                )
            )
        else:
            offerings.append(offering)
    targets = tuple(
        ExecutionTarget(
            id=target_id,
            operator="frisket",
            egress_class="frisket_shared",
            engines=tuple(supports),
        )
        for target_id, supports in grouped.items()
    )
    if not targets:
        return base
    provider = CloudExecutionTargetProvider(
        base.provider,
        targets,
        payers,
        token_prices,
        presentations,
        preference=preference,
        execution_transport=execution_transport,
    )
    return replace(
        base,
        provider=provider,
        offerings=base.offerings + tuple(offerings),
        included=base.included + tuple(included),
    )


def cloud_execution_composition(
    base: ExecutionComposition,
    project: object,
    *,
    store: CloudConnectionStore,
    client: CloudConnectionClient,
    execution_transport: CloudExecutionTransport | None = None,
    router: Any | None = None,
) -> ExecutionComposition:
    """Build one project's account-bound Cloud view from its last route snapshot."""

    settings = read_project_settings(project)
    # Solo exposes model routing even while signed out. ``None`` is reserved
    # for compositions (notably native hosted) that do not support the
    # selector; existing Solo projects therefore become an empty explicit map
    # until the settings service materializes their configured choices.
    legacy_routes = settings.model_execution_routes is None
    try:
        profile = store.load()
    except CloudConnectionStoreError:
        preference = _effective_legacy_cloud_execution_preference(settings, None)
        return replace(
            base,
            model_routes=dict(settings.model_execution_routes or {}),
            model_route_default=(
                "cloud"
                if legacy_routes and preference == "cloud_first"
                else settings.model_execution_default
            ),
        )
    preference = (
        _effective_legacy_cloud_execution_preference(settings, profile)
        if legacy_routes
        else "local_first"
    )
    base = replace(
        base,
        model_routes=dict(settings.model_execution_routes or {}),
        model_route_default=(
            "cloud"
            if legacy_routes and preference == "cloud_first"
            else settings.model_execution_default
        ),
    )
    connection = profile.connection
    if connection is None:
        return base
    if (
        settings.cloud_user_id != connection.session.user.id
        or settings.cloud_account_id is None
    ):
        return base
    account = next(
        (
            candidate
            for candidate in connection.session.accounts
            if candidate.id == settings.cloud_account_id
        ),
        None,
    )
    snapshot = profile.capabilities.get(settings.cloud_account_id)
    unavailable_reason: str | None = None
    if _expired(connection.session.expires_at):
        unavailable_reason = "Reconnect Frisket Cloud and retry."
        capabilities = snapshot
    elif account is None:
        unavailable_reason = "The selected Frisket Cloud account is unavailable."
        capabilities = snapshot
    else:
        capabilities = snapshot
    if capabilities is None:
        return base
    if unavailable_reason is not None:
        capabilities = _blocked_capabilities(capabilities, unavailable_reason)
    if account is None:
        account = _UnavailableAccount(settings.cloud_account_id)
    return compose_cloud_targets(
        base,
        user_id=connection.session.user.id,
        account=account,
        capabilities=capabilities,
        preference=preference,
        execution_transport=execution_transport,
        router=router,
    )


def legacy_cloud_execution_preference(
    project: object, store: CloudConnectionStore | None
) -> Literal["local_first", "cloud_first"]:
    """Read the legacy project/install preference without exposing the store."""

    settings = read_project_settings(project)
    if store is None:
        return _effective_legacy_cloud_execution_preference(settings, None)
    try:
        profile = store.load()
    except CloudConnectionStoreError:
        profile = None
    return _effective_legacy_cloud_execution_preference(settings, profile)


def _effective_legacy_cloud_execution_preference(
    settings: Any, profile: Any | None
) -> Literal["local_first", "cloud_first"]:
    if settings.cloud_user_id is None or settings.cloud_account_id is None:
        return "local_first"
    return settings.cloud_execution_preference or (
        profile.preference if profile is not None else "cloud_first"
    )


def cloud_fallback_is_blocked(
    composition: ExecutionComposition,
    *,
    target_id: str,
    capability: str,
    engine: str,
) -> bool:
    hook = getattr(composition.provider, "blocks_cloud_fallback", None)
    return bool(callable(hook) and hook(target_id, capability, engine))


def _explicitly_local_engine(capability: str, engine: str) -> bool:
    tables = {
        CAPABILITY_OCR: OCR_ENGINE_TABLE,
        CAPABILITY_TRANSCRIBE: TRANSCRIBE_ENGINE_TABLE,
        CAPABILITY_TO_MARKDOWN: to_markdown_engine_table(),
    }
    table = tables.get(capability)
    declaration = find_engine(table, engine) if table is not None else None
    return declaration is not None and declaration.tier == "local"


def _expired(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) <= datetime.now(UTC)


def _media_capability(route: CloudCapabilityRoute) -> str | None:
    if route.capability == "ocr":
        return CAPABILITY_OCR
    if route.capability == "transcribe":
        return CAPABILITY_TRANSCRIBE
    if route.capability == "document.convert":
        return CAPABILITY_TO_MARKDOWN
    return None


def _support(route: CloudCapabilityRoute, capability: str) -> TargetEngineSupport:
    unavailable = (
        route.blocker or "Cloud route unavailable" if not route.ready else None
    )
    expected_price = (
        (CloudTokenPrice, CloudUnitPrice)
        if capability == CAPABILITY_OCR
        else (CloudTokenPrice if capability == CAPABILITY_LLM else CloudUnitPrice)
    )
    if unavailable is None and not isinstance(route.price, expected_price):
        unavailable = "Cloud route has no authoritative price"
    if capability == CAPABILITY_LLM:
        options = route.options
        if not isinstance(options, CloudLLMOptionSupport):
            raise ValueError("Cloud LLM route has invalid option support")
        return TargetEngineSupport(
            engine=route.engine,
            transport=CLOUD_LLM_TRANSPORT,
            capability=capability,
            options=LLMOptionSupport(
                structured_output=options.structured_output,
                tools=options.tools,
                vision=options.vision,
                max_output_tokens=options.max_output_tokens,
            ),
            unavailable_reason=unavailable,
        )
    if capability == CAPABILITY_OCR:
        options = route.options
        return TargetEngineSupport(
            engine=route.engine,
            transport=(
                CLOUD_LLM_TRANSPORT
                if isinstance(route.price, CloudTokenPrice)
                else CLOUD_MEDIA_TRANSPORT
            ),
            capability=capability,
            options=OcrOptionSupport(
                language=options.language,
                geometry=options.geometry,
                searchable_pdf=options.searchable_pdf,
                pdf_input=options.pdf_input,
            ),
            unavailable_reason=unavailable,
        )
    if capability == CAPABILITY_TRANSCRIBE:
        return TargetEngineSupport(
            engine=route.engine,
            transport=CLOUD_MEDIA_TRANSPORT,
            capability=capability,
            options=TranscriptionOptionSupport.model_validate(
                route.options.model_dump(mode="json", exclude={"kind"})
            ),
            unavailable_reason=unavailable,
        )
    return TargetEngineSupport(
        engine=route.engine,
        transport=CLOUD_MEDIA_TRANSPORT,
        capability=capability,
        options=ToMarkdownOptionSupport(),
        unavailable_reason=unavailable,
    )


def _offering(
    route: CloudCapabilityRoute,
    capability: str,
    target_id: str,
    account: _AccountIdentity,
) -> CommercialOffering | None:
    price = route.price
    billing = (
        f"{account.name} provider account"
        if route.lane == "account_key"
        else f"{account.name} Cloud credits"
    )
    if isinstance(price, CloudTokenPrice):
        return CommercialOffering(
            match=CommercialOfferingMatch(
                target_id=target_id,
                capability=capability,
                engine=route.engine,
            ),
            quote=CommercialQuoteTerms(
                pricing_key=(
                    f"frisket-cloud:{account.id}:{route.lane}:"
                    f"{route.provider}:{capability}:{route.engine}"
                ),
                terms_version=route.route_revision,
                unit_rate="1",
                quantity_unit="usd",
                rounding=CommercialQuoteRounding(mode="exact", decimal_places=None),
            ),
            settlement=CommercialSettlementTerms(
                meter_key="cloud_effective_cost_micro",
                meter_units_per_quantity_unit="1000000",
                ceiling_mode="consented_quantity",
                row_settlement_mode="quoted_successful_rows",
            ),
            charge_authority="frisket_cloud_receipt",
            presentation=CommercialPresentation(
                venue_label="Frisket Cloud",
                billing_label=billing,
            ),
        )
    if not isinstance(price, CloudUnitPrice):
        return None
    expected_unit = "audio_second" if capability == CAPABILITY_TRANSCRIBE else "page"
    if price.unit != expected_unit:
        raise ValueError("Cloud route price unit does not match its capability")
    rate = Decimal(price.micro_per_unit) / Decimal(1_000_000)
    return CommercialOffering(
        match=CommercialOfferingMatch(
            target_id=target_id,
            capability=capability,
            engine=route.engine,
        ),
        quote=CommercialQuoteTerms(
            pricing_key=(
                f"frisket-cloud:{account.id}:{route.lane}:"
                f"{route.provider}:{capability}:{route.engine}"
            ),
            terms_version=route.route_revision,
            unit_rate=format(rate, "f"),
            quantity_unit=expected_unit,
            rounding=CommercialQuoteRounding(),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds" if expected_unit == "audio_second" else "pages",
            meter_units_per_quantity_unit="1",
            ceiling_mode="consented_quantity",
            row_settlement_mode="quoted_successful_rows",
        ),
        charge_authority="frisket_cloud_receipt",
        presentation=CommercialPresentation(
            venue_label="Frisket Cloud",
            billing_label=billing,
        ),
    )


def _blocked_capabilities(
    capabilities: CloudCapabilitiesResponse, reason: str
) -> CloudCapabilitiesResponse:
    return capabilities.model_copy(
        update={
            "routes": [
                route.model_copy(
                    update={
                        "ready": False,
                        "blocker": reason,
                    }
                )
                for route in capabilities.routes
            ]
        }
    )


__all__ = [
    "CLOUD_LLM_TRANSPORT",
    "CLOUD_MEDIA_TRANSPORT",
    "CloudExecutionTargetProvider",
    "cloud_target_id",
    "cloud_execution_composition",
    "cloud_fallback_is_blocked",
    "legacy_cloud_execution_preference",
    "compose_cloud_targets",
]
