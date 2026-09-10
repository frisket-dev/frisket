"""Single resolution per action invocation plus route/consent persistence.

``resolve_for_action`` is the capability-dispatch seam ``validate_spec``
calls EXACTLY ONCE per action invocation: for a resolution-aware recipe
(markers ``consumes_resolution = True`` + ``execution_capability``) it maps
the request to its one target,
builds the estimate/cost basis, compiles the promise set, and returns an
invocation-scoped ``ResolvedExecution``; for every other recipe it returns
``None`` and the action proceeds exactly as today. Estimate, the claims gate,
and route persistence all consume THAT object; nothing downstream re-resolves.

The capability-specific surface is the cost-basis mint table plus the
authored-option key sets; every capability uses the one route compiler.
Everything else in this module — coverage, consent, the route writer, and the
successor writer — is written against route facts and promise sets.

``record_resolved_execution`` is the route writer for a FRESH launch:
it persists consent (when a consent event happened) + promise set + route
through ``RouteStore``, refuses ephemeral objects BY TYPE (previews carry
``persistence="ephemeral"`` and never touch disk), and runs inside the
caller's project-store transaction when one is supplied.

``confirm_changed_claims`` is its successor-chain twin: the
ONE construction behind every confirmation that ADVANCES an existing run's
consent chain — the backfill-confirm branch of ``validate_spec``. Same three
rows, appended at the head the caller gated against instead of at an empty
chain.

Hash identity note: the promise-set hash echoed
through the 402 confirm, recorded on the consent row, and stored on the
``promise_sets`` row are ALL ``frisket.execution.promises.promise_rows_hash``
over the canonical ``HASHED_PROMISE_KEYS`` row projection (the store/wire
list shape) — ``PromiseSet.set_hash``, ``consented_set_hash``, and the
store column are the SAME digest, so the ``consent.promise_set_hash == head
set hash`` coverage check is an equality of one construction with
itself.

``action_identity_hash`` lives in the sibling
``frisket.execution.action_identity`` because the allowlist construction is a
projection through the action's op declaration, which needs the sdk
declaration registry this module must not import at module scope. It is
re-exported here so the mint, reconsent, and worker verification keep
importing ONE name from ONE place.
"""

from __future__ import annotations
import json

import sqlite3
from frisket.execution.consent_coverage import (
    ConsentCoverage,
    effective_consent_coverage,
)
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Mapping, Sequence

from frisket.execution.action_identity import action_identity_hash
from frisket.execution.claim_labels import claim_display
from frisket.execution.commercial import CommercialOffering, CommercialPresentation
from frisket.execution.promise_compiler import (
    CostBasis,
    OperatorBorneZeroCost,
    compile_route_promises,
)
from frisket.execution.price_book import (
    Funding,
    PlatformMetered,
    funding_for_cost_posture,
    quote_census,
    quote_geocode,
    quote_ocr,
    quote_to_markdown,
    quote_transcription,
    quote_translate,
    sku_for,
)
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    validated_target_snapshot,
)
from frisket.execution.promises import (
    HASHED_PROMISE_KEYS,
    Promise,
    PromiseSet,
    promise_rows_hash,
)
from frisket.execution.provider import ExecutionComposition
from frisket.execution.resolver import (
    Persistence,
    Refusal,
    ResolvedExecution,
    Resolution,
    ResolutionRequest,
    RouteRowFacts,
    _canonical_engine,
    resolve,
)

# The authored option keys a runner spec may carry, PER CAPABILITY; these
# shape the route row's options_json and the resolution request. Confirmation
# plumbing (``confirmed``, ``consented_promise_set_hash``) is deliberately
# NOT an option — it never enters route or identity material.
#
# The OCR set is short because most of what an OCR spec carries is not a
# target-varying option: ``dpi`` is app-side rasterization every target sees
# identically, ``output_name`` is an output intent. What is here is what a
# target can honestly refuse (``support_inability``): the recognition-language
# hint and searchable-PDF composition.
_CAPABILITY_OPTION_KEYS: dict[str, tuple[str, ...]] = {
    CAPABILITY_TRANSCRIBE: (
        "vad",
        "language",
        "model_size",
        "context",
        "clean",
        "diarize",
        "num_speakers",
        "min_speakers",
        "max_speakers",
    ),
    CAPABILITY_OCR: (
        "language",
        "searchable_pdf",
    ),
    # Each set is exactly what its capability's ability checker can
    # refuse — not every authored key. ``target_language``/``output_name``/
    # ``include_moe``/``geography`` are absent for the reason ``dpi`` is absent
    # from the OCR set: no declared target answers them differently, so putting
    # them here would widen the route's identity material with knobs that
    # cannot change the route.
    CAPABILITY_TRANSLATE: ("language",),
    # No target-varying knob exists (``ToMarkdownOptionSupport`` says why),
    # so the honest set is empty. An empty tuple is a DECLARATION — the
    # capability is registered and has nothing to declare — where an absent
    # key would make ``authored_options`` raise "unknown capability".
    CAPABILITY_TO_MARKDOWN: (),
    CAPABILITY_GEOCODE: ("structured_query",),
    CAPABILITY_CENSUS: (),
}


SCRATCH_PREVIEW_UNIT_ID = 1


@dataclass(frozen=True)
class BoundedScratchInput:
    """Server-measured input for an accounted scratch-media preview."""

    quantity: float
    work_scope: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, (int, float))
            or not isfinite(float(self.quantity))
            or self.quantity < 0
        ):
            raise ValueError("bounded scratch quantity must be non-negative")
        if not isinstance(self.work_scope, Mapping):
            raise TypeError("bounded scratch work scope must be a mapping")


def default_provider(project: Any | None = None, router: Any | None = None) -> Any:
    """Build the open-edition provider used by legacy *dispatch* binding.

    Resolution deliberately does not call this helper: ``resolve_for_action``
    requires an :class:`ExecutionComposition`, so a hosted request cannot
    silently fall back to open-edition facts.  Unrouted/legacy dispatch still
    needs a small provider factory until that older surface is retired.
    """
    from frisket.execution.definitions import StaticExecutionTargetProvider

    return StaticExecutionTargetProvider(secrets=project, router=router)


def authored_options(
    spec: Mapping[str, Any], capability: str = CAPABILITY_TRANSCRIBE
) -> dict[str, Any]:
    """The authored option subset of one capability's runner spec (route
    ``options_json`` + ``ResolutionRequest.options``).

    ``capability`` defaults to transcription for the three dispatch-side
    callers that are transcription BY CONSTRUCTION (the transcribe recipe's
    own transport preference, the network policy's transcribe branch); every
    resolution-time caller passes the recipe's declared capability.
    """
    keys = _CAPABILITY_OPTION_KEYS.get(capability)
    if keys is None:
        raise ValueError(f"unknown execution capability {capability!r}")
    return {key: spec[key] for key in keys if key in spec and spec[key] is not None}


# ---------------------------------------------------------------------------
# Cost basis (transcription)
# ---------------------------------------------------------------------------


def _transcribe_row_quantities_seconds(
    project: Any, spec: Mapping[str, Any], recipe: Any
) -> dict[int, float] | None:
    """Selected audio seconds keyed by exact row, or ``None`` if unknown.

    The aggregate quote is derived from this allocation, not vice versa.
    Empty media cells retain an explicit zero allocation so the authorized
    scope and the row-quote scope cannot silently diverge.
    """
    from frisket.engine.runner.row_inputs import row_values
    from frisket.engine.runner.validation import target_rows
    from frisket.sdk.ops.transcribe_engines import total_audio_seconds

    row_ids = target_rows(project, dict(spec))
    col_map = {c["name"]: c["id"] for c in project.columns(spec["sheet_id"])}
    quantities: dict[int, float] = {}
    for row_id in row_ids:
        values = row_values(
            project, recipe, dict(spec), col_map, row_id, for_model=False
        )
        seconds = total_audio_seconds([values], project)
        if seconds is None:
            return None
        quantities[int(row_id)] = seconds
    return quantities


def _document_quantity_pages(
    project: Any, spec: Mapping[str, Any], recipe: Any
) -> int | None:
    """Total selected PAGES, or ``None`` when a selected row's page count is
    not knowable before the run.

    Shared by the OCR and to_markdown capabilities, because it is the same
    measurement of the same cells: both meter the pages of the documents the
    user selected, and both read the count the ingest probe recorded. Two
    copies would be two answers to one question the moment either learned a new
    media shape.

    A stored image is one page; document page counts come from ingest-owned
    blob metadata. Inline content and paths have no pre-run page measurement.
    Multiple nonempty sources are also unknown: a typed handler may choose
    any of them, so the first cell cannot stand in for its actual arguments.
    One unknown row makes the total unknown, never a smaller number than the
    truth. Blank rows contribute zero.
    """
    from frisket.engine.runner.row_inputs import is_empty_cell_value, row_values
    from frisket.engine.runner.validation import target_rows
    from frisket.engine.store.media_blobs import MediaBlobStore

    row_ids = target_rows(project, dict(spec))
    col_map = {c["name"]: c["id"] for c in project.columns(spec["sheet_id"])}
    blobs = MediaBlobStore(project)
    total = 0
    for row_id in row_ids:
        values = row_values(
            project, recipe, dict(spec), col_map, row_id, for_model=False
        )
        candidates = [v for v in values.values() if not is_empty_cell_value(v)]
        if not candidates:
            continue
        if len(candidates) != 1:
            return None
        media = candidates[0]
        if not isinstance(media, dict) or not isinstance(media.get("blob"), str):
            return None
        try:
            blob = blobs.blob_row(media["blob"])
            if blob is None:
                return None
            probe = blobs.probe_metadata(media["blob"])
        except Exception:  # noqa: BLE001 - estimates fall back to unknown
            return None
        pages = probe.get("pages")
        if pages is None:
            # Use stored facts, not an authored cell's MIME declaration.
            if probe.get("kind") == "image" or (
                not probe.get("kind") and str(blob["mime"]).startswith("image/")
            ):
                total += 1
                continue
            return None
        if type(pages) is not int or pages <= 0:
            return None
        total += pages
    return total


def _ocr_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, int | None]:
    """(cost basis, pages), rated through THE price book — the OCR sibling of
    :func:`_transcribe_cost_basis`, same shape, different unit.

    The free-local short-circuit is the same one, and it is what keeps the
    O1 rent bound true for a second capability: a local rapidocr resolution
    never loads a single row's media metadata.
    """
    funding = funding_for_cost_posture(resolution.facts.cost_posture)
    target_id = resolution.target.id
    engine = resolution.facts.engine
    offering = composition.offering_for(
        target_id=target_id,
        capability=CAPABILITY_OCR,
        engine=engine,
    )
    if offering is None and (
        isinstance(funding, PlatformMetered)
        or sku_for(
            capability=CAPABILITY_OCR,
            target_id=target_id,
            engine=engine,
        )
        is None
    ):
        return OperatorBorneZeroCost(), None
    pages = _document_quantity_pages(project, spec, recipe)
    basis = quote_ocr(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        pages=pages,
    )
    return basis, pages


def _transcribe_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, float | None]:
    """(cost basis, audio seconds), rated through THE price book.

    Free-local class targets never load row media metadata at all (O1 rent: a
    local resolution costs one liveness probe plus this constant-time
    return), so the book is asked for the SKU first and the quantity is
    measured only when there is something to price.

    ``funding`` is read back off the resolved route facts rather than
    re-derived from the target id: ``cost_posture`` and the cost basis used
    to be two unconnected answers to "who pays" (a constant and a
    string-prefix dispatch), and the route facts are the one authority
    both read.
    """
    funding = funding_for_cost_posture(resolution.facts.cost_posture)
    target_id = resolution.target.id
    engine = resolution.facts.engine
    offering = composition.offering_for(
        target_id=target_id,
        capability=CAPABILITY_TRANSCRIBE,
        engine=engine,
    )
    if offering is None and (
        isinstance(funding, PlatformMetered)
        or sku_for(
            capability=CAPABILITY_TRANSCRIBE,
            target_id=target_id,
            engine=engine,
        )
        is None
    ):
        return OperatorBorneZeroCost(), None
    row_seconds = _transcribe_row_quantities_seconds(project, spec, recipe)
    seconds = None if row_seconds is None else sum(row_seconds.values())
    basis = quote_transcription(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        audio_seconds=seconds,
        hardware_class=resolution.estimate_basis.hardware_class,
        row_audio_seconds=row_seconds,
    )
    return basis, seconds


# ---------------------------------------------------------------------------
# Cost basis
# ---------------------------------------------------------------------------


def _free_local_or(
    capability: str,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[Funding, str, str, CommercialOffering | None] | None:
    """``(funding, target_id, engine)`` when this route costs something, or
    ``None`` when the book says nothing is charged at all.

    The free-local short-circuit, in one place for the four
    capabilities: it keeps free-local estimation constant-time —
    a local opus_mt or markitdown resolution never loads a single row's cell
    values, exactly as a local rapidocr one never loads media metadata.
    """
    funding = funding_for_cost_posture(resolution.facts.cost_posture)
    target_id = resolution.target.id
    engine = resolution.facts.engine
    offering = composition.offering_for(
        target_id=target_id,
        capability=capability,
        engine=engine,
    )
    if offering is None and (
        isinstance(funding, PlatformMetered)
        or sku_for(
            capability=capability,
            target_id=target_id,
            engine=engine,
        )
        is None
    ):
        return None
    return funding, target_id, engine, offering


def _translate_quantity_characters(
    project: Any, spec: Mapping[str, Any], recipe: Any
) -> int | None:
    """Total selected SOURCE characters — what both hosted MT providers bill
    on, measured off the same cells the run will send.

    It reads ``translation_text``, the capability's OWN definition of
    what leaves the box (labels excluded, blobs skipped), rather than
    re-deriving a join here: a quote measured off a different string than the
    one dispatch sends is the two-surfaces-two-answers bug in its purest form.
    """
    from frisket.engine.runner.row_inputs import row_values
    from frisket.engine.runner.validation import target_rows
    from frisket.actions.translate_types import translation_text

    row_ids = target_rows(project, dict(spec))
    col_map = {c["name"]: c["id"] for c in project.columns(spec["sheet_id"])}
    total = 0
    for row_id in row_ids:
        values = row_values(
            project, recipe, dict(spec), col_map, row_id, for_model=False
        )
        total += len(translation_text(values))
    return total


def _selected_row_count(project: Any, spec: Mapping[str, Any]) -> int:
    """The number of selected rows — the quantity for both per-row
    capabilities. One lookup per row is what each of them buys."""
    from frisket.engine.runner.validation import target_rows

    return len(target_rows(project, dict(spec)))


def _translate_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, int | None]:
    """(cost basis, characters), rated through THE price book."""
    resolved = _free_local_or(CAPABILITY_TRANSLATE, resolution, composition)
    if resolved is None:
        return OperatorBorneZeroCost(), None
    funding, target_id, engine, offering = resolved
    characters = _translate_quantity_characters(project, spec, recipe)
    basis = quote_translate(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        characters=characters,
    )
    return basis, characters


def _to_markdown_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, int | None]:
    """(cost basis, pages), rated through THE price book.

    The page count is the SAME read OCR does (:func:`_document_quantity_pages`)
    — a document whose page count was never probed makes the whole quote
    unpriceable rather than a smaller number than the truth.
    """
    resolved = _free_local_or(CAPABILITY_TO_MARKDOWN, resolution, composition)
    if resolved is None:
        return OperatorBorneZeroCost(), None
    funding, target_id, engine, offering = resolved
    pages = _document_quantity_pages(project, spec, recipe)
    basis = quote_to_markdown(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        pages=pages,
    )
    return basis, pages


def _geocode_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, int | None]:
    """(cost basis, rows), rated through THE price book."""
    del recipe
    resolved = _free_local_or(CAPABILITY_GEOCODE, resolution, composition)
    if resolved is None:
        return OperatorBorneZeroCost(), None
    funding, target_id, engine, offering = resolved
    rows = _selected_row_count(project, spec)
    basis = quote_geocode(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        rows=rows,
    )
    return basis, rows


def _census_cost_basis(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    resolution: Resolution,
    composition: ExecutionComposition,
) -> tuple[CostBasis, int | None]:
    """(cost basis, rows), rated through THE price book."""
    del recipe
    resolved = _free_local_or(CAPABILITY_CENSUS, resolution, composition)
    if resolved is None:
        return OperatorBorneZeroCost(), None
    funding, target_id, engine, offering = resolved
    rows = _selected_row_count(project, spec)
    basis = quote_census(
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        rows=rows,
    )
    return basis, rows


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def resolve_for_action(
    project: Any,
    spec: Mapping[str, Any],
    recipe: Any,
    *,
    composition: ExecutionComposition,
    persistence: Persistence = "durable",
    scope_row_ids: Sequence[int] | None = None,
    bounded_scratch_input: BoundedScratchInput | None = None,
) -> ResolvedExecution | Refusal | None:
    """Resolve one action invocation through the execution seam.

    Returns ``None`` for recipes that do not consume resolution (they
    proceed exactly as today), a ``Refusal`` for an honest inability (the
    caller maps it to an actionable validation error), or a
    ``ResolvedExecution`` carrying resolution + estimate basis + compiled
    promise set + cost basis + persistence policy.
    """
    if not recipe.consumes_resolution:
        return None
    # The recipe declares which capability it resolves for. Read as an
    # attribute, not with a getattr default, for the same reason
    # ``consumes_resolution`` is: a recipe that consumes resolution without
    # saying what kind of work it is must fail loudly here, never resolve as
    # transcription by accident.
    capability = recipe.execution_capability
    if capability not in _CAPABILITY_COST_BASIS:
        raise ValueError(
            f"recipe '{getattr(recipe, 'name', recipe)}' declares "
            f"execution_capability={capability!r}, which no execution-seam "
            "capability serves"
        )
    quote = _CAPABILITY_COST_BASIS[capability]
    raw_engine = (
        _resolve_geocode_engine(
            spec, project, credential_context=composition.credential_use_context
        )
        if capability == CAPABILITY_GEOCODE
        else str(spec.get("engine") or _default_engine(capability))
    )
    options = authored_options(spec, capability)
    request = ResolutionRequest(
        engine=raw_engine, options=options, capability=capability
    )
    canonical_engine = _canonical_engine(raw_engine, capability)
    outcome = resolve(
        request,
        composition.provider_for_resolution(),
        composition.facts,
    )
    if isinstance(outcome, Refusal):
        engine = outcome.engine
        if engine is not None:
            raw_declares = _target_rows_declare_engine(
                composition.provider.targets(), capability=capability, engine=engine
            )
            resolution_declares = _target_rows_declare_engine(
                composition.resolution_targets(),
                capability=capability,
                engine=engine,
            )
            if raw_declares and not resolution_declares:
                # The provider still knows this engine, but the request's
                # commercial composition intentionally removed it.  That is
                # absence, not an unpriceable route: downstream estimate
                # surfaces must not mint a legacy/provider quote for it.
                outcome = replace(outcome, quote_absent=True)
            elif (
                isinstance(composition.facts.funding, PlatformMetered)
                and canonical_engine is not None
            ):
                # Even a provider that supplies no rows at all cannot turn a
                # known platform-world engine into the recipe's unrouted local
                # or provider-direct quote.  Unknown authored symbols retain
                # their ordinary invalid-engine refusal semantics.
                outcome = replace(outcome, quote_absent=True)
        return outcome

    selected_offering = composition.offering_for(
        target_id=outcome.target.id,
        capability=capability,
        engine=outcome.facts.engine,
    )
    from frisket.execution.commercial import commercial_offering_allowed

    if (
        isinstance(composition.facts.funding, PlatformMetered)
        and selected_offering is None
        and not composition.includes_execution_match(
            target_id=outcome.target.id,
            capability=capability,
            engine=outcome.facts.engine,
        )
        and commercial_offering_allowed(
            target_id=outcome.target.id,
            capability=capability,
            engine=outcome.facts.engine,
        )
    ):
        # A funding marker is not an offering.  In the platform-metered world
        # an exact unmatched target simply does not exist for this request;
        # it must not fall through as a free or unpriceable quote.
        return Refusal(
            family="unfunded",
            remedy=(
                "No commercial offering is available for this exact execution "
                "target and engine. Choose an offered target or change the "
                "execution funding configuration."
            ),
            engine=outcome.facts.engine,
            target_id=outcome.target.id,
            quote_absent=True,
        )

    if bounded_scratch_input is None:
        cost_basis, quantity = quote(project, spec, recipe, outcome, composition)
    else:
        quantity = bounded_scratch_input.quantity
        funding = funding_for_cost_posture(outcome.facts.cost_posture)
        offering = composition.offering_for(
            target_id=outcome.target.id,
            capability=capability,
            engine=outcome.facts.engine,
        )
        if capability == CAPABILITY_TRANSCRIBE:
            cost_basis = quote_transcription(
                target_id=outcome.target.id,
                engine=outcome.facts.engine,
                funding=funding,
                offering=offering,
                audio_seconds=float(quantity),
                hardware_class=outcome.estimate_basis.hardware_class,
                row_audio_seconds={SCRATCH_PREVIEW_UNIT_ID: float(quantity)},
            )
        elif capability == CAPABILITY_OCR:
            if int(quantity) != quantity or quantity <= 0:
                raise ValueError("bounded OCR scratch quantity must be positive pages")
            cost_basis = quote_ocr(
                target_id=outcome.target.id,
                engine=outcome.facts.engine,
                funding=funding,
                offering=offering,
                pages=int(quantity),
                row_pages={SCRATCH_PREVIEW_UNIT_ID: int(quantity)},
            )
        else:
            raise ValueError(
                f"bounded scratch input does not support capability {capability!r}"
            )
    if quantity is not None:
        outcome = replace(
            outcome,
            estimate_basis=replace(
                outcome.estimate_basis, quantity_hint=float(quantity)
            ),
        )
    # The resolved route facts go straight to the compiler — the former
    # hand re-projection into a 5-field ``ResolvedRouteFacts`` twin is gone
    # with the type.
    if scope_row_ids is None and bounded_scratch_input is not None:
        scope_row_ids = ()
    elif scope_row_ids is None:
        from frisket.engine.runner.validation import target_rows

        scope_row_ids = target_rows(project, dict(spec))
    from frisket.execution.scope_identity import canonical_work_scope_binding

    work_scope = (
        dict(bounded_scratch_input.work_scope)
        if bounded_scratch_input is not None
        else canonical_work_scope_binding(project, recipe, spec, scope_row_ids)
    )
    promise_set = compile_route_promises(
        outcome.facts,
        cost_basis,
        options=options,
        work_scope=work_scope,
    )
    return ResolvedExecution(
        resolution=outcome,
        persistence=persistence,
        promise_set=promise_set,
        cost_basis=cost_basis,
        offering=selected_offering,
        presentation=composition.presentation_for(
            target_id=outcome.target.id,
            capability=capability,
            engine=outcome.facts.engine,
        ),
    )


def _target_rows_declare_engine(
    targets: Sequence[Any], *, capability: str, engine: str
) -> bool:
    """Probe-free support membership for raw-vs-composed absence detection."""

    return any(
        support.capability == capability
        and (
            support.engine == engine
            or (
                support.engine.endswith("/*") and engine.startswith(support.engine[:-1])
            )
        )
        for target in targets
        for support in target.engines
    )


def _resolve_geocode_engine(
    spec: Mapping[str, Any], project: Any, *, credential_context: Any = None
) -> str:
    """geocode's own selector spells its default ``"auto"`` — a
    credential-presence SENTINEL, deliberately absent from
    ``GEOCODE_ENGINE_TABLE`` (see its docstring: declaring it there would
    resolve an unkeyed deployment onto a venue it cannot reach, or silently
    decline a configured key). Resolved HERE, before the seam is ever asked,
    via the exact check ``GeocodeRecipe._resolve_engine`` performs, so
    routing can never choose a different provider than a direct call would
    for the same configuration. An authored ``"opencage"``/``"nominatim"``
    passes through unchanged — an explicit request for a venue with no key
    still reaches the seam and refuses there (``no_live_target``), the same
    shape every other capability's missing-credential case refuses in.
    """
    authored = spec.get("engine")
    if authored and authored != "auto":
        return str(authored)
    from frisket.credentials import resolve_credential_for_use
    from frisket.execution.definitions import OPENCAGE_API_KEY_ENV

    return (
        "opencage"
        if resolve_credential_for_use(
            project, OPENCAGE_API_KEY_ENV, context=credential_context
        )
        else "nominatim"
    )


def _default_engine(capability: str) -> str:
    """The capability's default engine symbol, from its owning declaration
    (lazy: ``engine.runner.validation`` imports this module).

    ``CAPABILITY_GEOCODE`` is absent: its default is credential-presence
    SELECTED, not a static symbol, and is resolved by
    :func:`_resolve_geocode_engine` before this function is ever consulted.
    """
    if capability == CAPABILITY_OCR:
        from frisket.ops.ocr_engines import LIGHT_ENGINE

        return LIGHT_ENGINE
    if capability == CAPABILITY_TO_MARKDOWN:
        from frisket.actions.document_types import DEFAULT_DOCUMENT_ENGINE

        return DEFAULT_DOCUMENT_ENGINE
    if capability == CAPABILITY_TRANSLATE:
        # NOT ``TranslateRecipe``'s own default: that is "llm", which this
        # capability deliberately does not serve (see TRANSLATE_ENGINE_TABLE).
        # The default here is the first LOCAL build, so a translate spec that
        # names no engine resolves to the free on-box path rather than
        # defaulting a user onto a paid third-party API.
        return "opus_mt"
    if capability == CAPABILITY_CENSUS:
        return "us_census_acs"
    from frisket.sdk.ops.transcribe_engines import DEFAULT_ENGINE

    return DEFAULT_ENGINE


#: capability -> (cost-basis mint, promise compiler). The whole
#: capability-specific surface of this module, in one table: everything else
#: below — coverage, consent, the route writer, the successor writer — is
#: written against route facts and promise sets and needed no change for a
#: second capability.
_CAPABILITY_COST_BASIS: dict[str, Any] = {
    CAPABILITY_TRANSCRIBE: _transcribe_cost_basis,
    CAPABILITY_OCR: _ocr_cost_basis,
    CAPABILITY_TRANSLATE: _translate_cost_basis,
    CAPABILITY_TO_MARKDOWN: _to_markdown_cost_basis,
    CAPABILITY_GEOCODE: _geocode_cost_basis,
    CAPABILITY_CENSUS: _census_cost_basis,
}


# ---------------------------------------------------------------------------
# Promise-set wire/store identity + coverage (§5)
# ---------------------------------------------------------------------------


def promise_rows(promise_set: PromiseSet) -> list[dict[str, Any]]:
    """The canonical persisted row projection (``HASHED_PROMISE_KEYS``) —
    the exact material both the stored ``promises_json`` and the consent-
    binding set hash are built from."""
    rows: list[dict[str, Any]] = []
    for promise in promise_set.promises:
        row = promise.to_row()
        rows.append({key: row.get(key) for key in HASHED_PROMISE_KEYS})
    return rows


def consented_set_hash(promise_set: PromiseSet) -> str:
    """THE promise-set identity for 402 echo, consent rows, and the stored
    ``promise_sets.promise_set_hash`` column — the single
    ``promise_rows_hash`` construction (F3; identical to
    ``promise_set.set_hash``)."""
    return promise_rows_hash(promise_rows(promise_set))


def _bound_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def uncovered_user_claims(
    promise_set: PromiseSet,
    *,
    standing_consents: Sequence[Any],
    principal: str,
) -> list[Promise]:
    """The ``user_claim`` rows NOT covered by standing consents (§5.1/F2).

    The ``cost_under_gate_v1`` standing consent covers cost ``le`` claims
    whose bound <= the threshold RECORDED on the persisted head standing
    consent for this installation ``principal`` (F2: coverage — validation
    AND worker — reads persisted policy parameters, never the env knob; F5:
    a foreign install's standing rows never cover). ``unbounded`` cost and
    every categorical claim (egress beyond ``none``) are covered only by an
    explicit per-action consent, so their presence gates.
    """
    from frisket.engine.store.execution_routes import standing_cost_threshold

    threshold = standing_cost_threshold(list(standing_consents), principal=principal)
    uncovered: list[Promise] = []
    for promise in promise_set.promises:
        if promise.audience != "user_claim":
            continue
        if promise.field == "cost" and promise.op == "le" and threshold is not None:
            bound = _bound_decimal(promise.value)
            if bound is not None and bound <= threshold:
                continue
        uncovered.append(promise)
    return uncovered


def exact_match_consent_covers(
    project: Any,
    spec: Mapping[str, Any],
    promise_set: PromiseSet,
    *,
    consent_coverage: ConsentCoverage | None = None,
) -> bool:
    """Has this installation already consented to
    THIS exact action with THIS exact compiled claim set?

    The validation-time twin of the worker's §5.3 exact match, and the same
    three-way equality: the canonical action identity, the promise-set hash
    (ONE construction — :func:`consented_set_hash`, the digest the consent
    row records), and the actor (F5: a foreign install's consents, e.g. from
    a restored bundle, never cover). Subject-agnostic by necessity — a
    re-run is a NEW run id, so a subject-scoped read could never see the
    consent the user already gave.

    Categorical consent is never IMPLIED across identities: a different
    action, a different venue, or a different claim set all shift one of the
    three components and re-ask. What this ends is the re-asking of an
    IDENTICAL consented action, which left the hardened standing-threshold
    machinery with nothing to decide.

    A match here is not merely a pass: the caller RECORDS the derivation as
    this run's own consent row (``CONSENT_BASIS_EXACT_MATCH``, written by
    :func:`record_resolved_execution` via the validation gate). The worker's
    §5.3 fence stays subject-scoped — its locality is the feature — and an
    admitted run therefore always carries the artifact that fence reads.
    """
    from frisket.engine.store.execution_routes import (
        ConsentRegistry,
    )

    identity = action_identity_hash(spec)
    set_hash = consented_set_hash(promise_set)
    principal = effective_consent_coverage(project, consent_coverage).principal
    return any(
        consent.promise_set_hash == set_hash and consent.actor == principal
        for consent in ConsentRegistry(project).action_consents(identity)
    )


# The claim ops the coverage ENVELOPE owns (§5.3 monotone quantitative rows);
# every other op is categorical/interacting and is covered only by an exact
# consent — whole-set, or the verbatim row recurrence below.
_MONOTONE_CLAIM_OPS = frozenset({"le", "unbounded"})


class PersistedClaimCoverage:
    """What a subject's PERSISTED consent artifacts already cover, asked one
    claim at a time — THE composition both the validation gate and the
    worker's effect-site fence use, so they can never disagree.

    Two independent covers, both fail-closed:

    - the consented high-water ENVELOPE (§5.3), for monotone quantitative
      rows only (``claim_covered_by_envelope``);
    - VERBATIM recurrence: the identical promise ROW (field+op+value+basis+
      order_ref, the one ``promise_row_fingerprint`` construction) already
      appears in a promise set this installation consented to FOR THIS
      ACTION IDENTITY. This is what covers CATEGORICAL claims across a
      scope change, and it is the narrowest rule that can: no implication,
      no widening, no cross-identity carry — the exact claim the user was
      shown and accepted, re-offered unchanged.

      Without it the standing cost threshold is structurally unreachable on
      a resume: every priced venue compiles cost TOGETHER WITH a categorical
      egress claim, and any cost change breaks the whole-set exact match, so
      egress would re-gate no matter what the threshold said — the "test
      passes for the wrong reason" shape. With it, a
      backfill asks about exactly what CHANGED.

    Both covers are folded from ONE walk of the subject's promise-set chain
    (see :func:`persisted_claim_coverage`) — the envelope and the fingerprint
    set read exactly the same rows, so they cannot be built from different
    views of the chain, and asking both costs one pass, not two.
    """

    def __init__(
        self, envelope: dict[str, Any], consented_fingerprints: frozenset[str]
    ) -> None:
        self.envelope = envelope
        self._consented_fingerprints = consented_fingerprints

    def covers(self, promise: Promise) -> bool:
        from frisket.execution.promises import (
            claim_covered_by_envelope,
            promise_row_fingerprint,
        )

        if claim_covered_by_envelope(self.envelope, promise):
            return True
        if promise.op in _MONOTONE_CLAIM_OPS:
            # The envelope is the sole authority for monotone rows, and it
            # answered. Verbatim recurrence could only ever agree with it
            # here (an identical ``le`` row is a consented bound equal to the
            # candidate, which the envelope already covers), so consulting it
            # would buy nothing.
            return False
        return promise_row_fingerprint(promise.to_row()) in self._consented_fingerprints


def persisted_claim_coverage(
    store: Any,
    consents: Any,
    *,
    expected_identity: str,
    principal: str,
) -> PersistedClaimCoverage:
    """Build the coverage view for one subject's chain, from ONE walk.

    The contributing sets are exactly those this installation consented to
    FOR THIS ACTION IDENTITY (F5: a foreign actor's consents — the restored-
    bundle scene — never contribute). Their rows fold into both covers at
    once: the monotone high-water envelope and the verbatim fingerprint set.

    (The envelope previously came from a per-append
    ``promise_sets.coverage_envelope_json`` cache, read when every persisted
    consent happened to carry this principal and identity. It was deleted: the
    one reachable caller always has a CATEGORICAL egress claim in hand — no
    venue compiles a cost user-claim without one — which routes straight to
    the fingerprint set and walks the chain anyway, two lines after the cache
    "avoided" it. Recomputing is the same answer, from one walk instead of
    two.)
    """
    from frisket.execution.promises import (
        compute_coverage_envelope,
        promise_row_fingerprint,
    )

    consented_hashes = {
        consent.promise_set_hash
        for consent in consents
        if consent.action_identity_hash == expected_identity
        and consent.actor == principal
        and consent.promise_set_hash is not None
    }
    history: list[PromiseSet] = []
    fingerprints: set[str] = set()
    for stored in store.promise_sets():
        if stored.promise_set_hash not in consented_hashes:
            continue
        history.append(
            PromiseSet.make(tuple(Promise.from_row(row) for row in stored.promises))
        )
        fingerprints.update(promise_row_fingerprint(row) for row in stored.promises)
    return PersistedClaimCoverage(
        compute_coverage_envelope(history), frozenset(fingerprints)
    )


def project_uncovered_user_claims(
    project: Any,
    promise_set: PromiseSet,
    *,
    spec: Mapping[str, Any] | None = None,
    run_id: int | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> list[Promise]:
    """Claims not covered by this actor's exact consent or per-run preference.

    A known quote within the preference covers the cost and its provider call.
    Validation materializes that decision as an exact scope/quote-bound consent;
    workers verify that proof and never apply a saved threshold to changed work.
    Resumes may additionally reuse this actor's own persisted claim envelope.
    """
    from frisket.engine.store.execution_routes import RouteStore

    effective = effective_consent_coverage(project, consent_coverage)
    if spec is not None and exact_match_consent_covers(
        project, spec, promise_set, consent_coverage=effective
    ):
        return []
    principal = effective.principal
    claims = [p for p in promise_set.promises if p.audience == "user_claim"]
    quote_json = next(
        (
            (claim.basis or {})["consent_quote_json"]
            for claim in claims
            if (claim.basis or {}).get("consent_quote_json") is not None
        ),
        None,
    )
    if quote_json is not None:
        from frisket.engine.runner.confirmation_context import quoted_usd

        # The rated retail quote is what the user will pay. It governs
        # preapproval; neutral provider cost bounds retain their own rates.
        quoted = quoted_usd(json.loads(quote_json))
        preapproved = (
            quoted is not None and Decimal(str(quoted)) <= effective.threshold_usd
        )
    else:
        # Before a quote is attached, only known bounded costs can be covered.
        costs = [p for p in claims if p.field == "cost"]
        preapproved = bool(costs) and all(
            p.op == "le"
            and (bound := _bound_decimal(p.value)) is not None
            and bound <= effective.threshold_usd
            for p in costs
        )
    uncovered = [
        p for p in claims if not (preapproved and p.field in {"cost", "egress_class"})
    ]
    if not uncovered or spec is None or run_id is None:
        return uncovered
    store = RouteStore.for_run(project, run_id)
    if store.head() is None:
        return uncovered  # no chain yet: nothing persisted can cover anything
    coverage = persisted_claim_coverage(
        store,
        store.consents(),
        expected_identity=action_identity_hash(spec),
        principal=principal,
    )
    return [claim for claim in uncovered if not coverage.covers(claim)]


def gate_claims_payload(
    promises: Sequence[Promise],
    facts: RouteRowFacts,
    presentation: CommercialPresentation | None = None,
    *,
    has_cost_claim: bool,
) -> list[dict[str, str]]:
    """[{field, display}] rows for the 402 payload (§5.2). ``facts`` is the
    resolution's route facts — every caller passes ``resolution.facts``, so
    the former ``ResolvedRouteFacts | Any`` union collapsed to one type.

    ``has_cost_claim`` comes from the complete compiled promise set, not just
    the uncovered subset.  It prevents an egress-only free-public claim from
    inventing billing copy while retaining the billing label when a cost row
    was merely covered by a standing consent.
    """
    return [
        {
            "field": promise.field,
            "display": claim_display(
                promise,
                facts,
                presentation,
                show_billing=has_cost_claim,
            ),
        }
        for promise in promises
    ]


# ---------------------------------------------------------------------------
# The sole route writer (§4.1)
# ---------------------------------------------------------------------------


def target_snapshot(resolution: Resolution) -> dict[str, Any]:
    """The complete persisted dispatch snapshot."""
    support = resolution.support
    return validated_target_snapshot(
        {
            "target_id": resolution.target.id,
            "capability": support.capability,
            "transport": support.transport,
            "run_scoped": support.run_scoped,
        }
    )


def record_resolved_execution(
    project: Any,
    run_id: int | None,
    resolved: ResolvedExecution,
    *,
    spec: Mapping[str, Any],
    consent_required: bool,
    consent_basis: str | None = None,
    txn: Any | None = None,
    receipt_id: str | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> tuple[Any, Any]:
    """Persist the invocation's route artifacts for its run or receipt.

    Writes, via ``RouteStore`` (inside ``txn`` when supplied, so the caller
    can bind this to the run-creation transaction):

    - a consent row IFF ``consent_required`` (a consent event happened:
      uncovered claims were shown and confirmed, or the gate DERIVED
      coverage from an identical already-consented action — ``consent_basis``
      records which) — O1's rent bound: a local free run, whose claims are
      covered by nothing because it makes none, still writes NO consent row;
    - the promise set (consent-linked when one was recorded);
    - the route row with the resolved facts + target snapshot.

    Type-refuses ephemeral resolution objects. Accounted previews require
    a durable resolution even though their output values are ephemeral.
    """
    if not isinstance(resolved, ResolvedExecution):
        raise TypeError(
            f"route writer requires a ResolvedExecution, got {type(resolved).__name__}"
        )
    if resolved.persistence != "durable":
        raise TypeError(
            "route writer refuses ephemeral ResolvedExecution objects — "
            "use a durable resolution for an accounted invocation"
        )
    # (The "carries no compiled promise set" guard retired at E-6: the field
    # is required and typed on ResolvedExecution, so the writer no longer
    # re-checks at write time what the constructor now cannot omit. The two
    # remaining guards stay — they answer different questions: the argument
    # TYPE, and the ephemeral persistence POLICY, which is the Persistence
    # discriminator's job, not the type's.)
    from frisket.engine.store.execution_routes import (
        CONSENT_BASIS_CONFIRMED,
        RouteStore,
    )

    from frisket.execution.attempt import _attempt_owner

    _attempt_owner(run_id, receipt_id)
    store = (
        RouteStore.for_receipt(project, receipt_id)
        if receipt_id is not None
        else RouteStore.for_run(project, run_id)
    )
    # The route row's authored options are the CAPABILITY's option set; the
    # capability comes off the selected support row rather than off the spec,
    # so what is recorded is what was resolved.
    capability = resolved.resolution.support.capability
    # The instance principal may mint (and commit) project meta on first
    # use — resolve it BEFORE opening the write transaction.
    resolved_actor = (
        effective_consent_coverage(project, consent_coverage).principal
        if consent_required
        else None
    )

    def _write(connection: Any) -> tuple[Any, Any]:
        consent_id: str | None = None
        if consent_required:
            consent = store.record_consent(
                action_identity_hash=action_identity_hash(spec),
                promise_set_hash=consented_set_hash(resolved.promise_set),
                actor=resolved_actor or "deployment:unknown",
                grant_basis=consent_basis or CONSENT_BASIS_CONFIRMED,
                txn=connection,
            )
            consent_id = consent.id
        promise_set_row = store.append_promise_set(
            promises=promise_rows(resolved.promise_set),
            predecessor_id=None,
            consent_id=consent_id,
            txn=connection,
        )
        facts = resolved.resolution.facts
        route_row = store.append_route(
            promise_set_id=promise_set_row.id,
            engine=facts.engine,
            options=authored_options(spec, capability),
            target_snapshot=target_snapshot(resolved.resolution),
            operator=facts.operator,
            egress_class=facts.egress_class,
            region=facts.region,
            credential_source=facts.credential_source,
            cost_posture=facts.cost_posture,
            predecessor_id=None,
            txn=connection,
        )
        return promise_set_row, route_row

    if txn is not None:
        # Caller-owned transaction (e.g. an atomic prepare savepoint): the
        # caller owns atomicity and commit.
        return _write(txn)
    # Owned mode: consent + promise set + route land in ONE project-store
    # transaction (§4.1) — never observable partially.
    db = project.db
    if db.in_transaction:
        return _write(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        result = _write(db)
    except BaseException:
        db.rollback()
        raise
    db.commit()
    return result


# ---------------------------------------------------------------------------
# A confirm that changed the claims
# ---------------------------------------------------------------------------


class LiveAttemptConflict(RuntimeError):
    """This run has an ``execution_attempts`` row in state ``dispatching``.

    This serialization constraint is asserted here so it is asserted
    on EVERY consent-advancing path rather than only on reconsent's. A
    dispatch already authorized by that attempt is running against the head
    this call is about to advance, so nothing may be minted: the caller rolls
    back and re-asks once the attempt terminates.
    """

    def __init__(self, attempt_id: str) -> None:
        super().__init__(
            f"execution attempt {attempt_id} is dispatching for this run; "
            "its head cannot be advanced until it terminates"
        )
        self.attempt_id = attempt_id


@dataclass(frozen=True)
class ConfirmedClaims:
    """What one confirmation minted: the consent the user gave, the successor
    promise set it authorizes, and the successor route pointing at that set.

    (§3.1 names the return ``ConsentRow``; reconsent additionally needs the
    successor SET id — it is the resume job's dedupe key and the accepted
    response's ``promise_set_id`` — so the three rows the one transaction
    writes are returned together rather than re-read afterwards.)
    """

    consent: Any
    promise_set: Any
    route: Any


def confirm_changed_claims(
    project: Any,
    run_id: int,
    spec: Mapping[str, Any],
    resolved: ResolvedExecution,
    *,
    actor: str,
    head_route_id: str,
    head_promise_set_id: str,
    txn: Any | None = None,
) -> ConfirmedClaims:
    """Bind a confirmation of CHANGED claims to run ``run_id`` (§3.1).

    THE one construction that can advance a run's consent chain: the
    backfill-confirm branch of ``validate_spec`` (a resume that re-gated on
    claims its run had not consented to, whether because it extends scope or
    because the run halted ``promise_violation`` on changed terms). Before
    Previously a confirmed over-threshold backfill persisted nothing and the worker
    then admitted against the OLD head — the user consented to claims the
    system never bound.

    In one transaction (the caller's when ``txn`` is supplied, else an owned
    ``BEGIN IMMEDIATE``):

    - assert no LIVE attempt (§1.3), FIRST, so nothing is minted under a
      dispatch already authorized against the head this call advances;
    - the consent row (canonical action identity + the confirmed set hash);
    - the successor promise set, ``predecessor_id`` = the head the caller
      GATED against — the CAS is what makes a concurrent confirm safe
      (``ChainConflictError``), so the head is an argument and never re-read
      here;
    - the successor route referencing that set, ``predecessor_id`` = the head
      route.

    The route it appends is an ordinary ``append_route``: a backfill
    confirm can legitimately compile claims IDENTICAL to the persisted
    head's, so reusing an equivalent existing route is correct here rather
    than a conflict.
    """
    if not isinstance(resolved, ResolvedExecution):
        raise TypeError(
            f"claim confirmation requires a ResolvedExecution, got "
            f"{type(resolved).__name__}"
        )
    if resolved.persistence != "durable":
        raise TypeError(
            "claim confirmation refuses ephemeral ResolvedExecution objects — "
            "a binding consent is never compiled from an ephemeral preview"
        )
    from frisket.engine.store.execution_routes import RouteStore

    store = RouteStore.for_run(project, run_id)
    facts = resolved.resolution.facts
    capability = resolved.resolution.support.capability

    def _write(connection: Any) -> ConfirmedClaims:
        # §1.3, FIRST in the lock: a 'dispatching' attempt owns the current
        # head. Vacuous on the resume-confirm path today (the mint happens
        # later, inside MapRunner.run), asserted forever.
        #
        # Asked of ``execution_attempts`` DIRECTLY, never through
        # ``runs.current_attempt_id``. That pointer is a convenience for
        # stamping facts during dispatch, not an authority on liveness: it
        # holds ONE id, so with two live attempts (the pre-fix claim race) the
        # later one terminating made this predicate read None while the
        # earlier was still egressing — the POST returned 200 and a worker
        # started on the newly-consented head while another ran under the
        # route the user had just revoked. The supersede sweep only covers
        # ('created','admitted'), so it could not fence that retroactively.
        # The attempt row's own state cannot lie about itself.
        live = connection.execute(
            "SELECT id FROM execution_attempts "
            "WHERE run_id=? AND state='dispatching' ORDER BY seq LIMIT 1",
            (run_id,),
        ).fetchone()
        if live is not None:
            raise LiveAttemptConflict(str(live["id"]))
        consent = store.record_consent(
            action_identity_hash=action_identity_hash(spec),
            promise_set_hash=consented_set_hash(resolved.promise_set),
            actor=actor,
            txn=connection,
        )
        successor_set = store.append_promise_set(
            promises=promise_rows(resolved.promise_set),
            predecessor_id=head_promise_set_id,
            consent_id=consent.id,
            txn=connection,
        )
        route = store.append_route(
            promise_set_id=successor_set.id,
            engine=facts.engine,
            options=authored_options(spec, capability),
            target_snapshot=target_snapshot(resolved.resolution),
            operator=facts.operator,
            egress_class=facts.egress_class,
            region=facts.region,
            credential_source=facts.credential_source,
            cost_posture=facts.cost_posture,
            predecessor_id=head_route_id,
            txn=connection,
        )
        return ConfirmedClaims(consent=consent, promise_set=successor_set, route=route)

    if txn is not None:
        return _write(txn)
    db = project.db
    if db.in_transaction:
        return _write(db)
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # Lock contention is a CAS loss by another name: a concurrent writer
        # (a dispatch claim, a sibling confirm) holds the write lock and
        # nothing here was written. Answer in the chain-conflict vocabulary
        # callers already map to a typed 409 (``reconsent_conflict``) rather
        # than letting a bare sqlite3.OperationalError escape as a 500.
        from frisket.engine.store.execution_routes import ChainConflictError

        raise ChainConflictError(
            "another writer holds this project's write lock; the confirmed "
            f"claims for run {run_id} were not bound — re-ask to re-read the "
            "current head"
        ) from exc
    try:
        result = _write(db)
    except BaseException:
        db.rollback()
        raise
    db.commit()
    return result
