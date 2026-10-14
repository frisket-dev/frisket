"""Viewer-readable engine and model selector projection.

This service composes existing action, model, embedding, execution-target,
credential, and artifact owners.  It performs no inference, estimate, download,
or runtime-start operation.  The only network read it can cause is the existing
bounded configured-local-endpoint model listing in ``build_provider_catalog``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote


from frisket.authoring.action_metadata import action_available_in_edition
from frisket.authoring.project_ask import default_project_ask_model
from frisket.ai.embeddings.capabilities import resolve_embedding_capability
from frisket.contracts.http.selector_choices import (
    ActionSelectorSubject,
    ProjectAskSelectorSubject,
    EmbeddingSelectorSubject,
    SelectorChoicesQuery,
    SelectorChoicesResponse,
)
from frisket.engine._workers.parakeet_artifacts import (
    parakeet_setup_ready,
    whisper_setup_ready,
)
from frisket.engine.jobs.engine_setup import DOCLING_SETUP_REF, PARAKEET_TDT_SETUP_REF
from frisket.execution.definitions import (
    CLOUDFLARE_CLEF_TARGET_ID,
    LOCAL_MODELS_TARGET_ID,
    MODELS_GATEWAY_TARGET_ID,
    parakeet_runtime_present,
)
from frisket.execution.provider import ExecutionComposition
from frisket.execution.resolve_for_action import (
    resolve_geocode_engine,
    authored_options,
)
from frisket.execution.resolver import (
    ModelRoute,
    Refusal,
    ResolutionRequest,
    preferred_static_choice,
    resolve,
)
from frisket.execution.targets import CAPABILITY_GEOCODE, CAPABILITY_LLM
from frisket.project_settings import read_project_settings
from frisket.server.action_catalog_hints import (
    project_action_catalog_payload_with_launcher_hints,
)
from frisket.server.embedding_catalog import embedding_provider_catalog_payload
from frisket.server.provider_config import (
    ENV_VAR,
    PROVIDER_LABELS,
    build_provider_catalog,
)
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace
from frisket.server.services.selector_choices_setup import SelectorSetupService
from frisket.server.services.project_ask_defaults import project_ask_inventory
from frisket.server.services.selector_choices_capabilities import (
    SelectorCapabilities,
    SelectorCapabilitiesFor,
    SelectorModelsGatewayStatusFor,
)

from frisket.server.services.selector_choices_projection import (
    _network_off,
    _mapping,
    _credential_label,
    _credential_source,
    _schema_default,
    _optional_str,
    _provider_identity,
    _provider_from_qualified,
    _default_action_model,
    _recommendation_composition,
    _choices_have_cloud_relevance,
    _model_selection,
    _engine_selection,
    _choice,
    _configuration_actions,
    _execution_route,
    _execution_presentation,
    _selected_cloud_without_target,
    _matching_targets,
    _target_for_outcome,
    _response,
    _server_labels,
    _engine_depends_on,
    _engine_destination,
    _engine_facts,
    _model_facts,
    _cloud_model_facts,
    _model_target,
    _embedding_target,
    _embedding_facts,
    _network_policy_forbids_target,
    _route_selection,
    _model_route_selectable,
    _selected_route,
    _opus_pair_supported,
    _opus_pair_key,
)


_TARGET_OVERRIDE_FIELDS = frozenset({"target", "target_id", "execution_target"})


@dataclass(frozen=True)
class _SelectorCloudContext:
    state: str | None
    account_id: int | None
    account_name: str | None
    available_micro: int | None
    balance_url: str | None
    add_credits_url: str | None
    project_settings_url: str
    origin: str | None

    def setup(
        self,
        *,
        payer_lane: str | None = None,
    ) -> dict[str, Any] | None:
        state = self.state
        if state is None:
            return None
        if state == "connected":
            if (
                payer_lane == "frisket_credits"
                and self.available_micro is not None
                and self.available_micro <= 0
            ):
                state = "out_of_credits"
            else:
                return None
        return {
            "kind": "cloud",
            "state": state,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "balance_url": self.balance_url,
            "add_credits_url": self.add_credits_url
            if state == "out_of_credits"
            else None,
            "project_settings_url": self.project_settings_url,
        }

    def account_key_setup(self, provider: str) -> dict[str, Any] | None:
        if self.origin is None or self.account_id is None:
            return None
        provider_label = PROVIDER_LABELS.get(provider, provider)
        return {
            "kind": "instructions",
            "title": "Set up key in Frisket Cloud",
            "steps": [
                f"Add a {provider_label} key to the selected Frisket Cloud account, then recheck."
            ],
            "url": (
                f"{self.origin}/funding-accounts?account_id={self.account_id}"
                f"&provider={quote(provider, safe='')}"
            ),
        }


def _cloud_context(
    workspace: Workspace, project_id: str, project: Any, session: Any
) -> _SelectorCloudContext:
    settings = read_project_settings(project)
    project_settings_url = (
        f"/p/{quote(project_id, safe='')}/settings/project/ai-providers"
    )
    if not workspace.supports_cloud_connection():
        return _SelectorCloudContext(
            None, None, None, None, None, None, project_settings_url, None
        )
    origin = workspace.cloud_connection_origin()
    if session is None:
        return _SelectorCloudContext(
            "reconnect"
            if settings.cloud_user_id is not None
            and settings.cloud_account_id is not None
            else "connect",
            settings.cloud_account_id,
            None,
            None,
            None,
            None,
            project_settings_url,
            origin,
        )
    try:
        expires_at = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expired = expires_at.astimezone(UTC) <= datetime.now(UTC)
    except (TypeError, ValueError):
        expired = True
    selected_id = (
        settings.cloud_account_id if settings.cloud_user_id == session.user.id else None
    )
    account = next(
        (candidate for candidate in session.accounts if candidate.id == selected_id),
        None,
    )
    balance_url = (
        f"{origin}/funding-accounts?account_id={account.id}"
        if origin is not None and account is not None and account.manage_billing
        else None
    )
    add_credits_url = (
        f"{origin}/billing-checkout?account_id={account.id}"
        if origin is not None and account is not None and account.manage_billing
        else None
    )
    return _SelectorCloudContext(
        "reconnect"
        if expired
        else "account_required"
        if account is None
        else "connected",
        selected_id,
        account.name if account is not None else None,
        account.available_micro if account is not None else None,
        balance_url,
        add_credits_url,
        project_settings_url,
        origin,
    )


def _balance_label(cloud: _SelectorCloudContext) -> str | None:
    if cloud.available_micro is None:
        return None
    return f"${cloud.available_micro / 1_000_000:,.2f} balance"


def _resolve_route_choice(
    composition: ExecutionComposition,
    *,
    capability: str,
    engine: str,
    options: Mapping[str, Any],
) -> tuple[Any, ModelRoute]:
    """Resolve the one project route selected by the composition."""

    request = composition.resolution_request(
        engine,
        capability=capability,
        options=options,
    )
    outcome = resolve(request, composition.provider_for_resolution(), composition.facts)
    return outcome, _selected_route(
        composition,
        outcome,
        capability=capability,
        engine=engine,
        requested=request.route,
    )


def _classification_options_blocker(
    action_id: str,
    params: Mapping[str, Any],
    engine: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Explain why an authored Classify shape cannot use one engine."""
    if action_id != "map.classify":
        return None
    options = _mapping(engine.get("classification_options"))
    field_types = options.get("field_types")
    fields = params.get("fields")
    max_fields = options.get("max_fields")
    if (
        isinstance(fields, list)
        and isinstance(max_fields, int)
        and not isinstance(max_fields, bool)
        and len(fields) > max_fields
    ):
        return {
            "code": "classification_fields_unsupported",
            "message": f"This engine supports at most {max_fields} output field(s).",
            "field": "fields",
        }
    if isinstance(field_types, list) and field_types:
        allowed = {str(value) for value in field_types}
        if isinstance(fields, list) and any(
            isinstance(field, Mapping)
            and str(field.get("type") or "category") not in allowed
            for field in fields
        ):
            return {
                "code": "classification_fields_unsupported",
                "message": "This engine supports category fields only.",
                "field": "fields",
            }
    for param in ("include_confidence", "include_justification"):
        if params.get(param) and options.get(param) is False:
            return {
                "code": "classification_companion_unsupported",
                "message": "This engine emits labels without confidence or justification companions.",
                "field": param,
            }
    return None


class SelectorChoicesError(RouteError):
    pass


class SelectorChoiceService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        edition: str = "solo",
        models_gateway_status_for: SelectorModelsGatewayStatusFor | None = None,
    ) -> None:
        self._workspace = workspace
        self._edition = edition
        self._setup = SelectorSetupService(
            workspace,
            edition=edition,
            models_gateway_status_for=models_gateway_status_for,
        )

    def choices(
        self,
        project_id: str,
        query: SelectorChoicesQuery,
        *,
        request_context: Any = None,
        capabilities: SelectorCapabilities | None = None,
    ) -> SelectorChoicesResponse:
        capabilities = capabilities or SelectorCapabilities()
        if not isinstance(capabilities, SelectorCapabilities):
            raise TypeError("selector capabilities callback returned the wrong type")
        project = self._workspace.get(project_id)
        subject = query.subject
        router = (
            self._workspace.action_execution_router_for(project)
            if isinstance(subject, ActionSelectorSubject)
            else self._workspace.router_for(project)
        )
        composition, cloud_session = self._workspace.selector_execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(request_context),
        )
        cloud = _cloud_context(self._workspace, project_id, project, cloud_session)
        if isinstance(subject, ActionSelectorSubject):
            payload = self._action_choices(
                project_id,
                project=project,
                router=router,
                composition=composition,
                cloud=cloud,
                subject=subject,
                capabilities=capabilities,
            )
        elif isinstance(subject, ProjectAskSelectorSubject):
            payload = self._project_ask_choices(
                project_id,
                project=project,
                router=router,
                composition=composition,
                cloud=cloud,
                provider_catalog=self._provider_catalog(project, router),
                subject=subject,
                capabilities=capabilities,
            )
        else:
            payload = self._embedding_choices(
                project_id,
                project=project,
                router=router,
                composition=composition,
                subject=cast(EmbeddingSelectorSubject, subject),
                capabilities=capabilities,
            )
        return SelectorChoicesResponse.model_validate(payload)

    def _provider_catalog(self, project: Any, router: Any) -> dict[str, Any]:
        catalog = build_provider_catalog(
            self._workspace.root,
            # Keep authored and exact composed models visible. The selector
            # applies network policy to the resolved target below, preserving
            # the selected choice instead of filtering or reranking it.
            network_off=False,
            local_endpoints=router.local_endpoints,
            local_endpoint_authority="instance"
            if self._edition == "solo"
            else "organization",
        )
        configured_providers = set(router.providers())
        for provider in catalog.get("providers", []):
            if isinstance(provider, dict) and provider.get("id") in ENV_VAR:
                provider["configured"] = provider["id"] in configured_providers
                provider["credential_source"] = router.credential_source_for(
                    provider["id"]
                )
        return catalog

    def _action_choices(
        self,
        project_id: str,
        *,
        project: Any,
        router: Any,
        composition: ExecutionComposition,
        cloud: _SelectorCloudContext,
        subject: ActionSelectorSubject,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        forbidden = sorted(_TARGET_OVERRIDE_FIELDS.intersection(subject.params))
        if forbidden:
            raise SelectorChoicesError(
                400,
                {
                    "code": "authored_target_override",
                    "message": "execution targets are resolved by the project",
                    "field": f"subject.params.{forbidden[0]}",
                },
            )
        catalog = project_action_catalog_payload_with_launcher_hints(
            project,
            sidecar_capabilities={"catalog_from_composition": True},
            execution_composition=composition,
            org_provider_keys=self._workspace.org_provider_keys(),
            has_local_model_endpoint=bool(router.local_endpoints),
            effective_router=router,
            connected_account_resolver_configured=(
                self._workspace.executor_deps_factory is not None
            ),
            action_kinds=frozenset({subject.action_id}),
        )
        actions = [
            item
            for item in catalog.get("actions", [])
            if self._edition == "solo"
            or action_available_in_edition(item.get("kind"), self._edition)
        ]
        action = next(
            (item for item in actions if item.get("kind") == subject.action_id), None
        )
        if action is None:
            raise SelectorChoicesError(
                400,
                {
                    "code": "unknown_selector_action",
                    "message": f"unknown action '{subject.action_id}'",
                    "field": "subject.action_id",
                },
            )
        hints = _mapping(action.get("ui_hints"))
        controls = _mapping(hints.get("semantic_controls"))
        control = controls.get(subject.field)
        properties = _mapping(_mapping(action.get("input_schema")).get("properties"))
        if control not in {"engine", "model"} or subject.field not in properties:
            raise SelectorChoicesError(
                400,
                {
                    "code": "unknown_selector_field",
                    "message": (
                        f"'{subject.field}' is not a declared engine or model field "
                        f"for action '{subject.action_id}'"
                    ),
                    "field": "subject.field",
                },
            )

        if control == "model":
            provider_catalog = self._provider_catalog(project, router)
            choices = self._model_choices(
                router=router,
                project=project,
                provider_catalog=provider_catalog,
                capabilities=capabilities,
                composition=composition,
                cloud=cloud,
                selection_kind="model",
            )
            current_selection = _model_selection(subject.params.get(subject.field))
            default_model = _default_action_model(
                choices,
                composition,
                network_off=_network_off(project),
            )
            default_selection = _model_selection(default_model)
            depends_on: list[str] = []
        else:
            engines = [
                dict(row) for row in hints.get("engines", []) if isinstance(row, dict)
            ]
            has_model_field = controls.get("model") == "model" and "model" in properties
            provider_catalog = (
                self._provider_catalog(project, router)
                if has_model_field
                and any(engine.get("id") == "llm" for engine in engines)
                else {"providers": []}
            )
            choices = self._engine_choices(
                router=router,
                project=project,
                composition=composition,
                provider_catalog=provider_catalog,
                action_id=subject.action_id,
                execution_capability=_optional_str(hints.get("execution_capability")),
                engines=engines,
                has_model_field=has_model_field,
                params=subject.params,
                capabilities=capabilities,
                cloud=cloud,
            )
            if subject.action_id == "enrich.geocode":
                choices.insert(
                    0,
                    self._geocode_auto_choice(
                        project=project,
                        composition=composition,
                        engines=engines,
                        params=subject.params,
                        capabilities=capabilities,
                    ),
                )
            current_engine = subject.params.get(subject.field)
            current_model = subject.params.get("model") if has_model_field else None
            current_selection = _engine_selection(
                current_engine,
                model=current_model,
                mixed=has_model_field,
            )
            default_engine = _schema_default(properties.get(subject.field))
            default_model = (
                _default_action_model(
                    choices,
                    composition,
                    network_off=_network_off(project),
                )
                if has_model_field and default_engine == "llm"
                else None
            )
            default_selection = _engine_selection(
                default_engine,
                model=default_model,
                mixed=has_model_field,
            )
            depends_on = _engine_depends_on(properties, engines)
            execution_capability = _optional_str(hints.get("execution_capability"))
            if execution_capability is not None:
                depends_on = list(
                    dict.fromkeys(
                        [
                            *depends_on,
                            *authored_options(properties, execution_capability),
                        ]
                    )
                )

        return _response(
            project_id,
            subject={
                "kind": "action",
                "action_id": subject.action_id,
                "field": subject.field,
            },
            depends_on=depends_on,
            choices=choices,
            current_selection=current_selection,
            default_selection=default_selection,
            server_labels=_server_labels(provider_catalog),
            cloud_setup=(
                cloud.setup() if _choices_have_cloud_relevance(choices) else None
            ),
        )

    def _project_ask_choices(
        self,
        project_id: str,
        *,
        project: Any,
        router: Any,
        composition: ExecutionComposition,
        cloud: _SelectorCloudContext,
        provider_catalog: dict[str, Any],
        subject: ProjectAskSelectorSubject,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        candidate_models, direct_models, spend_by_provider = project_ask_inventory(
            provider_catalog,
            router=router,
            project=project,
        )
        choices = self._model_choices(
            router=router,
            project=project,
            provider_catalog=provider_catalog,
            capabilities=capabilities,
            composition=composition,
            cloud=cloud,
            selection_kind="model",
            provider_spend_states=spend_by_provider,
        )
        network_off = _network_off(project)
        return _response(
            project_id,
            subject={"kind": "project_ask"},
            depends_on=[],
            choices=choices,
            current_selection=_model_selection(subject.model),
            default_selection=_model_selection(
                default_project_ask_model(
                    router,
                    _recommendation_composition(composition),
                    candidate_models=candidate_models,
                    directly_runnable_models=direct_models,
                    network_off=network_off,
                    allow_setup_fallback=not network_off,
                )
            ),
            server_labels=_server_labels(provider_catalog),
            cloud_setup=cloud.setup(),
        )

    def _embedding_choices(
        self,
        project_id: str,
        *,
        project: Any,
        router: Any,
        composition: ExecutionComposition,
        subject: EmbeddingSelectorSubject,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        del composition
        environment = dict(os.environ)
        for key in ENV_VAR.values():
            environment.pop(key, None)
        catalog = embedding_provider_catalog_payload(
            router=router,
            env=environment,
            modality=subject.modality,
            source_column_type=subject.source_column_type,
        )
        choices: list[dict[str, Any]] = []
        recommended_selections: list[dict[str, Any]] = []
        current_pair = (
            (subject.provider, subject.model)
            if subject.provider is not None and subject.model is not None
            else None
        )
        for row in catalog.get("providers", []):
            if not isinstance(row, dict):
                continue
            candidate = dict(row)
            if (
                candidate.get("dimension_discovery_required")
                and candidate.get("provider_id") == subject.provider
                and subject.model
            ):
                candidate["model_id"] = subject.model
                candidate["label"] = subject.model
            choice = self._embedding_choice(
                router=router,
                project=project,
                row=candidate,
                capabilities=capabilities,
            )
            choices.append(choice)
            if candidate.get("recommended") and candidate.get("modality_compatible"):
                recommended_selections.append(choice["authored_selection"])
        if current_pair is not None and not any(
            choice["authored_selection"]
            == {
                "kind": "embedding",
                "provider": current_pair[0],
                "model": current_pair[1],
            }
            for choice in choices
        ):
            custom = resolve_embedding_capability(
                provider=current_pair[0],
                model=current_pair[1],
                modality=catalog.get("modality") or "text",
                router=router,
                env=environment,
            )
            if custom is not None and custom.get("available"):
                custom_choice = self._embedding_choice(
                    router=router,
                    project=project,
                    capabilities=capabilities,
                    row={**custom, "modality_compatible": True},
                )
                custom_choice["authored_selection"] = {
                    "kind": "embedding",
                    "provider": current_pair[0],
                    "model": current_pair[1],
                }
                choices.append(custom_choice)
        default = next(
            (selection for selection in recommended_selections),
            None,
        )
        return _response(
            project_id,
            subject={
                "kind": "embedding",
                "modality": catalog.get("modality"),
                "source_column_type": subject.source_column_type,
            },
            depends_on=[
                name
                for name, value in (
                    ("modality", subject.modality),
                    ("source_column_type", subject.source_column_type),
                )
                if value is not None
            ],
            choices=choices,
            current_selection=(
                {
                    "kind": "embedding",
                    "provider": current_pair[0],
                    "model": current_pair[1],
                }
                if current_pair is not None
                else None
            ),
            default_selection=default,
        )

    def _model_choices(
        self,
        *,
        router: Any,
        project: Any,
        provider_catalog: dict[str, Any],
        capabilities: SelectorCapabilities,
        composition: ExecutionComposition,
        cloud: _SelectorCloudContext,
        selection_kind: str,
        engine: str | None = None,
        provider_spend_states: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        choices: list[dict[str, Any]] = []
        for provider in provider_catalog.get("providers", []):
            if not isinstance(provider, dict):
                continue
            provider_id = _provider_identity(provider)
            if provider_id is None:
                continue
            local_http = provider.get("kind") == "local_http"
            configured = (
                bool(provider.get("reachable"))
                if local_http
                else bool(provider.get("configured"))
            )
            provider_setup = (
                self._setup.api_key(
                    router=router,
                    project=project,
                    provider=provider_id,
                    capabilities=capabilities,
                    credential_source=_optional_str(provider.get("credential_source")),
                )
                if not local_http and provider_id in ENV_VAR
                else None
            )
            own_credential_label = _credential_label(
                provider_setup,
                _credential_source(
                    router,
                    provider_id,
                    raw_source=_optional_str(provider.get("credential_source")),
                ),
            )
            spend = (
                provider_spend_states.get(provider_id)
                if provider_spend_states is not None
                else project.provider_spend_state(provider_id)
                if configured and provider.get("credential_source") == "project_key"
                else None
            )
            for model in provider.get("models", []):
                if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                    continue
                selection = (
                    {
                        "kind": "engine_model",
                        "engine": engine,
                        "model": model["id"],
                    }
                    if selection_kind == "engine_model"
                    else {"kind": "model", "model": model["id"]}
                )
                setup = None
                blocker = None
                status = "ready"
                if not configured:
                    blocker = {
                        "code": (
                            "local_model_endpoint_unavailable"
                            if local_http
                            else "provider_key_required"
                        ),
                        "message": (
                            str(provider.get("detail") or "Model server unavailable.")
                            if local_http
                            else f"Configure a {provider.get('label') or provider_id} API key."
                        ),
                        "field": None,
                    }
                    if not local_http and provider_id in ENV_VAR:
                        status = "needs_setup"
                        setup = provider_setup
                    else:
                        status = "unavailable"
                        setup = {
                            "kind": "instructions",
                            "title": "Configure the model server",
                            "steps": [
                                "Review the configured endpoint in AI Providers settings and ensure the server is reachable."
                            ],
                            "url": None,
                        }
                facts = _model_facts(model)
                if (
                    status == "ready"
                    and provider.get("credential_source") == "project_key"
                ):
                    if spend is not None and (
                        spend.over_cap or not spend.cap_enforceable
                    ):
                        status = "unavailable"
                        blocker = {
                            "code": "provider_spend_cap_exceeded"
                            if spend.over_cap
                            else "provider_spend_cap_unenforceable",
                            "message": "The project provider key cannot spend under its current cap.",
                            "field": None,
                        }
                target, destination = _model_target(provider)
                route_capable = not local_http
                outcome, selected_route = _resolve_route_choice(
                    composition,
                    capability=CAPABILITY_LLM,
                    engine=model["id"],
                    options={},
                )
                presentation = _execution_presentation(
                    composition,
                    outcome,
                    capability=CAPABILITY_LLM,
                    engine=model["id"],
                    fallback_target=target,
                    fallback_destination=destination,
                    status=status,
                    blocker=blocker,
                    setup=setup,
                    cloud_setup_for_lane=lambda lane: cloud.setup(payer_lane=lane),
                    cloud_account_key_setup=cloud.account_key_setup,
                    balance_label=_balance_label(cloud),
                    balance_url=cloud.balance_url,
                    settings_url=cloud.project_settings_url,
                    credential_label=own_credential_label,
                    network_off=_network_off(project),
                    preserve_unbound_catalog=local_http,
                )
                cloud_setup = cloud.setup()
                if (
                    route_capable
                    and selected_route == "cloud"
                    and presentation.target is None
                ):
                    assert isinstance(outcome, Refusal)
                    presentation = _selected_cloud_without_target(
                        presentation,
                        refusal=outcome,
                        cloud_setup=cloud_setup,
                    )
                    facts = []
                elif selected_route == "cloud" and presentation.target is not None:
                    facts = _cloud_model_facts(
                        composition, presentation.target, model["id"], CAPABILITY_LLM
                    )
                route_selection = (
                    _route_selection(
                        composition,
                        capability=CAPABILITY_LLM,
                        engine=model["id"],
                        options={},
                        selected=selected_route,
                        can_mutate=capabilities.may_author_actions,
                        cloud_unknown=cloud_setup is not None,
                        network_off=_network_off(project),
                        own_blocker=blocker if status == "unavailable" else None,
                    )
                    if route_capable
                    else None
                )
                configuration_actions = _configuration_actions(
                    provider_setup,
                    active_setup=presentation.setup,
                )
                if route_selection is not None:
                    configuration_actions = [
                        action
                        for action in configuration_actions
                        if action.get("kind") not in {"api_key", "models_gateway"}
                    ]
                choices.append(
                    _choice(
                        selection=selection,
                        label=str(model.get("label") or model["id"]),
                        summary="",
                        description="",
                        model_card_url=None,
                        resolved_target=presentation.resolved_target,
                        processing_destination=presentation.processing_destination,
                        facts=facts,
                        status=presentation.status,
                        can_author=capabilities.may_author_actions
                        and presentation.status != "unavailable",
                        can_run=capabilities.may_run_actions
                        and presentation.status == "ready",
                        blocker=presentation.blocker,
                        setup=presentation.setup,
                        configuration_actions=configuration_actions,
                        execution_route=presentation.execution_route,
                        route_selection=route_selection,
                    )
                )
        return choices

    def _geocode_auto_choice(
        self,
        *,
        project: Any,
        composition: ExecutionComposition,
        engines: list[dict[str, Any]],
        params: Mapping[str, Any],
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        # The authored sentinel is not a concrete roster engine. Resolve its
        # credential-dependent engine exactly as execution does, then retain
        # the sentinel in the authored selection.
        engine_id = resolve_geocode_engine(
            {"engine": "auto"},
            project,
            credential_context=composition.credential_use_context,
        )
        engine = next((row for row in engines if row.get("id") == engine_id), {})
        outcome = resolve(
            ResolutionRequest(
                engine=engine_id,
                options=authored_options(params, CAPABILITY_GEOCODE),
                capability=CAPABILITY_GEOCODE,
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
        refusal = outcome if isinstance(outcome, Refusal) else None
        target = (
            next(
                (
                    row
                    for row in composition.resolution_targets()
                    if row.id == refusal.target_id
                ),
                None,
            )
            if refusal is not None
            else outcome.target
        )
        policy_forbidden = _network_policy_forbids_target(_network_off(project), target)
        ready = refusal is None and not policy_forbidden
        destination = _engine_destination(engine, target)
        return _choice(
            selection={"kind": "engine", "engine": "auto"},
            label="Auto",
            summary=f"{engine.get('label') or engine_id} · {destination['label']}",
            description="Uses OpenCage when a scoped key is available, otherwise Nominatim.",
            model_card_url=_optional_str(engine.get("model_card_url")),
            resolved_target={
                "target_id": target.id,
                "operator": target.operator,
                "egress_class": target.egress_class,
            }
            if target is not None
            else None,
            processing_destination=destination,
            facts=_engine_facts(engine, target),
            status="ready" if ready else "unavailable",
            can_author=capabilities.may_author_actions and ready,
            can_run=capabilities.may_run_actions and ready,
            blocker={
                "code": "project_network_off",
                "message": "This project's network setting is off.",
                "field": None,
            }
            if policy_forbidden
            else {
                "code": refusal.family,
                "message": refusal.remedy,
                "field": None,
            }
            if refusal is not None
            else None,
            setup=None,
            execution_route=(
                _execution_route(
                    composition,
                    target,
                    capability=CAPABILITY_GEOCODE,
                    engine=engine_id,
                )
                if target is not None
                else None
            ),
        )

    def _engine_configuration_candidates(
        self,
        *,
        router: Any,
        project: Any,
        composition: ExecutionComposition,
        capability: str,
        engine: str,
        capabilities: SelectorCapabilities,
        candidate_setup: dict[str, Any] | None,
    ) -> list[dict[str, Any] | None]:
        """Existing setup operations for own venues matching one exact engine."""

        actions: list[dict[str, Any] | None] = [candidate_setup]
        own_targets = [
            target
            for target in _matching_targets(
                composition, capability=capability, engine=engine
            )
            if composition.cloud_payer_for(
                target_id=target.id,
                capability=capability,
                engine=engine,
            )
            is None
        ]
        if any(target.id == MODELS_GATEWAY_TARGET_ID for target in own_targets):
            actions.append(self._setup.models_gateway(capabilities))

        provider = (
            "datalab" if engine == "datalab" else _provider_from_qualified(engine)
        )
        if provider in ENV_VAR and own_targets:
            actions.append(
                self._setup.api_key(
                    router=router,
                    project=project,
                    provider=cast(str, provider),
                    capabilities=capabilities,
                )
            )

        provider_for_resolution = composition.provider_for_resolution()
        if engine == "docling" and any(
            target.id == LOCAL_MODELS_TARGET_ID
            and provider_for_resolution.configuration(target.id, capability, engine)
            == "not_configured"
            for target in own_targets
        ):
            actions.append(self._setup.engine_setup(DOCLING_SETUP_REF, capabilities))
        if engine == "parakeet-tdt" and any(
            target.id == "local-onnx"
            and provider_for_resolution.configuration(target.id, capability, engine)
            == "not_configured"
            for target in own_targets
        ):
            actions.append(
                self._setup.engine_setup(PARAKEET_TDT_SETUP_REF, capabilities)
            )
        return actions

    def _engine_choices(
        self,
        *,
        router: Any,
        project: Any,
        composition: ExecutionComposition,
        provider_catalog: dict[str, Any],
        action_id: str,
        execution_capability: str | None,
        engines: list[dict[str, Any]],
        has_model_field: bool,
        params: Mapping[str, Any],
        capabilities: SelectorCapabilities,
        cloud: _SelectorCloudContext,
    ) -> list[dict[str, Any]]:
        choices: list[dict[str, Any]] = []
        targets = {target.id: target for target in composition.resolution_targets()}
        for engine in engines:
            engine_id = engine.get("id")
            if not isinstance(engine_id, str):
                continue
            if engine_id == "llm" and has_model_field:
                choices.extend(
                    self._model_choices(
                        router=router,
                        project=project,
                        provider_catalog=provider_catalog,
                        capabilities=capabilities,
                        composition=composition,
                        cloud=cloud,
                        selection_kind="engine_model",
                        engine="llm",
                    )
                )
                continue
            selection = _engine_selection(
                engine_id,
                model=None,
                mixed=has_model_field,
            )
            assert selection is not None
            active_target_id = engine.get("target_id")
            catalog_target = (
                targets.get(active_target_id)
                if isinstance(active_target_id, str)
                else None
            )
            route_options = (
                authored_options(params, execution_capability)
                if execution_capability is not None
                else {}
            )
            route_capable = (
                execution_capability is not None
                and _model_route_selectable(
                    composition,
                    capability=execution_capability,
                    engine=engine_id,
                )
            )
            outcome, selected_route = (
                _resolve_route_choice(
                    composition,
                    capability=execution_capability,
                    engine=engine_id,
                    options=route_options,
                )
                if execution_capability is not None
                else (None, "own")
            )
            static_choice = (
                preferred_static_choice(
                    engine_id,
                    route_options,
                    composition.provider_for_resolution(),
                    capability=execution_capability,
                    route=selected_route if route_capable else None,
                )
                if execution_capability is not None
                else None
            )
            option_refusal = (
                static_choice if isinstance(static_choice, Refusal) else None
            )
            target = (
                _target_for_outcome(composition, outcome)
                if outcome is not None
                else catalog_target
            )
            if target is None and selected_route != "cloud":
                target = catalog_target
            projected_target_id = (
                target.id
                if target is not None
                else (
                    active_target_id
                    if selected_route != "cloud" and isinstance(active_target_id, str)
                    else None
                )
            )
            resolved_target = (
                {
                    "target_id": projected_target_id,
                    "operator": target.operator if target is not None else None,
                    "egress_class": target.egress_class if target is not None else None,
                }
                if projected_target_id is not None
                else None
            )
            destination = (
                {"kind": "unknown", "label": "Frisket Cloud"}
                if target is None and selected_route == "cloud"
                else _engine_destination(engine, target)
            )
            available = bool(engine.get("available"))
            # Catalog-only engines retain their legacy tier check. Routed
            # engines are gated from the exact selected target after resolve.
            policy_forbidden = (
                outcome is None
                and _network_off(project)
                and engine.get("tier") == "hosted"
            )
            status = "ready" if available else "unavailable"
            blocker = (
                None
                if available
                else {
                    "code": "engine_unavailable",
                    "message": str(engine.get("error") or "Engine unavailable."),
                    "field": None,
                }
            )
            setup: dict[str, Any] | None = None
            operation: dict[str, Any] | None = None
            engine_setup_ref = _optional_str(engine.get("setup_ref"))
            downloadable = engine.get("downloadable_models") or []
            artifact_ref = (
                _optional_str(_mapping(downloadable[0]).get("ref"))
                if engine_id == "spacy" and not available and downloadable
                else _optional_str(
                    _mapping(engine.get("downloadable_model")).get("ref")
                )
                if engine_id == "hy_mt2"
                and available
                and not _mapping(engine.get("downloadable_model")).get("installed")
                else _optional_str(_mapping(downloadable[0]).get("ref"))
                if engine_id == "faster_whisper"
                and active_target_id == "local"
                and available
                and params.get("model_size") in (None, "base")
                and downloadable
                and not whisper_setup_ready()
                else None
            )
            if policy_forbidden:
                status = "unavailable"
                blocker = {
                    "code": "project_network_off",
                    "message": str(
                        engine.get("error") or "This project's network setting is off."
                    ),
                    "field": None,
                }
            elif engine_setup_ref is not None:
                operation, blocked = self._setup.model_pull_operations(engine_setup_ref)
                if operation is not None or not available:
                    status = "working" if operation is not None else "needs_setup"
                    blocker = {
                        "code": (
                            "engine_setup_in_progress"
                            if operation is not None
                            else "engine_setup_required"
                        ),
                        "message": (
                            "Engine setup is still in progress."
                            if operation is not None
                            else str(engine.get("error") or "Engine setup is required.")
                        ),
                        "field": None,
                    }
                    setup = {
                        "kind": "engine_setup",
                        "setup_ref": engine_setup_ref,
                        "scope": (
                            "workspace" if self._edition == "solo" else "organization"
                        ),
                        "can_mutate": capabilities.manage_model_downloads,
                        "can_start": capabilities.manage_model_downloads
                        and operation is None
                        and blocked is None,
                        "blocked_by_operation": blocked or operation,
                    }
            elif engine_id == "parakeet-tdt" and active_target_id == "local-onnx":
                if not parakeet_runtime_present():
                    setup = {
                        "kind": "instructions",
                        "title": "Install the local Parakeet runtime",
                        "steps": [
                            "Install Frisket's standard data tier in the app environment.",
                            "Restart Frisket after installation.",
                        ],
                        "url": None,
                    }
                    blocker = {
                        "code": "engine_runtime_missing",
                        "message": str(
                            engine.get("error") or "Engine runtime unavailable."
                        ),
                        "field": None,
                    }
                elif not parakeet_setup_ready():
                    operation, blocked = self._setup.model_pull_operations(
                        PARAKEET_TDT_SETUP_REF
                    )
                    status = "working" if operation is not None else "needs_setup"
                    blocker = {
                        "code": "engine_artifacts_missing",
                        "message": str(
                            engine.get("error") or "Engine setup is required."
                        ),
                        "field": None,
                    }
                    setup = {
                        "kind": "engine_setup",
                        "setup_ref": PARAKEET_TDT_SETUP_REF,
                        "scope": "organization"
                        if self._edition != "solo"
                        else "workspace",
                        "can_mutate": capabilities.manage_model_downloads,
                        "can_start": capabilities.manage_model_downloads
                        and operation is None
                        and blocked is None,
                        "blocked_by_operation": blocked or operation,
                    }
            elif (
                self._edition == "solo"
                and engine_id == "docling"
                and active_target_id == LOCAL_MODELS_TARGET_ID
            ):
                operation, blocked = self._setup.model_pull_operations(
                    DOCLING_SETUP_REF
                )
                if operation is not None or not available:
                    status = "working" if operation is not None else "needs_setup"
                    blocker = {
                        "code": (
                            "engine_setup_in_progress"
                            if operation is not None
                            else "engine_runtime_missing"
                        ),
                        "message": (
                            "Local Docling setup is still in progress."
                            if operation is not None
                            else str(
                                engine.get("error")
                                or "Install the local Docling engine."
                            )
                        ),
                        "field": None,
                    }
                    setup = {
                        "kind": "engine_setup",
                        "setup_ref": DOCLING_SETUP_REF,
                        "scope": "workspace",
                        "can_mutate": capabilities.manage_model_downloads,
                        "can_start": capabilities.manage_model_downloads
                        and operation is None
                        and blocked is None,
                        "blocked_by_operation": blocked or operation,
                    }
            elif not available and active_target_id == CLOUDFLARE_CLEF_TARGET_ID:
                setup = {
                    "kind": "instructions",
                    "title": "Connect Cloudflare",
                    "steps": [
                        str(
                            engine.get("error")
                            or "Configure your Cloudflare credentials."
                        ),
                        (
                            "Use your Cloudflare account ID and a Workers AI API token. "
                            "Set them in the environment running frisket, then restart frisket."
                            if self._edition == "solo"
                            else "Ask the server administrator to configure these Cloudflare "
                            "credentials and restart frisket."
                        ),
                    ],
                    "url": None,
                }
            elif not available and (
                active_target_id == MODELS_GATEWAY_TARGET_ID
                or engine.get("tier") == "sidecar"
            ):
                status = "needs_setup"
                setup = self._setup.models_gateway(capabilities)
                blocker = {
                    "code": "models_gateway_required",
                    "message": str(
                        engine.get("error") or "Models gateway setup is required."
                    ),
                    "field": None,
                }
            elif artifact_ref is not None:
                operation, blocked = self._setup.model_pull_operations(artifact_ref)
                status = "working" if operation is not None else "needs_setup"
                blocker = {
                    "code": "engine_artifacts_missing",
                    "message": "Download the required engine model before running.",
                    "field": None,
                }
                setup = {
                    "kind": "artifact_download",
                    "setup_ref": artifact_ref,
                    "scope": "workspace" if self._edition == "solo" else "organization",
                    "can_mutate": capabilities.manage_model_downloads,
                    "can_start": capabilities.manage_model_downloads
                    and operation is None
                    and blocked is None,
                    "blocked_by_operation": blocked or operation,
                }
            elif not available and (
                engine_id == "datalab" or _provider_from_qualified(engine_id) in ENV_VAR
            ):
                provider = (
                    "datalab"
                    if engine_id == "datalab"
                    else cast(str, _provider_from_qualified(engine_id))
                )
                status = "needs_setup"
                setup = self._setup.api_key(
                    router=router,
                    project=project,
                    provider=provider,
                    capabilities=capabilities,
                )
                blocker = {
                    "code": "provider_key_required",
                    "message": str(
                        engine.get("error") or "Provider setup is required."
                    ),
                    "field": None,
                }
            elif (
                action_id == "map.translate"
                and engine_id == "opus_mt"
                and available
                and _opus_pair_supported(engine, params)
                and _opus_pair_key(params) not in (engine.get("models") or [])
            ):
                setup = {
                    "kind": "first_use_download",
                    "disclosure": "Downloads the required language model on first use.",
                }
            if option_refusal is not None and not policy_forbidden:
                status = "unavailable"
                setup = None
                blocker = {
                    "code": "engine_options_unsupported",
                    "message": option_refusal.remedy,
                    "field": None,
                }
            elif (
                classification_blocker := _classification_options_blocker(
                    action_id, params, engine
                )
            ) is not None:
                status = "unavailable"
                if operation is None:
                    setup = None
                blocker = classification_blocker
            elif (
                action_id == "map.translate"
                and engine_id == "opus_mt"
                and available
                and _opus_pair_supported(engine, params) is False
            ):
                status = "unavailable"
                setup = None
                blocker = {
                    "code": "language_pair_unsupported",
                    "message": "Select a supported source and target language pair.",
                    "field": "language",
                }
            elif status == "unavailable" and setup is None and not policy_forbidden:
                setup = {
                    "kind": "instructions",
                    "title": "Set up this engine",
                    "steps": [
                        str(
                            engine.get("error")
                            or "Review the engine runtime and model server in AI Providers settings."
                        )
                    ],
                    "url": None,
                }
            candidate_setup = setup
            presentation = (
                _execution_presentation(
                    composition,
                    outcome,
                    capability=execution_capability,
                    engine=engine_id,
                    fallback_target=resolved_target,
                    fallback_destination=destination,
                    status=status,
                    blocker=blocker,
                    setup=setup,
                    active_operation=operation,
                    cloud_setup_for_lane=lambda lane: cloud.setup(payer_lane=lane),
                    cloud_account_key_setup=cloud.account_key_setup,
                    balance_label=_balance_label(cloud),
                    balance_url=cloud.balance_url,
                    settings_url=cloud.project_settings_url,
                    network_off=_network_off(project),
                )
                if outcome is not None and execution_capability is not None
                else None
            )
            if presentation is not None:
                cloud_setup = cloud.setup()
                if (
                    route_capable
                    and selected_route == "cloud"
                    and presentation.target is None
                ):
                    assert isinstance(outcome, Refusal)
                    presentation = _selected_cloud_without_target(
                        presentation,
                        refusal=outcome,
                        cloud_setup=cloud_setup,
                    )
                target = presentation.target
                resolved_target = presentation.resolved_target
                destination = presentation.processing_destination
                status = presentation.status
                blocker = presentation.blocker
                setup = presentation.setup
                operation = presentation.active_operation
            configuration_candidates = (
                self._engine_configuration_candidates(
                    router=router,
                    project=project,
                    composition=composition,
                    capability=execution_capability,
                    engine=engine_id,
                    capabilities=capabilities,
                    candidate_setup=candidate_setup,
                )
                if execution_capability is not None
                else [candidate_setup]
            )
            route_selection = (
                _route_selection(
                    composition,
                    capability=execution_capability,
                    engine=engine_id,
                    options=route_options,
                    selected=selected_route,
                    can_mutate=capabilities.may_author_actions,
                    cloud_unknown=cloud.setup() is not None,
                    network_off=_network_off(project),
                )
                if route_capable and execution_capability is not None
                else None
            )
            configuration_actions = _configuration_actions(
                *configuration_candidates,
                active_setup=setup,
            )
            if route_selection is not None:
                configuration_actions = [
                    action
                    for action in configuration_actions
                    if action.get("kind") not in {"api_key", "models_gateway"}
                ]
            facts = _engine_facts(
                engine,
                target,
                include_active_route=not (selected_route == "cloud" and target is None),
            )
            if target is not None and execution_capability is not None:
                token_facts = _cloud_model_facts(
                    composition, target, engine_id, execution_capability
                )
                if token_facts:
                    facts = [
                        fact for fact in facts if fact["kind"] != "rate"
                    ] + token_facts
            choices.append(
                _choice(
                    selection=selection,
                    label=str(engine.get("label") or engine_id),
                    summary=destination["label"],
                    description=str(engine.get("description") or ""),
                    model_card_url=_optional_str(engine.get("model_card_url")),
                    resolved_target=resolved_target,
                    processing_destination=destination,
                    facts=facts,
                    status=status,
                    can_author=capabilities.may_author_actions
                    and status != "unavailable",
                    can_run=capabilities.may_run_actions and status == "ready",
                    blocker=blocker,
                    setup=setup,
                    configuration_actions=configuration_actions,
                    execution_route=(
                        presentation.execution_route
                        if presentation is not None
                        else _execution_route(
                            composition,
                            target,
                            capability=execution_capability,
                            engine=engine_id,
                        )
                        if target is not None and execution_capability is not None
                        else None
                    ),
                    route_selection=route_selection,
                    active_operation=operation,
                )
            )
        return choices

    def _embedding_choice(
        self,
        *,
        router: Any,
        project: Any,
        row: dict[str, Any],
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        provider = str(row.get("provider_id") or "")
        model = str(row.get("model_id") or "")
        compatible = bool(row.get("modality_compatible"))
        available = bool(row.get("available")) and compatible and bool(model)
        placeholder = bool(row.get("dimension_discovery_required")) and not model
        status = "ready" if available else "unavailable"
        setup = None
        provider_setup = (
            self._setup.api_key(
                router=router,
                project=project,
                provider=provider,
                capabilities=capabilities,
            )
            if provider in ENV_VAR
            else None
        )
        reason = row.get("disabled_reason") or row.get("error")
        blocker = None
        if not available:
            if placeholder:
                blocker = {
                    "code": "custom_model_required",
                    "message": "Enter a provider model ID to continue.",
                    "field": "model",
                }
            elif not compatible:
                blocker = {
                    "code": "embedding_modality_unsupported",
                    "message": str(
                        reason or "This model does not support the requested modality."
                    ),
                    "field": "modality",
                }
            elif provider in ENV_VAR:
                status = "needs_setup"
                setup = provider_setup
                blocker = {
                    "code": "provider_key_required",
                    "message": str(reason or "Provider setup is required."),
                    "field": None,
                }
            else:
                blocker = {
                    "code": "embedding_model_unavailable",
                    "message": str(reason or "Embedding model unavailable."),
                    "field": None,
                }
        target, destination = _embedding_target(row)
        return _choice(
            selection={"kind": "embedding", "provider": provider, "model": model},
            label=str(row.get("label") or model or provider),
            summary="",
            description="",
            model_card_url=None,
            resolved_target=target,
            processing_destination=destination,
            facts=_embedding_facts(row),
            status=status,
            can_author=capabilities.may_author_actions and status != "unavailable",
            can_run=capabilities.may_run_actions and status == "ready",
            blocker=blocker,
            setup=setup,
            configuration_actions=_configuration_actions(
                provider_setup,
                active_setup=setup,
            ),
        )


__all__ = [
    "SelectorCapabilities",
    "SelectorCapabilitiesFor",
    "SelectorModelsGatewayStatusFor",
    "SelectorChoiceService",
    "SelectorChoicesError",
]
