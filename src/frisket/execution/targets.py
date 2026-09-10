"""Execution-target seam types.

The static resolver, route/promise persistence, and route-computed labels all
key on ``ExecutionTarget`` and ``TargetEngineSupport``.

Targets are non-secret: connection secrets re-deref at dispatch
through the credential env name a definition names directly; nothing here
may ever hold a URL, token, or key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Union, get_args

from pydantic import BaseModel, ConfigDict

from frisket.contracts.actions.schemas._engines import (
    CensusTransport,
    GeocodeTransport,
    OcrTransport,
    ToMarkdownTransport,
    TranscriptionTransport,
    TranslateTransport,
)
from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport

# The capability vocabulary is the SAME vocabulary the model-call
# fact's ``capability`` column already carries ("transcribe", "ocr") and the
# same one ``RunResultStore._ROUTED_FACT_CAPABILITIES`` keys the epoch
# invariant on — one word per capability across the seam, never a second
# spelling. A target row declares which capability each of its engine rows
# serves, so "which target can run this" is a declaration lookup rather than
# an assumption that every engine id in the tree is a transcription engine.
CAPABILITY_TRANSCRIBE = "transcribe"
CAPABILITY_OCR = "ocr"
#: Each names the work, not the venue or the unit: the token is
#: what a receipt says the user's data was sent off to have done to it, so
#: geocoding an address and fetching ACS demographics for a point are two
#: capabilities even though both meter rows through one producer. Collapsing
#: them onto one row-shaped token would make a receipt unable to say which
#: external API the addresses went to, which is the question the product
#: exists to answer.
#:
#: **The VALUES are not chosen here.** Each is the spelling the model-call
#: fact's ``capability`` column already carries for that work — read off the
#: producing call sites (``sdk/ops/translate.py``, ``sdk/ops/to_markdown.py``
#: and ``ops/integrations/datalab.py``, ``sdk/ops/geocode.py``,
#: ``sdk/ops/census_demographics.py``), not invented to match the recipe or
#: module name. That is the rule stated at the top of this block, applied:
#: ``document.convert`` and ``census_demographics`` read oddly next to the
#: constant names, and minting the tidier ``to_markdown``/``census`` would have
#: put a SECOND spelling on facts already persisted under the first — the
#: same-name-different-meaning failure, in the column
#: ``RunResultStore._ROUTED_FACT_CAPABILITIES`` and a downstream composition's
#: settlement allowlist both key on.
CAPABILITY_TRANSLATE = "translate"
CAPABILITY_TO_MARKDOWN = "document.convert"
CAPABILITY_GEOCODE = "geocode"
CAPABILITY_CENSUS = "census_demographics"
EXECUTION_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_TRANSCRIBE,
        CAPABILITY_OCR,
        CAPABILITY_TRANSLATE,
        CAPABILITY_TO_MARKDOWN,
        CAPABILITY_GEOCODE,
        CAPABILITY_CENSUS,
    }
)

# Closed enum vocabularies persisted on route facts.
EgressClass = Literal[
    "none",
    "operator_lan",
    "frisket_dedicated_org",
    "frisket_shared",
    "third_party_api",
]
# CostPosture is deliberately NOT a field of
# ExecutionTarget: posture is a route-level fact, computed at resolution from
# (target, credential decision) and recorded on the route — O2 and O3 share
# target rows and differ only in resolved posture.
CostPosture = Literal["operator_borne", "platform_metered", "org_key"]

# Target-id family prefix — one spelling. This was declared independently in
# definitions, the resolver, and the price book, which is the "same name,
# different meaning" shape one typo away from a target family silently pricing
# as another. It lives in the target vocabulary home, which every consumer can
# import without inverting the definitions -> resolver -> price-book order.
REMOTE_API_TARGET_ID_PREFIX = "remote-api:"
#: The hosted OCR venue. Its own family — one target, no instance label —
#: so it is a whole id rather than a prefix. It lives here for the same reason
#: the prefix does: the price book classifies venues by it and must not
#: invert the definitions -> resolver -> price-book import order, and a second
#: spelling is one typo away from a venue pricing as another.
DATALAB_TARGET_ID = "datalab"
#: The third-party venues, each its own family for the same reason
#: Datalab is: the price book classifies a venue by its id, one target per
#: provider, and a second spelling is one typo away from a venue pricing as
#: another. ``DATALAB_TARGET_ID`` is reused rather than duplicated — Datalab's
#: /convert endpoint serves BOTH the OCR and the to_markdown capabilities, and
#: a target is a venue, not a kind of work.
DEEPL_TARGET_ID = "deepl"
GOOGLE_TRANSLATE_TARGET_ID = "google-translate"
OPENCAGE_TARGET_ID = "opencage"
NOMINATIM_TARGET_ID = "nominatim"
US_CENSUS_TARGET_ID = "us-census"

_VALID_EGRESS_CLASSES: frozenset[str] = frozenset(get_args(EgressClass))
# Every wire a target can speak, across capabilities. Each family keeps its own
# Literal at the roster; this union is the ANNOTATION, because ``transport`` is
# a TARGET-row field and one target can speak several (the models gateway
# serves /v1/transcribe, /ocr AND /to-markdown).
Transport = Union[
    TranscriptionTransport,
    OcrTransport,
    ToMarkdownTransport,
    TranslateTransport,
    GeocodeTransport,
    CensusTransport,
]

#: capability -> the wires THAT capability's engines may be spoken to over.
#:
#: The validation authority, and per capability rather than a global union on
#: purpose. A flat "is this string any known transport" check accepts
#: ``transport="deepl.v2"`` on a transcription row — a declaration error the
#: contract layer is supposed to catch at construction, which instead survives
#: to dispatch and surfaces as ``runtime_binding``'s unknown-transport raise
#: with a route already persisted. The union above widened that hole from two
#: families to six, so the check moved to the effect site: a row is validated
#: against the wire set of the capability it declares, and nothing else.
CAPABILITY_TRANSPORTS: dict[str, frozenset[str]] = {
    CAPABILITY_TRANSCRIBE: frozenset(get_args(TranscriptionTransport)),
    CAPABILITY_OCR: frozenset(get_args(OcrTransport)),
    CAPABILITY_TO_MARKDOWN: frozenset(get_args(ToMarkdownTransport)),
    CAPABILITY_TRANSLATE: frozenset(get_args(TranslateTransport)),
    CAPABILITY_GEOCODE: frozenset(get_args(GeocodeTransport)),
    CAPABILITY_CENSUS: frozenset(get_args(CensusTransport)),
}


class OcrOptionSupport(BaseModel):
    """Per-(engine, target) OCR option support.

    Unlike its transcription sibling this type mirrors NO wire contract: OCR
    has no ``/ocr`` support schema to be faithful to, so this is an app-side
    declaration of what one build actually honors, and every field is here
    because some declared target answers it differently:

    - ``language``: the recognition-language hint. RapidOCR selects a
      recognition model with it and a VLM takes it as a prompt hint; the
      gateway's settled ``/ocr`` contract carries no language field at all
      (the sidecar parsers auto-detect script) and Datalab's ``/convert`` takes none
      either. Authoring one against those targets used to be a SILENT drop —
      the exact bug class the one ability checker exists to kill.
    - ``geometry``: whether the engine returns per-block bounding boxes. It is
      what makes ``searchable_pdf`` composable: a VLM returns text only, so an
      authored searchable_pdf there would paint an empty layer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: bool = False
    geometry: bool = False


class TranslateOptionSupport(BaseModel):
    """Per-(engine, target) translate option support.

    Both fields describe the SOURCE language hint, and they are two questions
    rather than one because the four engines answer them in three different
    combinations:

    - ``source_language``: whether an authored source-language hint crosses the
      boundary at all. ``hy_mt2`` is auto-only — its prompt names the target
      language and nothing else — so a hint authored against it would be a
      silent drop, the bug class the ability checker exists to kill.
    - ``auto_detect_source``: whether the engine can run with NO source hint.
      ``opus_mt`` cannot: it loads a per-language-PAIR CTranslate2 artifact, so
      without a source there is no model to pick. DeepL and Google both detect.

    Neither field can be derived from the other: hy_mt2 is (False, True) and
    opus_mt is (True, False).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_language: bool = False
    auto_detect_source: bool = False


class ToMarkdownOptionSupport(BaseModel):
    """Per-(engine, target) document-conversion option support.

    It declares NO option fields, and that absence is the accurate
    declaration rather than an unfinished one: ``MediaToMarkdownParams``
    carries ``engine``, ``output_name``, ``input_columns`` and ``row_ids``, and
    none of those is a knob a venue can honestly refuse — ``engine`` IS the
    venue selector, and the other three are app-side output/scope intents every
    target sees identically.

    The type still does load-bearing work: ``TargetEngineSupport`` isinstance-
    checks the options object against the row's declared capability, so a
    to_markdown row carrying an OCR or transcription support object refuses at
    declaration time. A field belongs here the release a params knob appears
    that one venue serves and another does not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class GeocodeOptionSupport(BaseModel):
    """Per-(engine, target) geocode option support.

    ``structured_query`` is the one authored shape the two providers answer
    differently: ``input_template`` composes a free-text query both accept,
    while OpenCage additionally honours a country/bounds-scoped lookup that
    Nominatim's ``/search`` (called here with ``q`` + ``format=jsonv2`` only)
    does not. Declared FALSE for Nominatim so a scoped lookup refuses there
    instead of quietly widening to a whole-world free-text match, which returns
    a confident wrong point rather than an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    structured_query: bool = False


class CensusOptionSupport(BaseModel):
    """Per-(engine, target) census-enrichment option support.

    No option fields, for the reason ``ToMarkdownOptionSupport`` has none and
    one more besides: the capability has exactly ONE venue, so there is no
    second declaration for a knob to differ from. ``geography`` and
    ``include_moe`` are authored options, but they select WHICH ACS variables
    the one venue is asked for — the venue serves both levels and both
    shapes — so neither is refusable and neither belongs here or in
    ``_CAPABILITY_OPTION_KEYS``. A second demographics venue is what would
    populate this type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


#: The option-support declaration for one engine row, per capability. The
#: field used to be hard-typed to the transcription mirror (the spec's named
#: generalization point, §5): a union keeps it TYPED — a garbage object still
#: refuses at construction — while letting a second capability declare its own
#: option vocabulary instead of borrowing diarization/vad/sizes.
OptionSupport = Union[
    TranscriptionOptionSupport,
    OcrOptionSupport,
    TranslateOptionSupport,
    ToMarkdownOptionSupport,
    GeocodeOptionSupport,
    CensusOptionSupport,
]

#: Which option-support type each capability declares. One table, consulted by
#: ``TargetEngineSupport.__post_init__`` (so an OCR row carrying a
#: transcription support object refuses at declaration time, not at dispatch)
#: and by the resolver's ability checker.
CAPABILITY_OPTION_SUPPORT: dict[str, type] = {
    CAPABILITY_TRANSCRIBE: TranscriptionOptionSupport,
    CAPABILITY_OCR: OcrOptionSupport,
    CAPABILITY_TRANSLATE: TranslateOptionSupport,
    CAPABILITY_TO_MARKDOWN: ToMarkdownOptionSupport,
    CAPABILITY_GEOCODE: GeocodeOptionSupport,
    CAPABILITY_CENSUS: CensusOptionSupport,
}

# (The categorical-implication lattice used to be declared twice: here, as
# ``_EGRESS_CHAIN_RANK`` + ``egress_at_most_as_open``, and again as the
# ``egress.v1`` OrderTable in ``promises.builtin_registry()``. This copy had
# zero callers and was deleted — the registry's table is the one
# construction, and every egress implication question goes through it.)


def require_clean(name: str, value: str) -> None:
    """THE shared "non-empty, whitespace-clean string" field validator.

    Lives here (the lowest execution-seam layer) so the route-fact and
    promise-compiler types validate identically — the same rule the target
    types above spell out inline."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, whitespace-clean string")


def require_egress_class(value: str) -> None:
    """Refuse an egress class outside the declared vocabulary (§0 posture:
    a garbage class must halt loudly, never flow into policy comparisons)."""
    if value not in _VALID_EGRESS_CLASSES:
        raise ValueError(f"unknown egress class: {value!r}")


TARGET_SNAPSHOT_KEYS = frozenset({"target_id", "capability", "transport", "run_scoped"})


def validated_target_snapshot(value: object) -> dict[str, object]:
    """Validate the complete persisted dispatch snapshot, without defaults."""

    if not isinstance(value, Mapping):
        raise ValueError("target snapshot must be an object")
    if set(value) != TARGET_SNAPSHOT_KEYS:
        raise ValueError(
            "target snapshot must contain exactly "
            "target_id, capability, transport, and run_scoped"
        )
    target_id = value["target_id"]
    capability = value["capability"]
    transport = value["transport"]
    run_scoped = value["run_scoped"]
    require_clean("target_id", target_id)
    require_clean("capability", capability)
    require_clean("transport", transport)
    if capability not in EXECUTION_CAPABILITIES:
        raise ValueError(f"unknown execution capability: {capability!r}")
    if transport not in CAPABILITY_TRANSPORTS[capability]:
        raise ValueError(f"transport {transport!r} is not a {capability!r} wire")
    if not isinstance(run_scoped, bool):
        raise ValueError("run_scoped must be a bool")
    return {
        "target_id": target_id,
        "capability": capability,
        "transport": transport,
        "run_scoped": run_scoped,
    }


@dataclass(frozen=True)
class TargetEngineSupport:
    """Per-(engine, target) support declaration.

    ``transport`` lives here — not on the engine — because one engine can
    speak different wires on different targets (fixes A3). ``options`` is the
    capability's option-support declaration: for transcription the app-side
    mirrored copy of the sidecar's wire-contract support type (a faithful
    mirror that must never diverge from its source); for OCR the app-side
    :class:`OcrOptionSupport`.

    ``capability`` is required and keyword-only. A target is a venue, not
    a capability — the same ``local`` box runs faster_whisper and rapidocr,
    the same gateway serves /v1/transcribe and /ocr — so the capability a row
    serves is a per-row declaration. Defaulting it to "transcribe" would have
    made every future capability's rows silently claim to be transcription
    rows, which is the failure this field exists to prevent.
    """

    engine: str
    transport: Transport
    options: OptionSupport
    capability: str = field(kw_only=True)
    # Per-target model-size support: faster_whisper authors
    # model_size, and per-target `sizes` declares which are served. This field
    # lives HERE, not on the mirrored TranscriptionOptionSupport — that
    # type mirrors the sidecar wire contract verbatim and gains fields only
    # when the sidecar source does.
    sizes: tuple[str, ...] = ()
    run_scoped: bool = False  # adapter resource behavior

    def __post_init__(self) -> None:
        if (
            not self.engine
            or not self.engine.strip()
            or self.engine != self.engine.strip()
        ):
            raise ValueError("engine must be a non-empty, whitespace-clean string")
        if self.capability not in EXECUTION_CAPABILITIES:
            raise ValueError(f"unknown execution capability: {self.capability!r}")
        # Against THIS capability's wire set, never a global union: a
        # transcription row carrying an OCR or translate wire is a declaration
        # error, and catching it here is the difference between a refusal at
        # roster construction and an unknown-transport raise at dispatch with
        # the route already written.
        wires = CAPABILITY_TRANSPORTS[self.capability]
        if self.transport not in wires:
            raise ValueError(
                f"transport {self.transport!r} is not a "
                f"{self.capability!r} wire (it speaks: "
                f"{', '.join(sorted(wires))})"
            )
        expected = CAPABILITY_OPTION_SUPPORT[self.capability]
        if not isinstance(self.options, expected):
            raise ValueError(
                f"options for capability {self.capability!r} must be a "
                f"{expected.__name__}"
            )
        if any(
            not isinstance(size, str) or not size.strip() or size != size.strip()
            for size in self.sizes
        ):
            raise ValueError(
                "sizes entries must be non-empty, whitespace-clean strings"
            )
        if len(set(self.sizes)) != len(self.sizes):
            raise ValueError("sizes entries must be unique")
        if not isinstance(self.run_scoped, bool):
            raise ValueError("run_scoped must be a bool")


@dataclass(frozen=True)
class ExecutionTarget:
    """One execution venue: a place work can run, owned by an operator.

    Secrets themselves never appear on a
    target; a definition names its credential env directly at dispatch.

    Cost posture is deliberately NOT a field of this type: posture is a
    route-level fact computed at resolution from (target, credential
    decision) and recorded on the route.

    ``region`` follows the honest-absence rule: ``None`` (not pinned) or a
    real value — never an empty string standing in for "unknown".
    """

    id: str
    operator: str  # "self" | "frisket" | named third party
    egress_class: EgressClass
    region: str | None = None  # recorded only when truly pinned
    engines: tuple[TargetEngineSupport, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip() or self.id != self.id.strip():
            raise ValueError("target id must be a non-empty, whitespace-clean string")
        if not self.operator or self.operator != self.operator.strip():
            raise ValueError(
                "target operator must be a non-empty, whitespace-clean string"
            )
        if self.egress_class not in _VALID_EGRESS_CLASSES:
            raise ValueError(f"unknown egress class: {self.egress_class!r}")
        if self.region is not None and (
            not isinstance(self.region, str) or not self.region.strip()
        ):
            raise ValueError(
                "region must be None or a real value, never an empty string"
            )
        # Uniqueness is per (capability, engine), not per engine: one engine
        # ID can legitimately name two different engines in two families
        # ('datalab' is a hosted OCR engine AND a hosted document-conversion
        # one), and a venue may serve both.
        seen: set[tuple[str, str]] = set()
        for support in self.engines:
            if not isinstance(support, TargetEngineSupport):
                raise ValueError("engines entries must be TargetEngineSupport")
            key = (support.capability, support.engine)
            if key in seen:
                raise ValueError(
                    f"duplicate engine entry on target {self.id!r}: "
                    f"{support.capability}/{support.engine!r}"
                )
            seen.add(key)
