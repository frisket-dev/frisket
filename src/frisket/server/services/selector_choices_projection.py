"""Pure selector grouping, authored values and descriptive facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.actions.translate_types import TranslationOptions
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
    if not params.get("language"):
        return None
    pair = _opus_pair_key(params)
    return any(
        pair is not None and isinstance(row, Mapping) and row.get("pair") == pair
        for row in engine.get("downloadable_pairs", []) or []
    )
