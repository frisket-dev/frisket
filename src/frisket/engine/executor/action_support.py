"""Pure value, confirmation, cost, and serialization helpers for actions."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionResult,
    ActionSpec,
)
from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.models.metadata import (
    model_calls_cost_actual as _model_calls_cost_actual,
)
from frisket.engine.runner import (
    CostGate,
)
from frisket.engine.runner.confirmation_context import (
    ActionScope,
    ParamsScope,
)


def _failed_result(
    *,
    project_id: str,
    action_kind: str,
    error: ActionError,
) -> ActionResult:
    # One seam for the unified confirmation envelope: a resolve_fn/precheck error carrying the generic
    # needs_confirmation marker (e.g. derive.join's fan-out guard) surfaces as the
    # SAME status="needs_confirmation" -> HTTP 402 the model-cost gate emits, rather
    # than a plain status="failed"/400. Unmarked errors keep failing normally.
    status = "needs_confirmation" if error.needs_confirmation else "failed"
    return ActionResult(
        action=ActionIdentity(kind=action_kind, action_id=_new_id("act")),
        status=status,
        project_id=project_id,
        errors=[error],
    )


def _params_hash(action: ActionSpec) -> str:
    payload = action.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _params_hash_without_confirmed(action: ActionSpec) -> str:
    # ``consented_promise_set_hash`` is the claims-gate echo — retry-flow
    # plumbing exactly like ``confirmed``, so the confirmed retry hashes to
    # the SAME action identity the user first submitted (additive: actions
    # without the field hash identically to before).
    payload = action.model_dump(mode="json", exclude_none=True)
    params = dict(payload.get("params") or {})
    for field in {"confirmed", "consented_promise_set_hash"}:
        params.pop(field, None)
    payload["params"] = params
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def action_confirmation_scope(action: ActionSpec) -> ActionScope:
    """The confirmation identity of a family that holds the whole ``ActionSpec``.

    These families do not have a routed promise set, but their confirmation
    has the same invariant: changing target, scope, or quote after the modal
    was shown must produce a fresh 402 rather than spending under the old
    approval. The payload and the hash belong to
    ``engine/runner/confirmation_context.py``; this is only the identity half.
    """

    return ActionScope(action_hash=_params_hash_without_confirmed(action))


def params_confirmation_scope(params: BaseModel) -> ParamsScope:
    """The confirmation identity of a deterministic family with no ActionSpec
    seam: every authored param, minus the retry-flow fields so the confirmed
    retry recomputes the token it was shown."""

    authored_params = params.model_dump(mode="json", exclude_none=True)
    authored_params.pop("confirmed", None)
    authored_params.pop("consented_promise_set_hash", None)
    return ParamsScope(params=authored_params)


def _estimate_confirmation_reason(estimate: float | None) -> str:
    """Generic confirmation reason for a model-cost gate.

    An unpriced model has no estimate, so the gate cannot certify "cheap" and
    demands the same explicit confirmation as an expensive one; that is reported
    as the generic ``unknown_estimate`` rather than a cost number.
    """

    return "unknown_estimate" if estimate is None else "model_cost"


def _claims_gate_details(exc: CostGate) -> dict[str, Any]:
    """Additive claims-gate payload: when the
    gate is a ClaimsGate carrying claims, the 402 details keep their existing
    keys and ADD ``claims: [{field, display}]`` + ``promise_set_hash`` (the
    hash the confirm retry echoes). Empty for plain CostGates, so pre-claims
    behavior is byte-identical."""
    details: dict[str, Any] = {}
    claims = getattr(exc, "claims", None)
    if claims:
        details["claims"] = [dict(claim) for claim in claims]
    promise_set_hash = getattr(exc, "promise_set_hash", None)
    if promise_set_hash:
        details["promise_set_hash"] = promise_set_hash
    return details


def _model_cost_requires_confirmation_error(
    action: ActionSpec,
    exc: CostGate,
) -> ActionError:
    return ActionError(
        code="model_cost_requires_confirmation",
        message=str(exc),
        action_kind=action.kind,
        field="params.confirmed",
        needs_confirmation=True,
        details={
            "reason": _estimate_confirmation_reason(exc.estimate),
            "estimate": exc.estimate_details or exc.estimate,
            **_claims_gate_details(exc),
        },
    )


def _external_cost_requires_confirmation_error(
    action: Any,
    exc: CostGate,
) -> ActionError:
    return ActionError(
        code="external_cost_requires_confirmation",
        message=str(exc),
        action_kind=action.kind,
        field="params.confirmed",
        needs_confirmation=True,
        details={
            "reason": "external_metered",
            "estimate": exc.estimate_details or exc.estimate,
            **_claims_gate_details(exc),
        },
    )


def _model_family(model: str) -> str:
    return provider_from_model_id(model).strip().lower()


def _model_call_units(call: Any) -> dict[str, Any]:
    raw_units = _row_value(call, "units")
    if not raw_units:
        return {}
    if isinstance(raw_units, dict):
        return raw_units
    try:
        units = json.loads(raw_units)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(units, dict):
        return {}
    return units


def _model_call_provider_use(
    model_calls: list[Any],
    *,
    model: str,
    run: Any,
) -> list[dict[str, Any]]:
    if not model_calls:
        provider = provider_from_model_id(model)
        return [
            {
                "provider": provider,
                "model": model,
                "model_call_count": 0,
                "cost_actual": _model_calls_cost_actual(model_calls),
            }
        ]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for call in model_calls:
        key = (str(call["provider"]), str(call["engine"]))
        item = grouped.setdefault(
            key,
            {
                "provider": str(call["provider"]),
                "model": str(call["engine"]),
                "model_call_count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "_calls": [],
            },
        )
        item["model_call_count"] += 1
        item["_calls"].append(call)
        units = _model_call_units(call)
        item["tokens_in"] += int(units.get("tokens_in") or 0)
        item["tokens_out"] += int(units.get("tokens_out") or 0)
    provider_use: list[dict[str, Any]] = []
    for item in grouped.values():
        item["cost_actual"] = _model_calls_cost_actual(item.pop("_calls"))
        provider_use.append(item)
    return provider_use


def _routed_call_provider_use(
    model_calls: list[Any], *, capability: str
) -> list[dict[str, Any]]:
    """Project only durable host facts, including rowless batch HTTP calls."""
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for call in model_calls:
        if call["capability"] != capability:
            continue
        units = _model_call_units(call)
        details = {
            key: units[key]
            for key in ("dataset", "vintage", "geography")
            if key in units
        }
        provider_kind = _row_value(call, "provider_kind")
        credential_source = _row_value(call, "credential_source")
        key = (
            str(call["provider"]),
            str(call["engine"]),
            provider_kind,
            credential_source,
            tuple(details.items()),
        )
        item = grouped.setdefault(
            key,
            {
                "provider": str(call["provider"]),
                "engine": str(call["engine"]),
                "service": capability,
                "external_api": provider_kind not in {"local_process", "local_http"},
                **(
                    {"credential_source": credential_source}
                    if credential_source is not None
                    else {}
                ),
                "request_count": 0,
                "model_call_count": 0,
                **details,
                "_calls": [],
            },
        )
        item["request_count"] += int(units.get("requests", 0))
        item["model_call_count"] += 1
        item["_calls"].append(call)
    provider_use = []
    for item in grouped.values():
        item["cost_actual"] = _model_calls_cost_actual(item.pop("_calls"))
        provider_use.append(item)
    return provider_use


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _parse_json_path_segment(part: str) -> tuple[str, list[int]] | None:
    """Split a segment into (key, [indices]). Returns None for malformed
    bracket syntax — unclosed, non-integer, or trailing garbage — so a typo
    fails closed instead of silently resolving to a shorter path."""
    bracket = part.find("[")
    if bracket == -1:
        return part, []
    key = part[:bracket]
    indices: list[int] = []
    rest = part[bracket:]
    while rest:
        if not rest.startswith("["):
            return None
        close = rest.find("]")
        if close == -1:
            return None
        try:
            indices.append(int(rest[1:close]))
        except ValueError:
            return None
        rest = rest[close + 1 :]
    return key, indices


def _extract_json_path(value: Any, path: str) -> tuple[Any, bool]:
    if path == "$":
        return value, True
    current = value
    for part in path.removeprefix("$.").split("."):
        # An exact dict key wins first: keeps plain dotted paths unchanged and
        # keeps a literal bracket-bearing key (e.g. "a[0]") addressable, only
        # falling back to index parsing when no such key exists.
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        parsed = _parse_json_path_segment(part)
        if parsed is None:
            return None, False
        key, indices = parsed
        if not indices or not (isinstance(current, dict) and key in current):
            return None, False
        current = current[key]
        for index in indices:
            if not isinstance(current, list) or not (
                -len(current) <= index < len(current)
            ):
                return None, False
            current = current[index]
    return current, True


def _json_schema_error(
    value: Any, schema: dict[str, Any], path: str = "$"
) -> str | None:
    if not _is_json_value(value):
        return f"{path} is not JSON-serializable"
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return f"{path} expected object"
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                return f"{path}.{name} is required"
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            for name, child_schema in properties.items():
                if name in value and isinstance(child_schema, dict):
                    err = _json_schema_error(
                        value[name], child_schema, f"{path}.{name}"
                    )
                    if err is not None:
                        return err
        return None
    if expected == "array":
        if not isinstance(value, list):
            return f"{path} expected array"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                err = _json_schema_error(item, item_schema, f"{path}[{idx}]")
                if err is not None:
                    return err
        return None
    if expected == "string":
        return None if isinstance(value, str) else f"{path} expected string"
    if expected == "integer":
        if isinstance(value, int) and not isinstance(value, bool):
            return None
        return f"{path} expected integer"
    if expected == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return None
        return f"{path} expected number"
    if expected == "boolean":
        return None if isinstance(value, bool) else f"{path} expected boolean"
    if expected == "null":
        return None if value is None else f"{path} expected null"
    return None


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True
