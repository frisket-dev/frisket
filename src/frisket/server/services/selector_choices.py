"""Viewer-readable engine and model selector projection.

This service composes existing action, model, embedding, execution-target,
credential, and artifact owners.  It performs no inference, estimate, download,
or runtime-start operation.  The only network read it can cause is the existing
bounded configured-local-endpoint model listing in ``build_provider_catalog``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast


from frisket.authoring.action_metadata import action_available_in_edition
from frisket.authoring.copilot import default_copilot_model
from frisket.contracts.http.selector_choices import (
    ActionSelectorSubject,
    CopilotSelectorSubject,
    EmbeddingSelectorSubject,
    SelectorChoicesQuery,
    SelectorChoicesResponse,
)
from frisket.engine._workers.parakeet_artifacts import parakeet_setup_ready
from frisket.engine.jobs.engine_setup import PARAKEET_TDT_SETUP_REF
from frisket.execution.definitions import (
    MODELS_GATEWAY_TARGET_ID,
    parakeet_runtime_present,
)
from frisket.execution.provider import ExecutionComposition
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.resolver import Refusal, preferred_static_choice
from frisket.server.action_catalog_hints import (
    project_action_catalog_payload_with_launcher_hints,
)
from frisket.server.embedding_catalog import embedding_provider_catalog_payload
from frisket.server.provider_config import (
    ENV_VAR,
    build_provider_catalog,
)
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace
from frisket.server.services.selector_choices_setup import SelectorSetupService
from frisket.server.services.selector_choices_capabilities import (
    SelectorCapabilities,
    SelectorCapabilitiesFor,
    SelectorModelsGatewayStatusFor,
)

from frisket.server.services.selector_choices_projection import (
    _network_off,
    _mapping,
    _schema_default,
    _optional_str,
    _provider_identity,
    _provider_from_qualified,
    _default_action_model,
    _model_selection,
    _engine_selection,
    _choice,
    _response,
    _engine_depends_on,
    _engine_destination,
    _engine_facts,
    _model_facts,
    _model_target,
    _embedding_target,
    _embedding_facts,
    _opus_pair_supported,
)


_TARGET_OVERRIDE_FIELDS = frozenset({"target", "target_id", "execution_target"})


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
        composition = self._workspace.execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(request_context),
        )
        provider_catalog = (
            build_provider_catalog(
                self._workspace.root,
                network_off=_network_off(project),
                local_endpoints=router.local_endpoints,
                local_endpoint_authority="instance"
                if self._edition == "solo"
                else "organization",
            )
            if not isinstance(subject, EmbeddingSelectorSubject)
            else {"providers": []}
        )
        configured_providers = set(router.providers())
        for provider in provider_catalog.get("providers", []):
            if isinstance(provider, dict) and provider.get("id") in ENV_VAR:
                provider["configured"] = provider["id"] in configured_providers
                provider["credential_source"] = router.credential_source_for(
                    provider["id"]
                )
        if isinstance(subject, ActionSelectorSubject):
            payload = self._action_choices(
                project_id,
                project=project,
                router=router,
                composition=composition,
                provider_catalog=provider_catalog,
                subject=subject,
                capabilities=capabilities,
            )
        elif isinstance(subject, CopilotSelectorSubject):
            payload = self._copilot_choices(
                project_id,
                project=project,
                router=router,
                composition=composition,
                provider_catalog=provider_catalog,
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

    def _action_choices(
        self,
        project_id: str,
        *,
        project: Any,
        router: Any,
        composition: ExecutionComposition,
        provider_catalog: dict[str, Any],
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
            choices = self._model_choices(
                router=router,
                project=project,
                provider_catalog=provider_catalog,
                capabilities=capabilities,
                selection_kind="model",
            )
            current_selection = _model_selection(subject.params.get(subject.field))
            default_model = _default_action_model(provider_catalog, router)
            default_selection = _model_selection(default_model)
            depends_on: list[str] = []
        else:
            engines = [
                dict(row) for row in hints.get("engines", []) if isinstance(row, dict)
            ]
            has_model_field = controls.get("model") == "model" and "model" in properties
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
                _default_action_model(provider_catalog, router)
                if has_model_field and default_engine == "llm"
                else None
            )
            default_selection = _engine_selection(
                default_engine,
                model=default_model,
                mixed=has_model_field,
            )
            depends_on = _engine_depends_on(properties, engines)

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
        )

    def _copilot_choices(
        self,
        project_id: str,
        *,
        project: Any,
        router: Any,
        composition: ExecutionComposition,
        provider_catalog: dict[str, Any],
        subject: CopilotSelectorSubject,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        del composition
        choices = self._model_choices(
            router=router,
            project=project,
            provider_catalog=provider_catalog,
            capabilities=capabilities,
            selection_kind="model",
        )
        return _response(
            project_id,
            subject={"kind": "copilot"},
            depends_on=[],
            choices=choices,
            current_selection=_model_selection(subject.model),
            default_selection=_model_selection(default_copilot_model(router)),
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
        catalog = embedding_provider_catalog_payload(
            router=router,
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
        selection_kind: str,
        engine: str | None = None,
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
                        setup = self._setup.api_key(
                            router=router,
                            project=project,
                            provider=provider_id,
                            capabilities=capabilities,
                        )
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
                    spend = project.provider_spend_state(provider_id)
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
                choices.append(
                    _choice(
                        selection=selection,
                        label=str(model.get("label") or model["id"]),
                        summary="",
                        description="",
                        model_card_url=None,
                        resolved_target=target,
                        processing_destination=destination,
                        facts=facts,
                        status=status,
                        can_author=capabilities.may_author_actions
                        and status != "unavailable",
                        can_run=capabilities.may_run_actions and status == "ready",
                        blocker=blocker,
                        setup=setup,
                    )
                )
        return choices

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
            target = (
                targets.get(active_target_id)
                if isinstance(active_target_id, str)
                else None
            )
            resolved_target = (
                {
                    "target_id": active_target_id,
                    "operator": target.operator if target is not None else None,
                    "egress_class": target.egress_class if target is not None else None,
                }
                if isinstance(active_target_id, str)
                else None
            )
            destination = _engine_destination(engine, target)
            available = bool(engine.get("available"))
            option_refusal = None
            if execution_capability is not None:
                preferred = preferred_static_choice(
                    engine_id,
                    authored_options(params, execution_capability),
                    list(targets.values()),
                    capability=execution_capability,
                )
                if isinstance(preferred, Refusal):
                    option_refusal = preferred
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
                else None
            )
            if engine_id == "parakeet-tdt" and active_target_id == "local-onnx":
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
            elif not available and _provider_from_qualified(engine_id) in ENV_VAR:
                provider = cast(str, _provider_from_qualified(engine_id))
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
                and f"{params.get('language')}-{params.get('target_language')}"
                not in (engine.get("models") or [])
            ):
                setup = {
                    "kind": "first_use_download",
                    "disclosure": "Downloads the required language model on first use.",
                }
            if option_refusal is not None:
                status = "unavailable"
                setup = None
                blocker = {
                    "code": "engine_options_unsupported",
                    "message": option_refusal.remedy,
                    "field": None,
                }
            elif (
                action_id == "map.translate"
                and engine_id == "opus_mt"
                and available
                and not _opus_pair_supported(engine, params)
            ):
                status = "unavailable"
                setup = None
                blocker = {
                    "code": "language_pair_unsupported",
                    "message": "Select a supported source and target language pair.",
                    "field": "language",
                }
            elif status == "unavailable" and setup is None:
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
            choices.append(
                _choice(
                    selection=selection,
                    label=str(engine.get("label") or engine_id),
                    summary=destination["label"],
                    description=str(engine.get("description") or ""),
                    model_card_url=_optional_str(engine.get("model_card_url")),
                    resolved_target=resolved_target,
                    processing_destination=destination,
                    facts=_engine_facts(engine, target),
                    status=status,
                    can_author=capabilities.may_author_actions
                    and status != "unavailable",
                    can_run=capabilities.may_run_actions and status == "ready",
                    blocker=blocker,
                    setup=setup,
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
                setup = self._setup.api_key(
                    router=router,
                    project=project,
                    provider=provider,
                    capabilities=capabilities,
                )
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
        )


__all__ = [
    "SelectorCapabilities",
    "SelectorCapabilitiesFor",
    "SelectorModelsGatewayStatusFor",
    "SelectorChoiceService",
    "SelectorChoicesError",
]
