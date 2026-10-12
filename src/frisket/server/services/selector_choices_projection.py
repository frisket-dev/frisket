"""Pure selector grouping, authored values and descriptive facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from frisket.actions.translate_types import TranslationOptions
from frisket.cloud_targets import cloud_fallback_is_blocked
from frisket.execution.definitions import MODELS_GATEWAY_TARGET_ID
from frisket.execution.provider import ExecutionComposition
from frisket.execution.resolver import Refusal, preferred_resolved_engine
from frisket.execution.targets import ExecutionTarget
from frisket.ops.integrations.opus_mt import resolve_pair_codes
from frisket.server.provider_config import PROVIDER_LABELS

_GROUP_STATUS_ORDER = {"ready": 0, "working": 1, "needs_setup": 2, "unavailable": 3}


def _network_off(project: Any) -> bool:
    return project.effective_network_policy() == "off"


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


def _credential_source(
    router: Any, provider: str, *, raw_source: str | None = None
) -> str:
    raw = router.credential_source_for(provider) if raw_source is None else raw_source
    if raw == "project_key":
        return "project"
    if raw in {"env", "environment"}:
        return "environment"
    if raw in {"org_byok", "org_key"}:
        return "organization"
    if raw in {"platform_key", "platform"}:
        return "platform"
    if raw in {"local", "workspace"}:
        return "workspace"
    return "missing"


def _default_action_model(
    choices: list[dict[str, Any]], composition: ExecutionComposition
) -> str | None:
    models = [
        model
        for choice in choices
        if isinstance((model := choice["authored_selection"].get("model")), str)
        and model
    ]
    preferred = preferred_resolved_engine(
        models,
        composition.provider_for_resolution(),
        composition.facts,
    )
    if preferred is not None:
        return preferred
    # Custom local endpoints are catalog-owned rather than static target rows;
    # unconfigured installations retain their curated catalog fallback.
    for status in ("ready", "needs_setup"):
        for choice in choices:
            selection = choice["authored_selection"]
            model = selection.get("model")
            if choice["status"] == status and isinstance(model, str) and model:
                return model
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
        "" if item is None else quote(str(item), safe="")
        for item in _selection_key(selection)
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
    configuration_actions: list[dict[str, Any]] | None = None,
    execution_route: dict[str, Any] | None = None,
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
        "execution_route": execution_route,
        "facts": facts,
        "status": status,
        "can_author": can_author,
        "can_run": can_run,
        "blocker": blocker,
        "setup": setup,
        "configuration_actions": configuration_actions or [],
        "active_operation": active_operation,
        "is_default": False,
        "is_current": False,
    }


def _execution_route(
    composition: ExecutionComposition,
    target: ExecutionTarget,
    *,
    capability: str,
    engine: str,
    balance_label: str | None = None,
    balance_url: str | None = None,
    settings_url: str | None = None,
) -> dict[str, Any]:
    payer = composition.cloud_payer_for(
        target_id=target.id, capability=capability, engine=engine
    )
    presentation = composition.presentation_for(
        target_id=target.id, capability=capability, engine=engine
    )
    if payer is not None:
        kind = "cloud_account_key" if payer.lane == "account_key" else "cloud_credits"
        label = (
            presentation.venue_label if presentation is not None else "Frisket Cloud"
        )
        billed_to = presentation.billing_label if presentation is not None else None
        sends_data_to = label
        account_id = payer.account_id
    elif target.egress_class == "none":
        kind = "local_engine"
        label = "On this computer"
        billed_to = None
        sends_data_to = None
        account_id = None
    elif target.egress_class in {"operator_lan", "frisket_dedicated_org"}:
        kind = "models_server"
        label = "Models server"
        billed_to = None
        sends_data_to = target.operator
        account_id = None
    else:
        kind = "local_key"
        provider = PROVIDER_LABELS.get(target.operator, target.operator)
        label = f"Your {provider} API key"
        billed_to = label
        sends_data_to = provider
        account_id = None
    return {
        "kind": kind,
        "label": label,
        "billed_to": billed_to,
        "sends_data_to": sends_data_to,
        "account_id": account_id,
        "balance_label": balance_label if payer is not None else None,
        "balance_url": balance_url if payer is not None else None,
        "settings_url": settings_url,
        "has_alternative_route": _has_exact_cloud_overlap(
            composition, capability=capability, engine=engine
        ),
    }


def _matching_targets(
    composition: ExecutionComposition, *, capability: str, engine: str
) -> tuple[ExecutionTarget, ...]:
    """Return each target that declares this exact authored work once."""

    return tuple(
        target
        for target in composition.resolution_targets()
        if any(
            support.capability == capability
            and (
                support.engine == engine
                or (
                    support.engine.endswith("/*")
                    and engine.startswith(support.engine[:-1])
                )
            )
            for support in target.engines
        )
    )


def _has_exact_cloud_overlap(
    composition: ExecutionComposition, *, capability: str, engine: str
) -> bool:
    """Whether this engine has both an exact Cloud offer and an own venue."""

    cloud = False
    own = False
    for target in _matching_targets(composition, capability=capability, engine=engine):
        payer = composition.cloud_payer_for(
            target_id=target.id, capability=capability, engine=engine
        )
        if payer is None:
            own = True
        else:
            cloud = True
    return cloud and own


def _target_for_outcome(
    composition: ExecutionComposition, outcome: Any
) -> ExecutionTarget | None:
    if not isinstance(outcome, Refusal):
        return outcome.target
    if outcome.target_id is None:
        return None
    return next(
        (
            target
            for target in composition.resolution_targets()
            if target.id == outcome.target_id
        ),
        None,
    )


def _configuration_action_key(action: Mapping[str, Any]) -> tuple[Any, ...] | None:
    kind = action.get("kind")
    if kind == "api_key":
        return kind, action.get("provider")
    if kind == "models_gateway":
        return (kind,)
    if kind in {"engine_setup", "artifact_download"}:
        return kind, action.get("scope"), action.get("setup_ref")
    return None


def _configuration_actions(
    *actions: dict[str, Any] | None,
    active_setup: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Deduplicate passive configuration operations and exclude remediation."""

    active_key = _configuration_action_key(active_setup or {})
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for action in actions:
        if action is None:
            continue
        key = _configuration_action_key(action)
        if key is None or key == active_key or key in seen:
            continue
        seen.add(key)
        result.append(action)
    return result


@dataclass(frozen=True)
class _ExecutionPresentation:
    target: ExecutionTarget | None
    resolved_target: dict[str, Any] | None
    processing_destination: dict[str, str]
    execution_route: dict[str, Any] | None
    status: str
    blocker: dict[str, Any] | None
    setup: dict[str, Any] | None
    active_operation: dict[str, Any] | None


def _execution_presentation(
    composition: ExecutionComposition,
    outcome: Any,
    *,
    capability: str,
    engine: str,
    fallback_target: dict[str, Any] | None,
    fallback_destination: dict[str, str],
    status: str,
    blocker: dict[str, Any] | None,
    setup: dict[str, Any] | None,
    active_operation: dict[str, Any] | None = None,
    cloud_setup_for_lane: Callable[[str | None], dict[str, Any] | None] | None = None,
    balance_label: str | None = None,
    balance_url: str | None = None,
    settings_url: str | None = None,
    policy_forbidden: bool = False,
    preserve_unbound_catalog: bool = False,
) -> _ExecutionPresentation:
    """Project one selected route without choosing or probing another payer."""

    refusal = outcome if isinstance(outcome, Refusal) else None
    target = _target_for_outcome(composition, outcome)
    resolved_target = fallback_target
    destination = fallback_destination
    route = None
    payer = None
    if target is not None:
        resolved_target = {
            "target_id": target.id,
            "operator": target.operator,
            "egress_class": target.egress_class,
        }
        destination = _destination(target.egress_class, target.operator)
        payer = composition.cloud_payer_for(
            target_id=target.id, capability=capability, engine=engine
        )
        route = _execution_route(
            composition,
            target,
            capability=capability,
            engine=engine,
            balance_label=balance_label,
            balance_url=balance_url,
            settings_url=settings_url,
        )

    if policy_forbidden:
        return _ExecutionPresentation(
            target,
            resolved_target,
            destination,
            route,
            status,
            blocker,
            setup,
            active_operation,
        )

    if refusal is None:
        if payer is not None:
            status = "ready"
            blocker = None
            setup = None
            active_operation = None
    else:
        refusal_blocker = {
            "code": refusal.family,
            "message": refusal.remedy,
            "field": None,
        }
        selected_cloud_blocker = payer is not None or (
            refusal.target_id is not None
            and cloud_fallback_is_blocked(
                composition,
                target_id=refusal.target_id,
                capability=capability,
                engine=engine,
            )
        )
        if selected_cloud_blocker:
            blocker = refusal_blocker
            setup = (
                cloud_setup_for_lane(payer.lane if payer is not None else None)
                if cloud_setup_for_lane is not None
                else None
            )
            active_operation = None
            status = "needs_setup" if setup is not None else "unavailable"
        elif not (preserve_unbound_catalog and refusal.target_id is None):
            if blocker is None or status == "ready":
                blocker = refusal_blocker
            if status not in {"working", "needs_setup"}:
                status = "unavailable"

    return _ExecutionPresentation(
        target,
        resolved_target,
        destination,
        route,
        status,
        blocker,
        setup,
        active_operation,
    )


def _response(
    project_id: str,
    *,
    subject: dict[str, Any],
    depends_on: list[str],
    choices: list[dict[str, Any]],
    current_selection: dict[str, Any] | None,
    default_selection: dict[str, Any] | None,
    server_labels: Mapping[str, str] | None = None,
    cloud_setup: dict[str, Any] | None = None,
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
        "schema_version": "frisket.selector_choices.v2",
        "project_id": project_id,
        "subject": subject,
        "depends_on": depends_on,
        "current_choice_id": current_choice_id,
        "default_choice_id": default_choice_id,
        "groups": _groups(choices, server_labels),
        "orphaned_current": orphan,
        "cloud_setup": cloud_setup,
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


def _group_identity(
    choice: Mapping[str, Any], server_labels: Mapping[str, str] | None = None
) -> tuple[str, str, str]:
    target = _mapping(choice.get("resolved_target"))
    destination = _mapping(choice.get("processing_destination"))
    selection = _mapping(choice.get("authored_selection"))
    target_id = target.get("target_id")
    if destination.get("kind") == "local":
        return "local", "local", "On this device"
    if destination.get("kind") in {"operator_network", "unknown"} and target_id:
        label = (
            "Frisket models server"
            if target_id == MODELS_GATEWAY_TARGET_ID
            else (server_labels or {}).get(
                str(target_id), f"Model server · {target_id}"
            )
        )
        return f"server:{target_id}", "server", label
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


def _groups(
    choices: list[dict[str, Any]], server_labels: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for choice in choices:
        group_id, kind, label = _group_identity(choice, server_labels)
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


def _server_labels(provider_catalog: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(_provider_identity(provider)): str(
            provider.get("label") or _provider_identity(provider)
        )
        for provider in provider_catalog.get("providers", [])
        if isinstance(provider, Mapping)
        and provider.get("kind") == "local_http"
        and _provider_identity(provider)
    }


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
    license_hint = _mapping(engine.get("license"))
    license_parts = [
        value
        for key in ("name", "note")
        if isinstance(value := license_hint.get(key), str) and value
    ]
    if license_parts:
        facts.append(
            {
                "kind": "text",
                "label": "Restrictive license"
                if license_hint.get("restricted")
                else "License",
                "value": ": ".join(license_parts),
            }
        )
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
    if target is None or target.egress_class == "none":
        downloads = engine.get("downloadable_models") or []
        single_download = engine.get("downloadable_model")
        if isinstance(single_download, Mapping):
            downloads = [single_download]
        sizes = [row.get("size") for row in downloads if isinstance(row, Mapping)]
        if sizes and all(
            isinstance(size, int) and not isinstance(size, bool) and size >= 0
            for size in sizes
        ):
            facts.append(
                {
                    "kind": "text",
                    "label": "Download size",
                    "value": f"{sum(sizes):,} bytes",
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


def _opus_pair_key(params: Mapping[str, Any]) -> str | None:
    try:
        options = TranslationOptions.model_validate(
            {
                key: params[key]
                for key in ("language", "target_language")
                if key in params
            }
        ).normalize("opus_mt")
        source, target = resolve_pair_codes(
            options["language"][0], options["target_language"]
        )
    except ValueError:
        return None
    return f"{source}-{target}"


def _opus_pair_supported(
    engine: Mapping[str, Any], params: Mapping[str, Any]
) -> bool | None:
    if not params.get("language") or not params.get("target_language"):
        return None
    pair = _opus_pair_key(params)
    return any(
        pair is not None and isinstance(row, Mapping) and row.get("pair") == pair
        for row in engine.get("downloadable_pairs", []) or []
    )
