"""Static resolution: engine -> a preferred capable target, with route facts.

``resolve`` is PURE and sync over the ``ExecutionTargetProvider`` protocol:
no env reads, no I/O of its own — liveness is the provider's
``connection(target_id)`` answer, probed for EXACTLY the one statically
chosen target. The static choice needs no probes, so a local resolution never
probes the gateway; tests assert this behavior.

The authored-symbol-to-target preference table preserves the raw symbol
through resolution (aliases canonicalize here, after identity),
and the preference lookup matches the raw symbol exactly BEFORE any
canonical fallback — a saved spec's venue/cost/build/concurrency behavior is
preserved, never silently substituted. Historical spellings that no longer
validate have no pins:

- ``whisper`` (the advertised convenience alias) -> local, always

VENUE NEVER DEPENDS ON LIVENESS OR OPTIONAL CONTROLS: a fresh canonical
name picks the first declaration-order target (local-first), then validates
authored options against that target. Liveness is checked ONLY for that
chosen target (the static choice needs no probes). A chosen-but-dead target
is a ``no_live_target`` refusal with its remedy; an option that target cannot
honor is a ``no_capable_target`` refusal. Resolution never walks to another
venue because a toggle was enabled. This keeps network policy, estimates,
concurrency scoping, previews, and the catalog on one deterministic target;
a venue flip remains an explicit, separately bound choice.

The choice/binding split: ``resolve`` runs once server-side per action
invocation and produces the choice; the worker never re-resolves — it calls
``candidate_binding`` with the persisted route-row facts, which derefs
connection material + liveness by snapshot ``target_id`` ONLY. A
missing/renamed target at binding time is a ``no_live_target`` durable
failure, never a silent re-resolution.

Refusal families are open by addition: ``no_capable_target``
(nothing declares the ability), ``no_live_target`` (the mapped target exists
but is not activated/available — carries a remedy), ``policy_denied`` and
``unfunded`` for policy and funding checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from frisket.contracts.actions.schemas._engines import (
    CENSUS_ENGINE_TABLE,
    GEOCODE_ENGINE_TABLE,
    OCR_ENGINE_TABLE,
    TO_MARKDOWN_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    TRANSLATE_ENGINE_TABLE,
    EngineDeclaration,
    find_engine,
)
from frisket.execution.credential_use import pinned_credential_source
from frisket.execution.price_book import OperatorBorne, cost_posture_for
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExecutionTargetProvider,
)
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    REMOTE_API_TARGET_ID_PREFIX,
    CostPosture,
    EgressClass,
    ExecutionTarget,
    GeocodeOptionSupport,
    OcrOptionSupport,
    TargetEngineSupport,
    TranslateOptionSupport,
    validated_target_snapshot,
    require_clean,
    require_egress_class,
)

if TYPE_CHECKING:  # annotation-only: importing the capability compiler at
    # runtime from this generic module is the cycle that made
    # ResolvedExecution's compiled fields ``Any`` in the first place.
    from frisket.execution.promise_compiler import CostBasis
    from frisket.execution.promises import PromiseSet
    from frisket.execution.commercial import CommercialOffering
    from frisket.execution.commercial import CommercialPresentation

RefusalFamily = Literal[
    "no_capable_target", "no_live_target", "policy_denied", "unfunded"
]
Persistence = Literal["durable", "ephemeral"]

_REMOTE_API_PREFIX = REMOTE_API_TARGET_ID_PREFIX


@dataclass(frozen=True)
class ResolutionRequest:
    """One invocation's resolution input.

    ``engine`` is the RAW authored symbol (aliases/deprecated ids are
    canonicalized here via the capability's roster table).

    ``capability`` decides two things that must not be inferred: which roster table
    canonicalizes the engine symbol, and which target engine ROWS are
    candidates at all. It keeps the transcription default because
    ``resolve_for_action`` — the one production constructor — reads it off the
    recipe's own declaration, so a capability that forgets to declare fails at
    the recipe, not silently here.

    The real quantity is patched onto ``Resolution.estimate_basis`` after
    resolution because it needs the chosen venue's cost basis.
    """

    engine: str
    options: Mapping[str, Any] = field(default_factory=dict)
    capability: str = CAPABILITY_TRANSCRIBE


@dataclass(frozen=True)
class RouteRowFacts:
    """The resolved (composition-computed) route facts — the exact columns
    the route row records and every promised receipt field derives from.
    Never target constants: computed by ``resolve`` from
    (target definition defaults x CompositionFacts x credential decision).

    This is the one route-facts type. Its ``__post_init__`` validates every
    construction site (``resolve``, :func:`_open_edition_facts`,
    :func:`route_row_facts_from_row`, tests) is now validated, INCLUDING the
    persisted-route load path. This is deliberately fail-closed: a route row
    carrying a garbage egress class or a blank operator refuses loudly rather
    than flowing into binding, satisfaction
    evaluation, and receipt facts. The one load path,
    ``attempt_authority.admit_routed``, wraps the construction so the refusal
    surfaces as its existing typed
    durable failure, never a naked ``ValueError``."""

    target_id: str
    engine: str
    operator: str
    egress_class: EgressClass
    region: str | None
    credential_source: str
    cost_posture: CostPosture

    def __post_init__(self) -> None:
        require_clean("operator", self.operator)
        require_clean("credential_source", self.credential_source)
        require_clean("cost_posture", self.cost_posture)
        require_egress_class(self.egress_class)
        if self.region is not None:  # honest absence: None or a real value
            require_clean("region", self.region)


@dataclass(frozen=True)
class EstimateBasisInputs:
    """Resolution-time inputs to the cost-estimate basis:
    the hardware class the mapped deployment runs on and the measured
    quantity.

    ``pricing_key``/``quantity_unit`` left this type because they were copied
    off ``ConnectionConfig.extra`` — a connection describing itself as a rate
    card — and `pricing_key` had zero readers anywhere. The SKU is the price
    book's answer now, derived from (target, engine, funding), so a target
    definition can no longer disagree with the book about what it charges
    for."""

    hardware_class: str | None = None
    quantity_hint: float | None = None


@dataclass(frozen=True)
class Resolution:
    """The invocation-scoped choice: target + selected support snapshot +
    resolved route facts + estimate basis inputs.

    ``connection`` is the exact liveness/config read that made the choice
    dispatchable. Atomic queue publication admits its prepared attempt in the
    same transaction and reuses this value; the eventual worker still performs
    its own fresh persisted-route dereference at effect time.
    """

    target: ExecutionTarget
    support: TargetEngineSupport
    facts: RouteRowFacts
    estimate_basis: EstimateBasisInputs
    connection: ConnectionConfig


@dataclass(frozen=True)
class Refusal:
    """An honest inability, with the remedy that would change the answer."""

    family: RefusalFamily
    remedy: str
    engine: str | None = None
    target_id: str | None = None
    # True only when composition filtering made the exact commercial choice
    # absent.  Estimate surfaces must not fall back to an unrouted/provider
    # quote for this refusal: absence means no target and no quote.
    quote_absent: bool = False


@dataclass(frozen=True)
class CandidateBinding:
    """Dispatch-time binding candidate: the persisted route facts plus the
    LIVE deref (connection material + current target row) for satisfaction
    evaluation. Assembled by ``candidate_binding`` — never a re-choice."""

    facts: RouteRowFacts
    connection: ConnectionConfig
    target: ExecutionTarget


@dataclass(frozen=True)
class ResolvedExecution:
    """Resolution + persistence policy (§0.1). Previews are ``ephemeral``;
    the route writer (lane B/D) refuses ephemeral objects BY TYPE — the
    contract is declared here with the type, enforced by the writer.

    Lane D (§0.1/§4.1) adds the invocation-scoped compiled artifacts: the
    COMPILED promise set and the cost basis it was compiled from. Both are
    REQUIRED and TYPED (E-6): they were ``Any = None``, which made "resolved
    but not compiled" a representable state that only the route writer
    checked — at write time, far from the constructor that failed to fill
    it. The imports are ``TYPE_CHECKING``-only, so this generic module still
    carries no runtime dependency on the capability compiler (the cycle that
    made them ``Any`` in the first place), and ``resolve_for_action``
    remains the one constructor."""

    resolution: Resolution
    persistence: Persistence
    promise_set: "PromiseSet"
    cost_basis: "CostBasis"
    offering: "CommercialOffering | None" = None
    presentation: "CommercialPresentation | None" = None


def preview_resolution_in_scope(extras: Any) -> Resolution | None:
    """Read the host-validated preview choice; never mint durable authority."""
    if not extras or extras.get("preview") is not True:
        return None
    resolved = extras.get("preview_execution")
    if (
        isinstance(resolved, ResolvedExecution)
        and resolved.persistence == "ephemeral"
        and resolved.resolution.facts.egress_class in {"none", "operator_lan"}
    ):
        return resolved.resolution
    return None


# ---------------------------------------------------------------------------
# Static preference table.
# ---------------------------------------------------------------------------

# Target FAMILY keys: stable identifiers independent of instance labels.
# Remote API ids carry provider suffixes; every other id IS its own family.
_FAMILY_REMOTE_API = "remote-api"

# (A RAW_SYMBOL_TARGET_PINS table pinned raw authored symbols to a target
# family ahead of the canonical walk. It held one entry — ``whisper`` to local
# — but was behaviorally inert because ``whisper`` canonicalizes to
# ``faster_whisper``, which is declared only on the local target. The fixed
# gateway model has the distinct identity ``whisper-turbo``.
# It was deleted along with its pre-check, whose "no candidate has family
# local" condition ``build_static_targets`` cannot produce.)


def target_family(target_id: str) -> str:
    """The stable family key for a target id.

    Remote API instances carry per-provider suffixes; every other target id
    is already its own stable family key.
    """
    if target_id.startswith(_REMOTE_API_PREFIX):
        return _FAMILY_REMOTE_API
    return target_id


def support_inability(
    support: TargetEngineSupport,
    options: Mapping[str, Any],
) -> str | None:
    """Why this target's declared support cannot honor the authored options
    (None = able).

    This is the one declaration-driven ability checker. The durable resolver, the probe-free
    static preference, preview/direct dispatch, and the egress/network-policy
    classifier all consult THIS function identically, so a per-target
    inability always refuses honestly on every surface — a silently dropped
    option is the bug class this checker exists to kill, and it was exactly
    the state OCR was in previously (an authored ``language`` against the
    gateway's /ocr wire, which has no language field, simply vanished).

    The dispatch is on the row's own ``capability`` declaration, not on a
    guess from the option keys: the two vocabularies are disjoint today, but
    "diarize means transcription" is precisely the kind of implicit rule this
    release exists to replace with a declaration.
    """
    checker = _CAPABILITY_INABILITY.get(support.capability)
    if checker is None:  # pragma: no cover - the vocabulary is closed
        raise ValueError(
            f"no ability checker declared for capability {support.capability!r}"
        )
    return checker(support, options)


def _ocr_inability(
    support: TargetEngineSupport, options: Mapping[str, Any]
) -> str | None:
    """The OCR half of the ability checker.

    Two authored options vary by target and both used to be dropped in
    silence: the recognition-language hint (no field on the gateway's /ocr
    wire or Datalab's /convert) and ``searchable_pdf``, which cannot be
    composed at all without per-block geometry a VLM does not return.
    ``dpi`` is deliberately absent: rasterization happens app-side, before any
    engine is spoken to, so every target serves every DPI.
    """
    declared = support.options
    if not isinstance(declared, OcrOptionSupport):  # pragma: no cover
        # Unreachable by construction (``TargetEngineSupport`` refuses a row
        # whose options type does not match its capability); named rather than
        # asserted so a future declaration path cannot make it silent.
        raise ValueError(
            f"ocr support row for {support.engine!r} carries "
            f"{type(declared).__name__} options"
        )
    language = options.get("language")
    if language not in (None, "", []) and not declared.language:
        return (
            "a recognition-language hint (it detects script automatically — "
            "drop language to use it)"
        )
    if options.get("searchable_pdf") is True and not declared.geometry:
        return (
            "searchable PDF output (it returns no text geometry to compose a "
            "text layer from)"
        )
    return None


def _transcription_inability(
    support: TargetEngineSupport, options: Mapping[str, Any]
) -> str | None:
    """The transcription half: diarize (plus the speaker-count fields), vad,
    the language hint, context, clean output, and model_size.

    A target with a declared non-empty ``sizes``
    vocabulary serves exactly those author-selectable sizes; a target whose
    ``sizes`` is empty serves its capability-reported configured weights and
    accepts no size selection, so any authored ``model_size`` is an inability
    there.
    """
    declared = support.options
    if options.get("diarize") is True and declared.diarization_mode == "none":
        return "diarization"
    if declared.speaker_hint == "none" and any(
        options.get(field) is not None
        for field in ("num_speakers", "min_speakers", "max_speakers")
    ):
        return "speaker-count hints"
    if options.get("vad") is True and not declared.vad:
        return "voice-activity detection (vad)"
    language = options.get("language")
    if language not in (None, "", []) and not declared.language:
        return "a language hint"
    if options.get("context") is not None and not declared.context:
        return "a context (hotword) prompt"
    if options.get("clean") is not None and not declared.clean:
        return "clean transcript output"
    size = options.get("model_size")
    if isinstance(size, str) and size:
        if not support.sizes:
            return (
                "model size selection (it serves its configured weights "
                "without size selection — drop model_size to use it)"
            )
        if size not in support.sizes:
            return f"model size {size!r} (it serves " + "/".join(support.sizes) + ")"
    return None


def _translate_inability(
    support: TargetEngineSupport, options: Mapping[str, Any] | Any
) -> str | None:
    """The translate half: the source-language hint, in both directions.

    The wire param is a canonical LIST (``language: list[str] | None``, [] =
    auto), so "authored a source" is a non-empty list and "asked for
    auto-detect" is its absence — the two cases the two declared fields answer.
    """
    declared = support.options
    if not isinstance(declared, TranslateOptionSupport):  # pragma: no cover
        raise ValueError(
            f"translate support row for {support.engine!r} carries "
            f"{type(declared).__name__} options"
        )
    language = options.get("language")
    if language not in (None, "", []):
        if not declared.source_language:
            return (
                "a source-language hint (it names only the target language — "
                "drop language to use it)"
            )
        return None
    if not declared.auto_detect_source:
        return (
            "source-language auto-detection (it loads a per-language-pair "
            "model and needs an explicit source language)"
        )
    return None


def _to_markdown_inability(
    support: TargetEngineSupport, options: Mapping[str, Any] | Any
) -> str | None:
    """The to_markdown half. There is nothing to refuse, and that is a
    declaration rather than a stub: ``ToMarkdownOptionSupport`` declares no
    option fields because no ``MediaToMarkdownParams`` knob varies by venue
    (see that type). The checker still exists so the capability has a ROW in
    the table below — a missing row raises, and a capability whose options are
    never checked must say so here rather than by being absent.
    """
    del support, options
    return None


def _geocode_inability(
    support: TargetEngineSupport, options: Mapping[str, Any] | Any
) -> str | None:
    """The geocode half: a scoped/structured lookup only OpenCage honours."""
    declared = support.options
    if not isinstance(declared, GeocodeOptionSupport):  # pragma: no cover
        raise ValueError(
            f"geocode support row for {support.engine!r} carries "
            f"{type(declared).__name__} options"
        )
    if options.get("structured_query") is True and not declared.structured_query:
        return (
            "a scoped/structured lookup (it takes a free-text query only, and "
            "an unscoped match would return a confident wrong point)"
        )
    return None


def _census_inability(
    support: TargetEngineSupport, options: Mapping[str, Any] | Any
) -> str | None:
    """The census half: one venue, no refusable option (see
    ``CensusOptionSupport``). Present for the same reason
    :func:`_to_markdown_inability` is."""
    del support, options
    return None


#: capability -> its ability checker. The table IS the generalization: adding
#: a capability adds a row, never a branch inside a caller.
_CAPABILITY_INABILITY = {
    CAPABILITY_TRANSCRIBE: _transcription_inability,
    CAPABILITY_OCR: _ocr_inability,
    CAPABILITY_TRANSLATE: _translate_inability,
    CAPABILITY_TO_MARKDOWN: _to_markdown_inability,
    CAPABILITY_GEOCODE: _geocode_inability,
    CAPABILITY_CENSUS: _census_inability,
}

#: capability -> the word a refusal uses for it. Display only; the identity is
#: the capability token.
_CAPABILITY_WORD = {
    CAPABILITY_TRANSCRIBE: "transcription",
    CAPABILITY_OCR: "OCR",
    CAPABILITY_TRANSLATE: "translation",
    CAPABILITY_TO_MARKDOWN: "document conversion",
    CAPABILITY_GEOCODE: "geocoding",
    CAPABILITY_CENSUS: "census enrichment",
}


def capability_engine_table(capability: str) -> tuple[EngineDeclaration, ...]:
    """The roster table one capability's engine symbols canonicalize through.
    Same shape as the checker table above, same reason: a capability adds a
    row, never a branch inside a caller. An unknown capability is a
    programming error, never a silent transcription fallback.

    The mapping is built PER CALL so it reads this module's table bindings at
    call time — a module-level dict would freeze the import-time objects and
    silently ignore a substituted roster (the synthetic-table tests do exactly
    that, and a fence that stops seeing its double proves nothing).
    """
    tables: dict[str, tuple[EngineDeclaration, ...]] = {
        CAPABILITY_TRANSCRIBE: TRANSCRIBE_ENGINE_TABLE,
        CAPABILITY_OCR: OCR_ENGINE_TABLE,
        CAPABILITY_TRANSLATE: TRANSLATE_ENGINE_TABLE,
        CAPABILITY_TO_MARKDOWN: TO_MARKDOWN_ENGINE_TABLE,
        CAPABILITY_GEOCODE: GEOCODE_ENGINE_TABLE,
        CAPABILITY_CENSUS: CENSUS_ENGINE_TABLE,
    }
    try:
        return tables[capability]
    except KeyError:
        raise ValueError(f"unknown execution capability {capability!r}") from None


def preferred_static_choice(
    raw_engine: str,
    options: Mapping[str, Any],
    targets: Sequence[ExecutionTarget],
    *,
    capability: str = CAPABILITY_TRANSCRIBE,
) -> tuple[ExecutionTarget, TargetEngineSupport] | Refusal | None:
    """The preference WITHOUT liveness — the deterministic static choice
    every surface classifies from: the first declaration-order candidate.
    Options are then checked against that chosen target, never used to select
    a different venue. The answer is pure and probe-free, so dispatch-side
    consumers (concurrency scope, estimates, previews, and egress policy)
    key on the same target the resolver binds.

    Returns the chosen ``(target, support)`` pair; a ``no_capable_target``
    ``Refusal`` when its target cannot honor the authored options (options
    are refused, never silently ignored or used to retarget); or ``None``
    for unknown symbols, free-form ``provider/model`` ids (their remote-api
    target is keyed by provider, not preference), and symbols the static
    targets simply do not declare (callers keep their own fallbacks/
    messages for those)."""
    engine_id = _canonical_engine(raw_engine, capability)
    if engine_id is None or "/" in engine_id:
        return None
    candidates = [
        (target, support)
        for target in targets
        for support in target.engines
        if support.capability == capability and support.engine == engine_id
    ]
    if not candidates:
        return None
    target, support = candidates[0]
    inability = support_inability(support, options)
    if inability is not None:
        return Refusal(
            family="no_capable_target",
            remedy=(
                f"target '{target.id}' for engine {engine_id!r} does not "
                f"support {inability}"
            ),
            engine=engine_id,
            target_id=target.id,
        )
    return target, support


def _canonical_engine(raw: str, capability: str = CAPABILITY_TRANSCRIBE) -> str | None:
    """Canonicalize an authored engine symbol for target lookup, through the
    CAPABILITY's roster table: aliases -> canonical id; the ``resolves_to``
    machinery applied (no row uses it since the ``remote``
    placement id); free-form ``provider/model`` ids passed through; unknown
    symbols -> None (deleted spellings land here and refuse upstream
    at validation)."""
    entry = find_engine(capability_engine_table(capability), raw)
    if entry is not None:
        return entry.resolves_to or entry.id
    if "/" in raw:
        provider, _, model = raw.partition("/")
        if provider.strip() and model.strip():
            return raw
    return None


def _liveness_remedy(
    provider: ExecutionTargetProvider, target_id: str, capability: str | None
) -> str:
    """``capability`` is a REQUIRED positional here (bug pattern #1: it is
    exactly the argument a call site would otherwise forget), so every
    ``no_live_target`` refusal states which work it is talking about. One
    target row can be adopted by several capabilities —
    ``remote-api:{provider}`` carries both transcription and VLM OCR engines,
    under the SAME ``{provider}/*`` wildcard symbol — so a remedy that assumes
    one of them is confidently wrong for the other.

    ``None`` is the honest answer on the persisted-route binding path: a
    ``RouteRowFacts`` row records the engine, not the capability, and the two
    wildcard rows share an engine symbol, so the capability is genuinely not
    recoverable there. It yields a generic-but-true remedy rather than a
    guess.

    The hook itself stays the port's OPTIONAL duck-typed enrichment (see
    ``execution/provider.py``): a provider whose ``liveness_remedy`` predates
    the capability argument — an external composition ships its own
    implementation over a ``targets`` table — is called the one-argument way,
    and its remedy is still better than the generic sentence below. The
    signature is INSPECTED rather than probed with ``try/TypeError``, so a
    ``TypeError`` raised
    inside a hook is never mistaken for a signature mismatch.
    """
    import inspect

    hook = getattr(provider, "liveness_remedy", None)
    if callable(hook):
        try:
            accepts_capability = "capability" in inspect.signature(hook).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            accepts_capability = False
        remedy = (
            hook(target_id, capability=capability)
            if accepts_capability
            else hook(target_id)
        )
        if remedy:
            return str(remedy)
    return (
        f"Execution target '{target_id}' is not currently available; "
        "configure it and retry."
    )


def _open_edition_facts(target: ExecutionTarget, engine_id: str) -> RouteRowFacts:
    """§3.2 composition-resolved rules for edition="open" (anchor O1): the
    operator runs and pays for everything — credentials are the operator's
    own env-held material (``local``, matching today's fact stamping for
    local/sidecar/env-key paths) and every cost posture is ``operator_borne``
    (free-local class for no-tariff targets; the operator's own provider bill
    otherwise). ``region`` follows the
    honest-absence rule: nothing is pinned in the open edition.

    The open composition is ``OperatorBorne`` BY CONSTRUCTION
    (``CompositionFacts.__post_init__`` refuses any other funding for
    edition="open"), so both facts are derived from that one funding class
    rather than restated as literals — the same derivation
    :func:`_hosted_edition_facts` uses, so the two editions cannot drift on
    what "the operator's own key" means (§3.5)."""
    funding = OperatorBorne()
    return RouteRowFacts(
        target_id=target.id,
        engine=engine_id,
        operator=target.operator,
        egress_class=target.egress_class,
        region=target.region,
        credential_source=pinned_credential_source(funding),
        cost_posture=cost_posture_for(funding),
    )


def _hosted_edition_facts(
    target: ExecutionTarget, engine_id: str, composition: CompositionFacts
) -> RouteRowFacts:
    """The sibling of :func:`_open_edition_facts` for edition="hosted",
    replacing the ``ValueError`` tripwire that used to stand where
    this branch belongs.

    ``funding`` is the credential/cost-posture fact the composition decides.
    It does not select a price or create an offer. Operator and egress class
    stay the target's own declarations; a resolver that overrode them would
    be fabricating facts about somebody else's row.
    """
    return RouteRowFacts(
        target_id=target.id,
        engine=engine_id,
        operator=target.operator,
        egress_class=target.egress_class,
        region=target.region,
        # ONE mint of the pinned credential token, in the module that also
        # owns the CLASS the adapter fence checks against (§3.5).
        credential_source=pinned_credential_source(composition.funding),
        cost_posture=cost_posture_for(composition.funding),
    )


def _edition_facts(
    target: ExecutionTarget, engine_id: str, composition: CompositionFacts
) -> RouteRowFacts:
    """THE edition dispatch. An unknown edition is a programming error, not a
    refusal: resolving it would fabricate operator/credential facts."""
    if composition.edition == "open":
        return _open_edition_facts(target, engine_id)
    if composition.edition == "hosted":
        return _hosted_edition_facts(target, engine_id, composition)
    raise ValueError(f"unknown composition edition {composition.edition!r}")


def route_row_facts_from_row(route: Any) -> RouteRowFacts:
    """The projection from a persisted route row to its route facts.

    The sibling of :func:`_open_edition_facts` (which projects from a target
    definition at resolution time): this one projects the stored row,
    for the ONE dispatch-time reader, ``attempt_authority.admit_routed``.
    Two readers used to hand-build the identical seven-field construction and
    had already drifted in their comments; there is one construction now, so
    the ``target_id`` fallback,
    the runtime-``str`` egress class, and the honest-absence ``region`` can
    never disagree between the queued and direct dispatch paths.

    Raises ``ValueError`` (from :class:`RouteRowFacts` validation) when the
    persisted row carries unusable facts; callers wrap it in their own typed
    refusal.
    """
    snapshot = validated_target_snapshot(route.target_snapshot)
    return RouteRowFacts(
        target_id=snapshot["target_id"],
        engine=route.engine,
        # Runtime strs off the row; typed at authoring, validated on
        # construction (a corrupt egress class refuses here).
        egress_class=route.egress_class,
        operator=route.operator,
        region=route.region,
        credential_source=route.credential_source,
        cost_posture=route.cost_posture,
    )


def resolve(
    request: ResolutionRequest,
    provider: ExecutionTargetProvider,
    composition: CompositionFacts,
) -> Resolution | Refusal:
    """Map one capability request to its single target, or refuse.

    Pure over ``provider``; sync; exactly one ``connection`` probe (the
    mapped target's) — no enumeration of other targets' liveness.
    """
    capability = request.capability
    engine_id = _canonical_engine(request.engine, capability)
    if engine_id is None:
        return Refusal(
            family="no_capable_target",
            remedy=(
                f"unknown {_CAPABILITY_WORD[capability]} engine "
                f"{request.engine!r}; use a roster engine id or a "
                "provider-qualified '<provider>/<model>'"
            ),
            engine=request.engine,
        )

    targets = list(provider.targets())
    if "/" in engine_id:
        provider_name = engine_id.partition("/")[0]
        target = next(
            (t for t in targets if t.id == _REMOTE_API_PREFIX + provider_name), None
        )
        if target is None:
            return Refusal(
                family="no_capable_target",
                remedy=(
                    f"no remote-api target exists for provider "
                    f"{provider_name!r}; the model router does not know it"
                ),
                engine=engine_id,
            )
        support = next(
            (
                s
                for s in target.engines
                if s.capability == capability and s.engine == engine_id
            ),
            None,
        )
        if support is None:
            wildcard = next(
                (
                    s
                    for s in target.engines
                    if s.capability == capability and s.engine == f"{provider_name}/*"
                ),
                None,
            )
            if wildcard is None:
                return Refusal(
                    family="no_capable_target",
                    remedy=(
                        f"target '{target.id}' declares no free-form engine support"
                    ),
                    engine=engine_id,
                    target_id=target.id,
                )
            support = replace(wildcard, engine=engine_id)
        # The one ability checker covers free-form ids too:
        # a provider/model engine with an authored knob its remote-transport
        # support row does not declare (vad, diarize/speaker counts,
        # model_size, context) refuses honestly — never a silent drop.
        inability = support_inability(support, request.options)
        if inability is not None:
            return Refusal(
                family="no_capable_target",
                remedy=(
                    f"engine {engine_id!r} on target '{target.id}' does not "
                    f"support {inability}"
                ),
                engine=engine_id,
                target_id=target.id,
            )
        connection = provider.connection(target.id)
        if connection is None:
            return Refusal(
                family="no_live_target",
                remedy=_liveness_remedy(provider, target.id, capability),
                engine=engine_id,
                target_id=target.id,
            )
    else:
        candidates = [
            (t, s)
            for t in targets
            for s in t.engines
            if s.capability == capability and s.engine == engine_id
        ]
        if not candidates:
            return Refusal(
                family="no_capable_target",
                remedy=(f"no execution target declares engine {engine_id!r}"),
                engine=engine_id,
            )
        # One deterministic choice: the first declaration-order candidate.
        # Optional controls are validated on it and cannot retarget a run.
        # The same probe-free function backs every dispatch-side consumer, so
        # the venue the resolver binds is the venue policy/estimate/preview
        # classified.
        choice = preferred_static_choice(
            request.engine, request.options, targets, capability=capability
        )
        if isinstance(choice, Refusal):
            return choice
        if choice is None:  # pragma: no cover - guarded by the checks above
            return Refusal(
                family="no_capable_target",
                remedy=(f"no execution target declares engine {engine_id!r}"),
                engine=engine_id,
            )
        target, support = choice
        # O1 short-circuit: the static choice needed no probes; liveness is
        # checked ONLY for the chosen target. Dead chosen target ->
        # no_live_target with its remedy — never a walk to another venue
        # (a venue flip is a claims change, §10).
        connection = provider.connection(target.id)
        if connection is None:
            return Refusal(
                family="no_live_target",
                remedy=_liveness_remedy(provider, target.id, capability),
                engine=engine_id,
                target_id=target.id,
            )

    facts = _edition_facts(target, engine_id, composition)
    estimate_basis = EstimateBasisInputs(hardware_class=connection.extra.get("gpu"))
    return Resolution(
        target=target,
        support=support,
        facts=facts,
        estimate_basis=estimate_basis,
        connection=connection,
    )


def candidate_binding(
    route_row_facts: RouteRowFacts,
    provider: ExecutionTargetProvider,
) -> CandidateBinding | Refusal:
    """Dispatch-time deref for a PERSISTED route: connection + liveness by
    the snapshot's ``target_id`` ONLY — never a re-choice (§0.1/§0.4). A
    missing or renamed target is a durable ``no_live_target``, and a dead
    one refuses with its remedy; satisfaction evaluation over the returned
    binding is lane E's.
    """
    target = next(
        (t for t in provider.targets() if t.id == route_row_facts.target_id), None
    )
    if target is None:
        return Refusal(
            family="no_live_target",
            remedy=(
                f"execution target '{route_row_facts.target_id}' recorded on "
                "this route no longer exists; re-run to resolve (and "
                "re-consent) against a current target"
            ),
            engine=route_row_facts.engine,
            target_id=route_row_facts.target_id,
        )
    connection = provider.connection(route_row_facts.target_id)
    if connection is None:
        return Refusal(
            family="no_live_target",
            # See _liveness_remedy: a persisted route row carries the engine,
            # not the capability, and the transcription/OCR rows share the
            # `{provider}/*` symbol — so there is nothing honest to pass.
            remedy=_liveness_remedy(provider, route_row_facts.target_id, None),
            engine=route_row_facts.engine,
            target_id=route_row_facts.target_id,
        )
    return CandidateBinding(facts=route_row_facts, connection=connection, target=target)
