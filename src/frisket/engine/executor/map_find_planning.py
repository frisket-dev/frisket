"""Planning, cost estimation, and admission for ``map.find``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.ai.llm import estimate_tokens, model_pricing
from frisket.ai.map.find_source import FindWindow, plan_find_windows
from frisket.ai.vision.region_locator import (
    LocateRegionsRequest,
    profile_for_engine,
    region_request_identity,
)
from frisket.contracts.action import ActionError
from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_source import (
    ResolvedFindSource,
    _find_target_snapshot,
    _hash_json,
    resolve_find_sources,
)


_MAX_CORE_UNITS = 24
_OVERLAP_UNITS = 2
_MAX_OUTPUT_TOKENS = 32_768
_ESTIMATED_OUTPUT_TOKENS_PER_SOURCE_ROW = 4_096
# The exact image-token formula differs by provider. The bounded 1024-edge
# inference copy is typically below this; use one conservative product constant
# so consent does not pretend the image itself is free input.
_ESTIMATED_IMAGE_INPUT_TOKENS = 2_048
_REPAIR_ATTEMPTS = 1


@dataclass(frozen=True)
class FindWindowPlan:
    source: ResolvedFindSource
    window: FindWindow
    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ImageFindPlan:
    source: ResolvedFindSource
    request: LocateRegionsRequest


FindPlan = FindWindowPlan | ImageFindPlan


@dataclass(frozen=True)
class PreparedFindScan:
    sources: tuple[ResolvedFindSource, ...]
    target: dict[str, Any]
    plans: tuple[FindPlan, ...]
    estimate: dict[str, Any]


def _details_schema(params: FindOperation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in params.fields:
        properties[field.name] = {
            "anyOf": [
                field.value_schema(),
                {"type": "null"},
            ]
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": [],
    }


def find_response_schema(params: FindOperation) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["matches"],
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["match", "source_unit_ids", "details"],
                    "properties": {
                        "match": {"type": "string", "minLength": 1},
                        "source_unit_ids": {
                            "type": "array",
                            "items": {"type": ["string", "integer"]},
                        },
                        "details": _details_schema(params),
                    },
                },
            }
        },
    }


def _messages(params: FindOperation, window: FindWindow) -> tuple[dict[str, Any], ...]:
    source = "\n".join(
        f"[{json.dumps(unit.unit_id, ensure_ascii=False)}] {unit.text}"
        for unit in window.units
    )
    return (
        {
            "role": "system",
            "content": (
                "Find every distinct occurrence satisfying the instruction in this "
                "source window. Return one item per occurrence, not an answer or "
                "summary. Match must be a verbatim excerpt copied wholly from one "
                "cited source unit and include enough context to identify one unique "
                "location among the cited units. Every item must cite all and only "
                "the numbered source unit ids needed for that occurrence. Return an "
                "empty matches list when there are none."
            ),
        },
        {
            "role": "user",
            "content": f"Instruction: {params.instruction}\n\nSource units:\n{source}",
        },
    )


def plan_find_scan(
    params: FindOperation,
    sources: tuple[ResolvedFindSource, ...],
) -> tuple[FindPlan, ...]:
    plans: list[FindPlan] = []
    for source in sources:
        if source.source_kind == "image":
            if source.image is None:
                raise ValueError("resolved image source lacks an image asset")
            plans.append(
                ImageFindPlan(
                    source=source,
                    request=LocateRegionsRequest(
                        engine_ref=params.model,
                        image=source.image,
                        query=params.instruction,
                        detail_schema=_details_schema(params),
                    ),
                )
            )
            continue
        plans.extend(
            FindWindowPlan(
                source=source,
                window=window,
                messages=_messages(params, window),
            )
            for window in plan_find_windows(
                source.units,
                max_core_units=_MAX_CORE_UNITS,
                overlap_units=_OVERLAP_UNITS,
            )
        )
    return tuple(plans)


def _plan_hash_payload(plans: tuple[FindPlan, ...]) -> list[dict[str, Any]]:
    """Stable admission identity that never includes image base64."""

    payload: list[dict[str, Any]] = []
    for plan in plans:
        if isinstance(plan, FindWindowPlan):
            payload.append({"kind": "text", "messages": plan.messages})
        else:
            payload.append(
                {
                    "kind": "image",
                    "request": region_request_identity(plan.request),
                }
            )
    return payload


def estimate_find_scan_cost(
    params: FindOperation,
    plans: tuple[FindPlan, ...],
) -> dict[str, Any]:
    token_counts = [
        estimate_tokens(
            json.dumps(
                plan.messages
                if isinstance(plan, FindWindowPlan)
                else region_request_identity(plan.request)
            )
        )
        + (_ESTIMATED_IMAGE_INPUT_TOKENS if isinstance(plan, ImageFindPlan) else 0)
        for plan in plans
    ]
    source_rows = {
        (plan.source.sheet_id, plan.source.row_id, plan.source.source_snapshot)
        for plan in plans
    }
    estimated_output_tokens = len(source_rows) * _ESTIMATED_OUTPUT_TOKENS_PER_SOURCE_ROW
    pricing = model_pricing(params.model)
    details: dict[str, Any] = {
        "rows": len(source_rows),
        "windows": len(plans),
        "source_rows": len(source_rows),
        "input_tokens": sum(token_counts),
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_output_tokens_per_source_row": (
            _ESTIMATED_OUTPUT_TOKENS_PER_SOURCE_ROW
        ),
        "estimated_image_input_tokens_per_plan": _ESTIMATED_IMAGE_INPUT_TOKENS,
        "execution_max_output_tokens_per_window": _MAX_OUTPUT_TOKENS,
        "repair_attempts": _REPAIR_ATTEMPTS,
        "resolved_prompt_hash": _hash_json(_plan_hash_payload(plans)),
    }
    if pricing.price is None:
        return {**details, "cost": None, "cost_source": "unknown"}
    pin, pout = pricing.price
    cost = sum(token_counts) * pin + estimated_output_tokens * pout
    cost /= 1e6
    return {
        **details,
        "cost": round(cost, 4),
        "cost_source": pricing.cost_source,
        **({"pricing_key": pricing.pricing_key} if pricing.pricing_key else {}),
    }


def _prepare_find_scan(
    project: Any,
    params: FindOperation,
) -> PreparedFindScan | ActionError:
    from frisket.engine.runner.confirmation_context import rate_estimate
    from frisket.execution.pricing_policy import default_pricing_policy

    sources = resolve_find_sources(project, params)
    if isinstance(sources, ActionError):
        return sources
    if (
        any(source.source_kind == "image" for source in sources)
        and profile_for_engine(params.model) is None
    ):
        return ActionError(
            code="unsupported_model",
            message=(
                "The selected model has no qualified image-region grounding "
                "profile for map.find"
            ),
            action_kind=params.action_kind,
            field="params.model",
            details={"model": params.model},
        )
    target = _find_target_snapshot(project, params)
    if isinstance(target, ActionError):
        return target
    plans = plan_find_scan(params, sources)
    estimate = rate_estimate(
        estimate_find_scan_cost(params, plans), policy=default_pricing_policy()
    )
    return PreparedFindScan(
        sources=sources,
        target=target,
        plans=plans,
        estimate=estimate,
    )
