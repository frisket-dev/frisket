"""THE canonical action identity for consent binding.

Why this module exists
----------------------
The seam holds three copies of "the spec" and they are NOT byte-equal:

- ``runs.params`` — the runner spec as persisted, MUTATED by finalization
  (``halted_code`` / ``halted_reason`` land in it) and by the direct action
  boundary (``confirmed``, explicit ``row_ids``);
- the queue payload's ``spec`` — pristine, what the worker re-verifies;
- the runner spec a ``run.backfill`` threads — the stored spec with the
  unrun ``row_ids`` injected.

The former identity was an OPEN-WORLD hash (the whole dict minus a two-key
denylist), so it hashed whatever copy the caller held. Two probed
criticals followed: reconsent hashed the marked-up ``runs.params`` while the
resuming worker hashed the pristine payload spec (the accepted consent was
unfindable — durable ``consent_missing``), and a routed backfill's injected
``row_ids`` shifted identity so exact-match could never succeed.

The construction (closed-world by projection)
---------------------------------------------
Identity is projected onto the keys the action's own DECLARATION says its
runner spec can carry:

1. The declaration (``frisket.sdk.declaration.Op``, looked up by the spec's
   ``action_kind``) yields the allowlist — the structural keys the generic
   ``runner_spec_fn`` always emits plus its declared ``passthrough`` params
   plus this module's enumeration of the op's ``runner_spec_extra`` keys.
   Anything else in the dict — halt markers, progress metadata, the direct
   boundary's flow flags, any FUTURE writer — is structurally incapable of
   shifting identity. That is the whole point.
2. Excluded from identity, with reasons:
   - ``confirmed`` and ``consented_promise_set_hash``: retry-flow flags. A
     confirm retry must hash to the identity of the request the user first
     saw. (Neither is declared passthrough on transcribe's structural keys
     — ``consented_promise_set_hash`` IS passthrough — so this exclusion is
     load-bearing for the latter and documentation for the former.)
   - ``row_ids``: execution SCOPE, not intent. Consent covers the action's
     NATURE; scope-driven cost is re-gated at every admission by the
     backfill claims gate, and a backfill's injected scope must not make an
     already-consented action unrecognizable.
3. ONE canonical kind. A runner spec carries only ``action_kind`` (the public
   kind). Private implementation ids never enter the runner-spec or consent
   identity vocabulary.
4. Absent stays ABSENT: defaults are deliberately NOT materialized. The
   ``supplied`` include policy exists precisely so a materialized default
   never masquerades as an authored option (see ``Op.passthrough``'s own
   comment and ``support_inability``, which treats supplied-vs-absent as
   semantic). The canonical kind of (3) is the one deliberate exception: it
   is DERIVED, not defaulted, and is always present.
   Copy-invariance comes from the allowlist TOGETHER WITH
   :data:`IDENTITY_EXCLUDED_KEYS`, not from the allowlist alone: the three
   spec copies mostly differ by keys outside the allowlist, but ``row_ids``
   is a declared passthrough INSIDE it (``run.backfill`` overwrites it with
   the unrun set), so only its exclusion makes a backfill's spec hash equal
   to the launch's. Normalization is otherwise limited to the known-numeric
   fields (canonical decimal strings, so a JSON client's ``1.0`` and
   Python's ``1`` share one identity, and no float ever reaches the hash
   family).

A kind with no registered declaration FAILS LOUD
(:class:`UnknownActionIdentityKind`) — there is no silent fallback to
open-world hashing, because that fallback is the bug this module replaces.

Hash family: ``frisket.action_identity.v1`` (rules unchanged — NFC strings,
floats rejected, bare-hex sha256; only the MATERIAL changed, and the family
note in ``promises.HASH_FAMILIES`` records the construction change).
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from pydantic import ValidationError

from frisket.execution.promises import (
    DOMAIN_ACTION_IDENTITY,
    content_hash,
    decimal_string,
)

# The keys ``sdk/maprunner.py::build_runner_spec_fn`` emits for EVERY op
# regardless of declaration: the canonical public kind, the sheet, and the input column list
# (emitted under this literal key by the source descriptor).
STRUCTURAL_RUNNER_SPEC_KEYS: tuple[str, ...] = (
    "action_kind",
    "sheet_id",
    "input_columns",
)

# ``Op.runner_spec_extra`` / ``runner_spec_resolve_extra`` are opaque
# callables, so the declaration cannot state their emitted keys. This table
# is where an op declaring such a hook states them by hand — the seat one
# would occupy: an op that grows a hook without an entry here fails loud
# (:class:`UnknownActionIdentityKind`, see :func:`_allowlist_for_declaration`)
# rather than silently dropping — or admitting — whatever the hook emits.
RUNNER_SPEC_EXTRA_KEYS: Mapping[str, tuple[str, ...]] = {}

# Keys that land on a dispatched runner spec AFTER ``runner_spec_fn`` built
# it, from writers outside the declaration. Every one is correctly OUTSIDE
# identity, and each is here so the closure test can state that on purpose
# rather than by omission:
#
# - ``group_label`` / ``overwrite``: output INTENT applied by the action
#   boundary (``action_inventory._apply_action_envelope_to_runner_spec``) —
#   where results land, not what work is authorized.
# - ``_frisket_queued_action_run``: the queued-action envelope marker
#   (receipt/action/params-hash bookkeeping for the worker's pre-run guards).
# - ``row_ids``: execution scope, set by run.backfill.
# - ``halted_code`` / ``halted_reason``: runner-owned TERMINAL markers that
#   finalization writes INTO ``runs.params`` — the copy reconsent reads.
#   These are the two keys whose presence in an open-world hash made an
#   accepted consent unfindable.
# - ``confirmed`` / ``consented_promise_set_hash``: retry/consent FLOW flags.
# - ``edition_run_context``: hosted funding/edition context attached at
#   enqueue.
NON_IDENTITY_RUNNER_SPEC_KEYS: frozenset[str] = frozenset(
    {
        "group_label",
        "overwrite",
        "_frisket_queued_action_run",
        "halted_code",
        "halted_reason",
        "confirmed",
        "edition_run_context",
    }
)

# Never identity material (see the module docstring for each reason).
IDENTITY_EXCLUDED_KEYS = frozenset(
    {"confirmed", "consented_promise_set_hash", "row_ids"}
)

# The identity key that carries the action's kind. Unlike every other
# projected key this one is DERIVED (from the resolved declaration) rather
# than copied, so it is always present on an admitted runner spec.
IDENTITY_KIND_KEY = "action_kind"

# Known-numeric runner-spec fields: converted to canonical decimal strings
# before hashing (the hash family REJECTS floats — no ``default=str`` type
# erasure — and an int/float spelling difference must not split a consent).
IDENTITY_NUMERIC_KEYS = frozenset(
    {"sheet_id", "num_speakers", "min_speakers", "max_speakers"}
)


class UnknownActionIdentityKind(ValueError):
    """A spec whose ``action_kind`` has no registered op declaration.

    Consent identity is a projection through the declaration; without one
    there is no allowlist, and hashing the raw dict would restore exactly
    the removed open-world behavior. Fail loud.
    """


def _declaration_for_kind(kind: str) -> Any:
    raise UnknownActionIdentityKind(
        f"action kind {kind!r} has no registered op declaration, so its "
        "consent identity cannot be projected through a declared runner-"
        "spec shape; identity is closed-world by construction and never "
        "falls back to hashing the raw spec"
    )


def _declaration_for_spec(spec: Mapping[str, Any]) -> Any:
    """Resolve the declaration from the sole canonical runner identity."""
    if "recipe" in spec:
        raise UnknownActionIdentityKind(
            "runner spec must not carry top-level 'recipe' identity"
        )
    kind = spec.get("action_kind")
    if not isinstance(kind, str) or not kind or kind != kind.strip() or "." not in kind:
        raise UnknownActionIdentityKind(
            "runner spec requires one canonical dotted 'action_kind'"
        )
    return _declaration_for_kind(kind)


def _allowlist_for_declaration(declaration: Any) -> frozenset[str]:
    kind = declaration.kind
    if kind not in RUNNER_SPEC_EXTRA_KEYS and (
        declaration.runner_spec_extra is not None
        or declaration.runner_spec_resolve_extra is not None
    ):
        raise UnknownActionIdentityKind(
            f"action kind {kind!r} declares a runner_spec_extra hook whose "
            "emitted keys are not enumerated in RUNNER_SPEC_EXTRA_KEYS; "
            "identity cannot be projected through an unknown shape"
        )
    fields = set(STRUCTURAL_RUNNER_SPEC_KEYS)
    fields.update(param for param, _policy in declaration.passthrough)
    fields.update(RUNNER_SPEC_EXTRA_KEYS.get(kind, ()))
    return frozenset(fields)


def runner_spec_allowlist(kind: str) -> frozenset[str]:
    """Every key the generic ``runner_spec_fn`` can legitimately emit for
    ``kind`` — the closed world identity projects onto (before exclusions).

    Pinned against the generator by a closure test: the emitted keys of a
    fully-populated params model must be a SUBSET of this set, so a future
    declaration change cannot silently re-open the world.
    """
    return _allowlist_for_declaration(_declaration_for_kind(kind))


def identity_fields(kind: str) -> frozenset[str]:
    """The allowlist minus :data:`IDENTITY_EXCLUDED_KEYS` — the exact key
    set consent identity is computed over."""
    return runner_spec_allowlist(kind) - IDENTITY_EXCLUDED_KEYS


def _identity_numeric(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return decimal_string(value)


def _pointer_child(pointer: str, segment: str) -> str:
    """Append one JSON-pointer segment without exposing a value."""
    escaped = segment.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _refuse_surviving_float(value: Any, pointer: str = "") -> None:
    """Refuse the first float that would otherwise reach the v1 hasher.

    The projected payload is JSON-shaped, so mapping keys have a stable
    sorted traversal and arrays retain their authored index order.  The
    refusal deliberately names the location and type but never the value:
    this seam can carry user-authored runner parameters.
    """
    if isinstance(value, float):
        raise ValueError(
            "action identity cannot hash float at "
            f"{pointer or '/'} (type=float; value=<redacted>)"
        )
    if isinstance(value, Mapping):
        for key in sorted(value):
            _refuse_surviving_float(value[key], _pointer_child(pointer, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_surviving_float(item, f"{pointer}/{index}")


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
    split a consent) of the declaration-projected spec, with the action's
    kind normalized to ONE canonical spelling (module docstring point 3).
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
    declaration = _declaration_for_spec(spec)
    fields = _allowlist_for_declaration(declaration) - IDENTITY_EXCLUDED_KEYS
    payload: dict[str, Any] = {}
    for key in fields:
        if key == IDENTITY_KIND_KEY:
            continue  # derived below, never copied
        if key not in spec:
            continue  # absent stays absent — defaults are never materialized
        value = spec[key]
        if key in IDENTITY_NUMERIC_KEYS:
            value = _identity_numeric(value)
        payload[key] = value
    # The canonical kind comes from the declaration selected by the spec's
    # required action_kind. Recipe-shaped specs are refused before projection.
    payload[IDENTITY_KIND_KEY] = declaration.kind
    _refuse_surviving_float(payload)
    return content_hash(DOMAIN_ACTION_IDENTITY, payload)
