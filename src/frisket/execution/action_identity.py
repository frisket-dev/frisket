"""THE canonical action identity for consent binding.

The seam holds three copies of "the spec" and they are NOT byte-equal:

- ``runs.params`` — the runner spec as persisted, MUTATED by finalization
  (``halted_code`` / ``halted_reason`` land in it) and by the direct action
  boundary (``confirmed``, explicit ``row_ids``);
- the queue payload's ``spec`` — pristine, what the worker re-verifies;
- the runner spec a ``run.backfill`` threads — the stored spec with the
  unrun ``row_ids`` injected.

Identity is therefore closed-world by projection, never a hash of whatever
dict the caller holds. A typed action's identity is its kind, its sheet and
its validated Params model (plus the resolved engine options), so halt
markers, progress metadata, retry flags (``confirmed``,
``consented_promise_set_hash``), execution scope (``row_ids``) and any future
runner-envelope writer are structurally incapable of shifting it. Consent
covers the action's NATURE; scope-driven cost is re-gated at every admission
by the backfill claims gate.

Absent stays ABSENT: defaults are deliberately NOT materialized. Numeric
fields are canonical decimal strings, so a JSON client's ``1.0`` and
Python's ``1`` share one identity, and no float reaches the hash family.

A spec that is not a typed action FAILS LOUD
(:class:`UnknownActionIdentityKind`); there is no fallback to hashing the raw
spec.

Hash family: ``frisket.action_identity.v1`` (NFC strings, floats rejected,
bare-hex sha256).
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, NoReturn

from pydantic import ValidationError

from frisket.execution.promises import (
    DOMAIN_ACTION_IDENTITY,
    content_hash,
    decimal_string,
)


class UnknownActionIdentityKind(ValueError):
    """A spec that is not a registered typed action.

    Consent identity is a projection through the typed Params schema; hashing
    the raw dict would make identity depend on whichever spec copy the caller
    holds. Fail loud.
    """


def _refuse_untyped_spec(spec: Mapping[str, Any]) -> NoReturn:
    kind = spec.get("action_kind")
    if not isinstance(kind, str) or not kind or kind != kind.strip() or "." not in kind:
        raise UnknownActionIdentityKind(
            "runner spec requires one canonical dotted 'action_kind'"
        )
    raise UnknownActionIdentityKind(
        f"action kind {kind!r} has no registered op declaration, so its "
        "consent identity cannot be projected through a declared runner-"
        "spec shape; identity is closed-world by construction and never "
        "falls back to hashing the raw spec"
    )


def _identity_numeric(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return decimal_string(value)


def _sanitized_validation_error(error: ValidationError) -> ValueError:
    # Locations are not schema-only: dict keys and forbidden extra fields are
    # authored values too. Keep only deterministic validation type codes.
    details = sorted(
        {
            item["type"]
            for item in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        }
    )
    return ValueError(
        "action identity params validation failed: "
        + json.dumps(details, sort_keys=True, separators=(",", ":"))
    )


def action_identity_hash(spec: Mapping[str, Any]) -> str:
    """THE canonical action identity for consent binding.

    ONE construction, called by all three sides so they cannot disagree:
    the mint (``resolve_for_action.record_resolved_execution``), the
    backfill-confirm writer (which hashes the marked-up
    ``runs.params``), and worker verification
    (``engine/jobs/runs.py::_verify_execution_route``, which hashes the
    pristine queue payload spec).

    Bare-hex sha256 over canonical JSON (sorted keys, compact separators,
    strings NFC-normalized recursively so NFD/NFC-equivalent params never
    split a consent) of the typed Params projection.
    """
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.core import (
        MapRows,
        MapBatch,
        ModelRows,
        resolved_engine_options,
    )

    kind = spec.get("action_kind")
    if "recipe" in spec:
        raise UnknownActionIdentityKind(
            "runner spec must not carry top-level 'recipe' identity"
        )
    try:
        typed = ACTION_REGISTRY.get(kind) if isinstance(kind, str) else None
    except KeyError:
        typed = None
    if typed is not None and isinstance(
        typed.definition.run, (MapRows, MapBatch, ModelRows)
    ):
        try:
            params = typed.definition.run.params_model.model_validate(
                spec.get("params")
            )
        except ValidationError as error:
            raise _sanitized_validation_error(error) from None
        # Project through the declared Params schema, never the mutable runner
        # envelope. Canonical JSON text retains typed decimal values without
        # introducing floats into the consent hash family's material.
        payload = {
            "action_kind": typed.action_id,
            "action_version": "1",
            "sheet_id": _identity_numeric(spec.get("sheet_id")),
            "params_json": json.dumps(
                params.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        }
        # Full Params dumps erase omission-sensitive engine defaults. Bind the
        # same semantic option projection used by placement and request hashing.
        options = resolved_engine_options(typed.definition.run, params)
        if options is not None:
            payload["engine_options"] = options
        return content_hash(DOMAIN_ACTION_IDENTITY, payload)
    _refuse_untyped_spec(spec)
