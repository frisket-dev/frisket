"""Viewer-readable engine and model selector projection.

This service composes existing action, model, embedding, execution-target,
credential, and artifact owners.  It performs no inference, estimate, download,
or runtime-start operation.  The only network read it can cause is the existing
bounded configured-local-endpoint model listing in ``build_provider_catalog``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from fastapi import Request

from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.authoring.action_metadata import action_available_in_edition
from frisket.authoring.copilot import default_copilot_model
from frisket.contracts.http.selector_choices import (
    ActionSelectorSubject,
    CopilotSelectorSubject,
    EmbeddingSelectorSubject,
    SelectorChoicesQuery,
    SelectorChoicesResponse,
)
from frisket.engine.jobs import model_pull_store
from frisket.execution.definitions import (
    MODELS_GATEWAY_TARGET_ID,
    MODELS_GATEWAY_TOKEN_ENV,
    MODELS_GATEWAY_URL_ENV,
    parakeet_artifacts_present,
    parakeet_runtime_present,
)
from frisket.execution.provider import ExecutionComposition
from frisket.server.action_catalog_hints import (
    project_action_catalog_payload_with_launcher_hints,
)
from frisket.server.embedding_catalog import embedding_provider_catalog_payload
from frisket.server.provider_config import (
    ENV_VAR,
    PROVIDER_LABELS,
    build_provider_catalog,
    provider_key_status,
)
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace


_PARAKEET_TDT_SETUP_REF = "engine-setup:parakeet-tdt.local-onnx@1"
_TARGET_OVERRIDE_FIELDS = frozenset({"target", "target_id", "execution_target"})
_GROUP_STATUS_ORDER = {"ready": 0, "working": 1, "needs_setup": 2, "unavailable": 3}


@dataclass(frozen=True, slots=True)
class SelectorCapabilities:
    may_author_actions: bool = False
    may_run_actions: bool = False
    configure_workspace_credentials: bool = False
    configure_project_credentials: bool = False
    configure_organization_credentials: bool = False
    manage_model_downloads: bool = False
    configure_models_gateway: bool = False


SelectorCapabilitiesFor = Callable[[Request, str], SelectorCapabilities]


class SelectorChoicesError(RouteError):
    pass


class SelectorChoiceService:
    def __init__(self, workspace: Workspace, *, edition: str = "solo") -> None:
        self._workspace = workspace
        self._edition = edition

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
        router = self._workspace.router_for(project)
        composition = self._workspace.execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(request_context),
        )
        provider_catalog = build_provider_catalog(
            self._workspace.root,
            network_off=_network_off(project),
        )
        configured_providers = set(router.providers())
        for provider in provider_catalog.get("providers", []):
            if isinstance(provider, dict) and provider.get("id") in ENV_VAR:
                provider["configured"] = provider["id"] in configured_providers
        subject = query.subject
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
                project=project,
                composition=composition,
                provider_catalog=provider_catalog,
                action_id=subject.action_id,
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
            choices.append(
                self._embedding_choice(
                    project=project,
                    row=candidate,
                    capabilities=capabilities,
                )
            )
        default = next(
            (
                choice["authored_selection"]
                for choice, row in zip(
                    choices, catalog.get("providers", []), strict=True
                )
                if isinstance(row, dict)
                and row.get("recommended")
                and row.get("modality_compatible")
            ),
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
                        setup = self._api_key_setup(
                            project=project,
                            provider=provider_id,
                            capabilities=capabilities,
                        )
                    else:
                        status = "unavailable"
                facts = _model_facts(model)
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
        project: Any,
        composition: ExecutionComposition,
        provider_catalog: dict[str, Any],
        action_id: str,
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
                elif not parakeet_artifacts_present():
                    operation, blocked = self._model_pull_operations(
                        _PARAKEET_TDT_SETUP_REF
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
                        "setup_ref": _PARAKEET_TDT_SETUP_REF,
                        "scope": "organization"
                        if self._edition != "solo"
                        else "workspace",
                        "can_mutate": capabilities.manage_model_downloads,
                        "can_start": capabilities.manage_model_downloads
                        and operation is None
                        and blocked is None,
                        "blocked_by_operation": blocked or operation,
                    }
            elif not available and active_target_id == MODELS_GATEWAY_TARGET_ID:
                status = "needs_setup"
                setup = self._models_gateway_setup(capabilities)
                blocker = {
                    "code": "models_gateway_required",
                    "message": str(
                        engine.get("error") or "Models gateway setup is required."
                    ),
                    "field": None,
                }
            elif not available and _provider_from_qualified(engine_id) in ENV_VAR:
                provider = cast(str, _provider_from_qualified(engine_id))
                status = "needs_setup"
                setup = self._api_key_setup(
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
            ):
                setup = {
                    "kind": "first_use_download",
                    "disclosure": "Downloads the required language model on first use.",
                }
            choices.append(
                _choice(
                    selection=selection,
                    label=str(engine.get("label") or engine_id),
                    summary=_engine_summary(engine),
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
                setup = self._api_key_setup(
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

    def _api_key_setup(
        self,
        *,
        project: Any,
        provider: str,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        status_rows = {
            row["id"]: row for row in provider_key_status(self._workspace.root)
        }
        workspace_status = status_rows.get(provider, {})
        project_row = project.provider_key_catalog_rows().get(provider)
        source = _credential_source(self._workspace.router_for(project), provider)
        org_configured = source == "organization"
        platform_configured = source == "platform"
        env_name = ENV_VAR[provider]
        return {
            "kind": "api_key",
            "provider": provider,
            "scopes": [
                {
                    "scope": "environment",
                    "configured": workspace_status.get("source") == "env",
                    "can_mutate": False,
                    "source": "environment"
                    if workspace_status.get("source") == "env"
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": None,
                    "hint": workspace_status.get("hint")
                    if workspace_status.get("source") == "env"
                    else None,
                },
                {
                    "scope": "workspace",
                    "configured": workspace_status.get("source") == "local_file",
                    "can_mutate": capabilities.configure_workspace_credentials,
                    "source": "workspace"
                    if workspace_status.get("source") == "local_file"
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": "workspace_ai_providers",
                    "hint": workspace_status.get("hint")
                    if workspace_status.get("source") == "local_file"
                    else None,
                },
                {
                    "scope": "project",
                    "configured": project_row is not None,
                    "can_mutate": capabilities.configure_project_credentials,
                    "source": "project" if project_row is not None else "missing",
                    "environment_names": [env_name],
                    "settings_location": "project_ai_providers",
                    "hint": project_row.get("hint") if project_row else None,
                },
                {
                    "scope": "organization",
                    "configured": org_configured or platform_configured,
                    "can_mutate": capabilities.configure_organization_credentials
                    and not platform_configured,
                    "source": "platform"
                    if platform_configured
                    else "organization"
                    if org_configured
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": "organization_ai_providers",
                    "hint": None,
                },
            ],
        }

    def _models_gateway_setup(
        self, capabilities: SelectorCapabilities
    ) -> dict[str, Any]:
        env_configured = bool(os.environ.get(MODELS_GATEWAY_URL_ENV)) and bool(
            os.environ.get(MODELS_GATEWAY_TOKEN_ENV)
        )
        scope = "organization" if self._edition != "solo" else "workspace"
        return {
            "kind": "models_gateway",
            "scopes": [
                {
                    "scope": "environment",
                    "configured": env_configured,
                    "can_mutate": False,
                    "source": "environment" if env_configured else "missing",
                    "environment_names": [
                        MODELS_GATEWAY_URL_ENV,
                        MODELS_GATEWAY_TOKEN_ENV,
                    ],
                    "settings_location": None,
                    "hint": None,
                },
                {
                    "scope": scope,
                    "configured": False,
                    "can_mutate": capabilities.configure_models_gateway,
                    "source": "missing",
                    "environment_names": [
                        MODELS_GATEWAY_URL_ENV,
                        MODELS_GATEWAY_TOKEN_ENV,
                    ],
                    "settings_location": f"{scope}_models_gateway",
                    "hint": None,
                },
            ],
        }

    def _model_pull_operations(
        self, setup_ref: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        rows = model_pull_store.list_recent(
            self._workspace.queue.engine,
            str(self._workspace.root),
            limit=20,
        )
        active = [row for row in rows if row.is_active]
        matching = next((row for row in active if row.model_ref == setup_ref), None)
        blocked = next((row for row in active if row.model_ref != setup_ref), None)
        return (
            model_pull_store.to_dto(matching) if matching is not None else None,
            model_pull_store.to_dto(blocked) if blocked is not None else None,
        )


def _network_off(project: Any) -> bool:
    try:
        return project.effective_network_policy() == "off"
    except Exception:
        return False


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _schema_default(value: Any) -> Any:
    return value.get("default") if isinstance(value, Mapping) else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _provider_identity(provider: Mapping[str, Any]) -> str | None:
    value = (
        provider.get("endpoint_id")
        if provider.get("kind") == "local_http"
        else provider.get("id")
    )
    return value if isinstance(value, str) and value else None


def _provider_from_qualified(value: str) -> str | None:
    provider, separator, _model = value.partition("/")
    return provider if separator else None


def _credential_source(router: Any, provider: str) -> str:
    raw = router.credential_source_for(provider)
    if raw == "project_key":
        return "project"
    if raw in {"org_byok", "org_key"}:
        return "organization"
    if raw in {"platform_key", "platform"}:
        return "platform"
    if raw in {"local", "workspace"}:
        return "workspace"
    return "missing"


def _default_action_model(provider_catalog: dict[str, Any], router: Any) -> str | None:
    usable: set[str] = set()
    rows = [
        row for row in provider_catalog.get("providers", []) if isinstance(row, dict)
    ]
    for row in rows:
        identity = _provider_identity(row)
        if identity is None:
            continue
        if row.get("kind") == "local_http":
            if row.get("reachable") and row.get("models"):
                usable.add(identity)
        elif row.get("configured") or identity in router.providers():
            usable.add(identity)
    for provider, entries in MODEL_ENTRIES.items():
        if provider in usable and entries:
            return f"{provider}/{entries[0]['id']}"
    for row in rows:
        identity = _provider_identity(row)
        if identity in usable and row.get("models"):
            return row["models"][0].get("id")
    return None


def _model_selection(value: Any) -> dict[str, Any] | None:
    return (
        {"kind": "model", "model": value} if isinstance(value, str) and value else None
    )


def _engine_selection(value: Any, *, model: Any, mixed: bool) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    if mixed:
        if value == "llm" and not (isinstance(model, str) and model):
            return None
        return {
            "kind": "engine_model",
            "engine": value,
            "model": model if isinstance(model, str) and model else None,
        }
    return {"kind": "engine", "engine": value}


def _selection_key(selection: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = selection.get("kind")
    if kind == "engine":
        return (kind, selection.get("engine"))
    if kind == "model":
        return (kind, selection.get("model"))
    if kind == "engine_model":
        return (kind, selection.get("engine"), selection.get("model"))
    return (kind, selection.get("provider"), selection.get("model"))


def _choice_id(selection: Mapping[str, Any]) -> str:
    return "selector:" + ":".join(
        "" if item is None else str(item) for item in _selection_key(selection)
    )


def _choice(
    *,
    selection: dict[str, Any],
    label: str,
    summary: str,
    description: str,
    model_card_url: str | None,
    resolved_target: dict[str, Any] | None,
    processing_destination: dict[str, str],
    facts: list[dict[str, Any]],
    status: str,
    can_author: bool,
    can_run: bool,
    blocker: dict[str, Any] | None,
    setup: dict[str, Any] | None,
    active_operation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "choice_id": _choice_id(selection),
        "label": label,
        "summary": summary,
        "description": description,
        "model_card_url": model_card_url,
        "authored_selection": selection,
        "resolved_target": resolved_target,
        "processing_destination": processing_destination,
        "facts": facts,
        "status": status,
        "can_author": can_author,
        "can_run": can_run,
        "blocker": blocker,
        "setup": setup,
        "active_operation": active_operation,
        "is_default": False,
        "is_current": False,
    }


def _response(
    project_id: str,
    *,
    subject: dict[str, Any],
    depends_on: list[str],
    choices: list[dict[str, Any]],
    current_selection: dict[str, Any] | None,
    default_selection: dict[str, Any] | None,
) -> dict[str, Any]:
    current_key = _selection_key(current_selection) if current_selection else None
    default_key = _selection_key(default_selection) if default_selection else None
    current_choice_id = None
    default_choice_id = None
    for choice in choices:
        key = _selection_key(choice["authored_selection"])
        if current_key is not None and key == current_key:
            choice["is_current"] = True
            current_choice_id = choice["choice_id"]
        if default_key is not None and key == default_key:
            choice["is_default"] = True
            default_choice_id = choice["choice_id"]
    orphan = None
    if current_selection is not None and current_choice_id is None:
        orphan = _orphan(current_selection)
        current_choice_id = orphan["choice_id"]
    return {
        "schema_version": "frisket.selector_choices.v1",
        "project_id": project_id,
        "subject": subject,
        "depends_on": depends_on,
        "current_choice_id": current_choice_id,
        "default_choice_id": default_choice_id,
        "groups": _groups(choices),
        "orphaned_current": orphan,
    }


def _orphan(selection: dict[str, Any]) -> dict[str, Any]:
    value = next(
        (
            str(selection[name])
            for name in ("model", "engine", "provider")
            if selection.get(name)
        ),
        "Unknown choice",
    )
    choice = _choice(
        selection=selection,
        label=value,
        summary="Saved choice",
        description="This saved choice is no longer offered in the current project.",
        model_card_url=None,
        resolved_target=None,
        processing_destination={"kind": "unknown", "label": "Destination unknown"},
        facts=[],
        status="unavailable",
        can_author=False,
        can_run=False,
        blocker={
            "code": "unknown_saved_choice",
            "message": "Choose an available replacement before running.",
            "field": None,
        },
        setup=None,
    )
    choice["is_current"] = True
    return choice


def _group_identity(choice: Mapping[str, Any]) -> tuple[str, str, str]:
    target = _mapping(choice.get("resolved_target"))
    destination = _mapping(choice.get("processing_destination"))
    selection = _mapping(choice.get("authored_selection"))
    target_id = target.get("target_id")
    if destination.get("kind") == "local":
        return "local", "local", "On this device"
    if destination.get("kind") in {"operator_network", "unknown"} and target_id:
        return f"server:{target_id}", "server", "Models server"
    provider = selection.get("provider")
    if not provider:
        model = selection.get("model")
        engine = selection.get("engine")
        provider = _provider_from_qualified(model) if isinstance(model, str) else None
        provider = provider or (
            _provider_from_qualified(engine) if isinstance(engine, str) else None
        )
    provider = provider or target.get("operator") or "external"
    return (
        f"provider:{provider}",
        "provider",
        PROVIDER_LABELS.get(str(provider), str(provider).replace("-", " ").title()),
    )


def _groups(choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for choice in choices:
        group_id, kind, label = _group_identity(choice)
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "kind": kind,
                "label": label,
                "status": "unavailable",
                "choices": [],
            },
        )
        group["choices"].append(choice)
        if _GROUP_STATUS_ORDER[choice["status"]] < _GROUP_STATUS_ORDER[group["status"]]:
            group["status"] = choice["status"]
    return list(groups.values())


def _engine_depends_on(
    properties: Mapping[str, Any], engines: list[dict[str, Any]]
) -> list[str]:
    names: list[str] = []
    has_language = any("language" in engine for engine in engines)
    has_transcription_options = any(
        "transcription_options" in engine for engine in engines
    )
    has_diarization = any("diarization" in engine for engine in engines)
    for name in properties:
        if name in {"language", "target_language"} and has_language:
            names.append(name)
        elif (
            name in {"model_size", "vad", "context", "clean"}
            and has_transcription_options
        ):
            names.append(name)
        elif (
            name in {"diarize", "num_speakers", "min_speakers", "max_speakers"}
            and has_diarization
        ):
            names.append(name)
    return names


def _engine_summary(engine: Mapping[str, Any]) -> str:
    tier = engine.get("tier")
    return {
        "local": "Runs on this device.",
        "sidecar": "Runs on a configured models server.",
        "hosted": "Runs through an external provider.",
    }.get(tier, "")


def _engine_destination(engine: Mapping[str, Any], target: Any) -> dict[str, str]:
    if target is not None:
        return _destination(target.egress_class, target.operator)
    tier = engine.get("tier")
    if tier == "local":
        return {"kind": "local", "label": "Runs on this device"}
    if tier == "sidecar":
        return {"kind": "unknown", "label": "Configured models server"}
    if tier == "hosted":
        return {"kind": "external", "label": "External provider"}
    return {"kind": "unknown", "label": "Destination unknown"}


def _destination(egress_class: str | None, operator: str | None) -> dict[str, str]:
    if egress_class == "none":
        return {"kind": "local", "label": "Runs on this device"}
    if egress_class in {"operator_lan", "frisket_dedicated_org"}:
        return {"kind": "operator_network", "label": "Runs on your infrastructure"}
    if egress_class in {"frisket_shared", "third_party_api"}:
        label = f"Sends data to {operator}" if operator else "External processing"
        return {"kind": "external", "label": label}
    return {"kind": "unknown", "label": "Destination unknown"}


def _engine_facts(engine: Mapping[str, Any], target: Any) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if target is not None and target.operator:
        facts.append({"kind": "text", "label": "Operator", "value": target.operator})
    language = _mapping(engine.get("language"))
    labels = [
        str(row["label"])
        for row in language.get("choices", []) or []
        if isinstance(row, Mapping) and isinstance(row.get("label"), str)
    ]
    if labels:
        facts.append({"kind": "list", "label": "Languages", "values": labels})
    active_target = next(
        (
            row
            for row in engine.get("targets", []) or []
            if isinstance(row, Mapping)
            and row.get("target_id") == engine.get("target_id")
        ),
        {},
    )
    sizes = active_target.get("sizes") if isinstance(active_target, Mapping) else None
    if isinstance(sizes, list) and sizes:
        facts.append(
            {"kind": "list", "label": "Model sizes", "values": [str(v) for v in sizes]}
        )
    diarization = _mapping(
        active_target.get("diarization")
        if isinstance(active_target, Mapping)
        else engine.get("diarization")
    )
    if diarization.get("supported"):
        facts.append(
            {
                "kind": "text",
                "label": "Speaker labels",
                "value": "Included"
                if diarization.get("mode") == "intrinsic"
                else "Optional",
            }
        )
    pricing = _mapping(engine.get("pricing"))
    amount = pricing.get("unit_price_usd")
    if isinstance(amount, int | float) and not isinstance(amount, bool) and amount >= 0:
        facts.append(
            {
                "kind": "rate",
                "label": str(pricing.get("label") or "Published price"),
                "amount": float(amount),
                "currency": "USD",
                "unit": str(pricing.get("unit") or "unit"),
                "source_url": None,
                "updated": None,
            }
        )
    return facts


def _model_facts(model: Mapping[str, Any]) -> list[dict[str, Any]]:
    price = _mapping(model.get("price"))
    facts: list[dict[str, Any]] = []
    for key, label, unit in (
        ("input", "Input price", "million input tokens"),
        ("output", "Output price", "million output tokens"),
    ):
        amount = price.get(key)
        if (
            isinstance(amount, int | float)
            and not isinstance(amount, bool)
            and amount >= 0
        ):
            facts.append(
                {
                    "kind": "rate",
                    "label": label,
                    "amount": float(amount),
                    "currency": "USD",
                    "unit": unit,
                    "source_url": None,
                    "updated": None,
                }
            )
    return facts


def _model_target(provider: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    identity = _provider_identity(provider) or "unknown"
    if provider.get("kind") == "local_http":
        return (
            {"target_id": identity, "operator": None, "egress_class": None},
            {"kind": "unknown", "label": "Configured model server"},
        )
    return (
        {
            "target_id": f"remote-api:{identity}",
            "operator": identity,
            "egress_class": "third_party_api",
        },
        {"kind": "external", "label": f"Sends data to {identity}"},
    )


def _embedding_target(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    provider = str(row.get("provider_id") or "unknown")
    if row.get("local") and row.get("provider_kind") == "local_process":
        return (
            {"target_id": "local", "operator": "self", "egress_class": "none"},
            {"kind": "local", "label": "Runs on this device"},
        )
    if row.get("provider_kind") == "local_http":
        return (
            {"target_id": provider, "operator": None, "egress_class": None},
            {"kind": "unknown", "label": "Configured model server"},
        )
    return (
        {
            "target_id": f"remote-api:{provider}",
            "operator": provider,
            "egress_class": "third_party_api",
        },
        {"kind": "external", "label": f"Sends data to {provider}"},
    )


def _embedding_facts(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for key, label in (("modalities", "Modalities"), ("dimensions", "Dimensions")):
        values = row.get(key)
        if isinstance(values, list) and values:
            facts.append(
                {
                    "kind": "list",
                    "label": label,
                    "values": [str(value) for value in values],
                }
            )
    max_tokens = row.get("max_input_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        facts.append(
            {"kind": "text", "label": "Maximum input tokens", "value": str(max_tokens)}
        )
    pricing = _mapping(row.get("pricing"))
    amount = pricing.get("input_usd_per_million_tokens")
    if isinstance(amount, int | float) and not isinstance(amount, bool) and amount >= 0:
        facts.append(
            {
                "kind": "rate",
                "label": "Input price",
                "amount": float(amount),
                "currency": "USD",
                "unit": "million input tokens",
                "source_url": _optional_str(pricing.get("source_url")),
                "updated": _optional_str(pricing.get("updated")),
            }
        )
    return facts


def _opus_pair_supported(engine: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    source = params.get("language")
    target = params.get("target_language")
    if (
        not isinstance(source, str)
        or not isinstance(target, str)
        or not source
        or not target
    ):
        return False
    pair = f"{source}-{target}"
    return any(
        isinstance(row, Mapping) and row.get("pair") == pair
        for row in engine.get("downloadable_pairs", []) or []
    )


__all__ = [
    "SelectorCapabilities",
    "SelectorCapabilitiesFor",
    "SelectorChoiceService",
    "SelectorChoicesError",
]
