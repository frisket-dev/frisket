"""Run-time refusal envelopes plus the one-ledger fact derivation.

What used to live here as a SECOND head read is gone. ``load_route_binding``
built the routed run's ``OpContext.extras`` from its own ``RouteStore.head()``
call, while the worker fence read the head again — the two-reads-one-truth
shape ``bound_route_id`` was threaded through three signatures to reconcile.
The attempt authority (``frisket.execution.attempt_authority``) now reads the
head once, derefs once, and hands the whole thing down as one
``AttemptCommitment`` in ``extras[ATTEMPT_EXTRA]``. With it went the four
route-binding extras (``resolved_route``, ``route_binding``,
``execution_binding`` and the ``RouteBinding`` pair type), the adapter
fence ``require_route_binding`` (a sum type cannot represent a route
without its binding, and a required argument cannot be omitted), and
``UnverifiedRoutedRunRefusal`` (the type-level check replaces it).

What remains:

- the typed refusal envelopes every dispatch path shares
  (:class:`ExecutionRouteVerificationFailed`, :class:`RouteBindingUnavailable`)
  plus :func:`pre_dispatch_refusal_markers`, which keeps a run's terminal
  markers and its receipt code ONE value;
- :func:`ephemeral_target_binding` — the persistence-free binding an UNROUTED
  invocation (preview, direct call)
  obtains through the same static-provider + ``candidate_binding`` machinery
  the routed path uses;
- :func:`bind_fact_to_route` — the §7 one-ledger derivation.
  Given the in-scope route and one routed model-call fact dict (transcription
  or OCR — the derivation reads the ROUTE, so it is capability-neutral), the
  route-derived receipt fields (provider grouping, provider kind,
  cost_source) come from the route ROW facts — never from env or path-local
  literals — while ``credential_source`` on the settlement-visible fact is
  the observed actual, not the route's persisted value. Divergence appends a
  successor route and epoch carrying the actual facts; because
  ``credential_source`` is not a compiled promise, it creates no fabricated
  route violation. The §6 observation payload is attached under
  :data:`ROUTE_OBSERVATION_KEY`
  so the fact writer (``engine/store/runs.py::write_model_calls``) can run
  ``observe_binding_divergence`` inside its own store transaction and stamp
  the returned ``epoch_id`` on the row.

Truth invariant: the fact's settlement-visible ``credential_source`` is
the actual observed value, not the route's persisted value. When they diverge, the observation
point appends a successor route carrying the actual facts and the fact links
that successor's epoch; no uncompiled credential promise is fabricated.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frisket.execution.provider import ExecutionTargetProvider
from frisket.execution.resolver import (
    CandidateBinding,
    Refusal,
    candidate_binding,
)
from frisket.execution.targets import (
    DATALAB_TARGET_ID,
    DEEPL_TARGET_ID,
    GOOGLE_TRANSLATE_TARGET_ID,
    NOMINATIM_TARGET_ID,
    OPENCAGE_TARGET_ID,
    US_CENSUS_TARGET_ID,
    require_clean,
    validated_target_snapshot,
)


class ExecutionRouteVerificationFailed(RuntimeError):
    """Worker-side route verification failed: the run terminalizes as a
    DURABLE, honest failure — nothing executes, the receipt records the
    remedy, and output-column claims release with the terminal receipt.

    ``code`` is the receipt error code: ``consent_missing`` (§5.3 coverage
    not satisfiable from persisted artifacts — including a run with no
    persisted route artifacts at all), ``no_live_target`` (§0.4: the pinned
    target no longer dereferences live), or ``stale_head`` (the route head
    moved between the dispatch binding and verification).

    Defined HERE, with its sibling refusals, rather than in the queued
    handler that raises it: the direct action boundary
    (``engine/executor/action_lifecycle.py``) must catch it by type to give it
    a typed receipt, and importing the queue handler from the executor is a
    cycle. ``engine.jobs.runs`` re-exports the name it has always exposed.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RouteBindingUnavailable(RuntimeError):
    """A PERSISTED route's pinned target will not deref (dead/renamed/
    deactivated) — a hard dispatch refusal (F6, §0.3: an unavailable pin
    halts; it NEVER falls back). The queued-run handler maps this to the
    durable ``no_live_target`` failure."""

    code = "no_live_target"

    def __init__(self, remedy: str) -> None:
        super().__init__(remedy)
        self.remedy = remedy


#: Terminal marker code for a pre-dispatch refusal that carries no typed
#: code of its own (an unexpected failure while assembling run extras).
PRE_DISPATCH_REFUSAL_CODE = "pre_dispatch_failed"


def pre_dispatch_refusal_markers(exc: BaseException) -> tuple[str, str]:
    """``(halted_code, halted_reason)`` for a refusal raised BEFORE any row
    dispatched, for the run's terminal markers.

    The three typed pre-dispatch refusals carry their receipt code as a class
    attribute (``ExecutionRouteVerificationFailed`` per-instance, since its
    code varies), so the marker written on the run row and the code written
    on the receipt are ONE value — a run failed ``consent_missing`` says so
    in both places. Anything else reaching a pre-dispatch failure is not a
    typed refusal at all and is recorded under
    :data:`PRE_DISPATCH_REFUSAL_CODE` with its message, never laundered into
    a consent-shaped code the operator cannot use to repair the wiring.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = PRE_DISPATCH_REFUSAL_CODE
    reason = getattr(exc, "remedy", None) or str(exc)
    return code, str(reason)


# Transient key a route-bound fact dict carries from the recipe to the fact
# writer; ``write_model_calls`` pops it, runs the observation point, and
# stamps the returned ``epoch_id`` — it is never a stored column.
ROUTE_OBSERVATION_KEY = "_route_observation"


# Typed, versioned provenance shape: the identity for
# "distinct observed provenance" — bump the version when the shape changes.
# per capability, which is why it is a function: the shape is the same
# for every capability, but the epochs of two different kinds of work are not
# the same provenance and must not dedupe against each other. Transcription's
# string is unchanged, so no existing epoch identity moved.
def binding_provenance_version(capability: str) -> str:
    return f"frisket.{capability}_binding.v1"


# Transport families, keyed by the exact transport token retained in the
# route snapshot.
# Zero-tariff = operator-owned compute with no per-request meter (§7's
# free_local class); structural-credential = paths where no separate
# credential is observable at dispatch (in-process worker / the gateway's
# shared bearer), so the route's pinned source IS the dispatch fact (§0.3).
ZERO_TARIFF_TRANSPORTS = frozenset(
    {
        "local",
        "frisket.transcription.v1",
        "sidecar.ocr",
        # The gateway's /to-markdown wire: the operator's own service, exactly
        # like the /ocr and /v1/transcribe wires beside it. Zero-tariff is a fact
        # about who owns the compute, not about which capability is asking.
        "sidecar.convert",
    }
)
_TRANSPORT_PROVIDER_KIND = {
    "local": "local_process",
    "frisket.transcription.v1": "local_http",
    "sidecar.ocr": "local_http",
    "sidecar.convert": "local_http",
    "remote": "platform_api",
    "datalab.convert": "platform_api",
    # Third-party wires. Registered with the rest of the capability set and
    # inert until a recipe routes: every one of them is somebody else's HTTP
    # API reached over the operator's own key, which is what platform_api
    # already names. The alternative was a documented deferral, and a deferral
    # is a promise that a future lane remembers to enumerate these — the
    # failure mode this codebase pays for most often.
    "deepl.v2": "platform_api",
    "google.translate.v2": "platform_api",
    "opencage.v1": "platform_api",
    "nominatim.search": "platform_api",
    "census.acs5": "platform_api",
}

# Observed-only binding facts (§6: recorded on the epoch, never promised).
_OBSERVED_ONLY_KEYS = ("revision", "device", "dtype")


def ephemeral_target_binding(
    target_id: str,
    engine: str,
    *,
    provider: ExecutionTargetProvider | None = None,
) -> CandidateBinding:
    """Persistence-free binding for an unrouted invocation.

    Previews, direct (non-queued) calls, and the non-transcription gateway
    ops have no persisted route row, but adapters take connection material
    ONLY from a ``ConnectionConfig`` — the marked pre-route env fallbacks
    was removed, so an unrouted call obtains its binding through the
    SAME machinery the routed path uses: the static provider +
    ``candidate_binding`` (for a known target id this is a deref, never a
    choice — §0.1). Nothing is persisted; the binding lives for the one
    invocation. A target that will not deref raises
    :class:`RouteBindingUnavailable` carrying its configuration remedy.
    """
    if provider is None:
        from frisket.execution.resolve_for_action import default_provider

        provider = default_provider()
    target = next((t for t in provider.targets() if t.id == target_id), None)
    if target is None:
        raise RouteBindingUnavailable(
            f"execution target '{target_id}' is not defined by the "
            "execution target provider"
        )
    from frisket.execution.resolver import _open_edition_facts

    binding = candidate_binding(_open_edition_facts(target, engine), provider)
    if isinstance(binding, Refusal):
        raise RouteBindingUnavailable(binding.remedy)
    return binding


def _provider_for_target(target_id: str, operator: str) -> str:  # noqa: D401
    """§7 provider grouping, derived from the route SNAPSHOT's target id —
    the same labels the pre-route factories stamped, sourced from the route
    instead of per-path literals."""
    if target_id == "local":
        return "local"
    if target_id == "local-onnx":
        return "local-onnx"
    if target_id == "models-gateway":
        return "frisket-sidecar"
    if target_id == DATALAB_TARGET_ID:
        return "datalab"
    # Third-party venues. Each label is the ``provider`` its own catalog row
    # and fact producer already use, so a receipt groups a routed geocode call
    # with the unrouted ones instead of opening a second provider bucket for
    # the same vendor.
    if target_id == DEEPL_TARGET_ID:
        return "deepl"
    if target_id == GOOGLE_TRANSLATE_TARGET_ID:
        return "google"
    if target_id == OPENCAGE_TARGET_ID:
        return "opencage"
    if target_id == NOMINATIM_TARGET_ID:
        return "nominatim"
    if target_id == US_CENSUS_TARGET_ID:
        return "us_census_acs"
    if target_id.startswith("remote-api:"):
        return target_id.split(":", 1)[1]
    # Downstream compositions may introduce their own stable target ids.  The
    # route's operator is the generic provider grouping for such a venue: it
    # was copied from that composition target, pinned in the route, compiled
    # into the promises, and re-verified by admission before any fact binds.
    # Base-known targets keep their durable historical groupings above.
    require_clean("route operator", operator)
    return operator


def _route_cost_source(
    cost_posture: str, transport: str, observed_cost: Any, declared: Any = None
) -> str:
    """§7 cost_source mapping rule, per path, from route-row facts only.

    - zero-tariff transports (local workers, the operator's own gateway):
      ``operator_borne`` -> ``free_local`` — genuinely zero, not unknown.
    - ``org_key`` posture: the org's own provider bill -> ``provider_billed``.
    - metered remote paths (``platform_metered`` or the open edition's
      operator-paid provider bills): costs come from pricing data; a missing
      observed cost is honestly ``unknown``.

    Two declarations carry information the route cannot derive and therefore
    survive binding: ``estimated`` says the provider did not report this
    call's cost and the adapter priced it from a list rate (Datalab's per-page
    fallback); ``free_public_api`` says the external provider bills nobody.
    Reclassifying that known-zero public call as ``pricing_data`` would mint
    tariff evidence where no priced offering exists.
    """
    if declared in {"estimated", "free_public_api"}:
        return str(declared)
    if transport in ZERO_TARIFF_TRANSPORTS:
        return "free_local"
    if cost_posture == "org_key":
        return "provider_billed"
    return "pricing_data" if observed_cost is not None else "unknown"


def bind_fact_to_route(
    route: Any,
    call: Mapping[str, Any],
    out: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One-ledger derivation (§7) for a single routed model-call fact dict.

    Capability-neutral; it was previously named for transcription and was
    already generic — every field it derives comes from the route row. The
    persisted snapshot ``capability`` selects the provenance shape's version,
    so an OCR epoch and a transcription epoch are distinct provenance even on
    the same target and a malformed fact cannot choose its own authority.

    Returns a NEW dict: route-derived fields (provider grouping,
    provider_kind, cost_source) from the route row + snapshot, with
    ``credential_source`` carrying the observed actual (settlement
    prices what happened; divergence from the pin is recorded, not
    normalized away);
    the §6 observation payload attached under :data:`ROUTE_OBSERVATION_KEY`
    for the fact writer's transaction. ``out`` is the engine result dict —
    the source of observed-only facts (revision/device/dtype) when the
    transport reports them.
    """
    snapshot = validated_target_snapshot(route.target_snapshot)
    target_id = str(snapshot["target_id"])
    transport = str(snapshot["transport"])
    kind = _TRANSPORT_PROVIDER_KIND.get(transport)
    if kind is None:
        raise ValueError(
            f"route {route.id} snapshot carries unknown transport {transport!r}"
        )
    bound = dict(call)
    if transport in ZERO_TARIFF_TRANSPORTS:
        # No separate credential is observable on these paths; the route's
        # PINNED source is the dispatch fact (§0.3).
        actual_credential = route.credential_source
    else:
        # The adapter's own observed provenance (router-reported source) —
        # ACTUAL wins on the fact (§0 truth
        # invariant); divergence from the route pin is recorded by the
        # observation point below, never silently normalized away.
        actual_credential = str(bound.get("credential_source") or "none")
    bound["provider"] = _provider_for_target(target_id, route.operator)
    bound["provider_kind"] = kind
    bound["credential_source"] = actual_credential
    bound["cost_source"] = _route_cost_source(
        route.cost_posture,
        transport,
        bound.get("provider_cost_usd"),
        declared=call.get("cost_source"),
    )
    observed: dict[str, Any] = {
        "provenance": binding_provenance_version(str(snapshot["capability"])),
        "target_id": target_id,
        "engine": str(bound.get("engine") or route.engine),
        "credential_source": actual_credential,
    }
    source = out or {}
    for key in _OBSERVED_ONLY_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value:
            observed[key] = value
    bound[ROUTE_OBSERVATION_KEY] = {"route_id": route.id, "observed": observed}
    return bound
