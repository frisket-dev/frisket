"""Per-family engine declaration tables — the single roster source.

One table per drifted media family (ocr, transcribe, to_markdown). Every
other surface DERIVES from these tables instead of restating them:

- the ops module's engine constants (``LOCAL_ENGINES``/``SIDECAR_ENGINES``/
  ``ENGINE_ALIASES``) — dispatch still lives in the recipe;
- the contract symbolic sets (``OCR_SYMBOLIC_ENGINES`` et al., media.py);
- the catalog hints roster labels/tiers (server/action_catalog_hints.py);
- the receipt provider maps (sdk/ops/{ocr,to_markdown,transcribe}.py);
- the compare-preview engine descriptors and comparability gates.

Deliberately dependency-light (stdlib only), the ``_language.py`` precedent:
the contract layer, the ops recipes, the catalog projection, and the compare
previews can all import it without heavy runtime deps — and without the
ops<->contracts cycle a table inside an ops module would force (ops modules
import ``frisket.contracts.action``).

Tier vocabulary (three visible tiers):

- ``local``   — runs in this process/box (sandboxed worker or system binary);
- ``sidecar`` — the operator's frisket-models service, which MAY be another
  machine: never labelled "local";
- ``hosted``  — leaves the operator's infrastructure (provider API,
  vendor pay-per-call).

``ner`` keeps its existing pinned roster (it never drifted); it is not
declared here.

``translate``, ``geocode`` and ``census`` gained tables when the execution
seam did: ``execution.resolver.capability_engine_table`` canonicalizes an
authored engine symbol through the CAPABILITY's table, so a capability with
no table here cannot resolve to a venue at all. Their tables are the seam
roster, not a second engine axis — ``TRANSLATE_ENGINES`` (maps.py) stays the
contract's authoritative set and a parity test pins the two together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EngineTier = Literal["local", "sidecar", "hosted"]
TranscriptionTransport = Literal[
    "local",
    "remote",
    "frisket.transcription.v1",
]
#: The wires an OCR engine is spoken to over. A separate Literal from
#: ``TranscriptionTransport`` on purpose: they overlap on ``local`` and
#: ``remote`` but a transcription engine can no more speak ``sidecar.ocr``
#: than an OCR engine can speak ``frisket.transcription.v1``, and the
#: execution seam takes their UNION (``execution.targets.Transport``) because
#: transport is a target-row field.
#:
#: - ``local``            — in-process/sandboxed subprocess on this box
#:   (rapidocr's pool, the tesseract worker);
#: - ``sidecar.ocr``      — the frisket-models service's POST /ocr multipart
#:   contract (dots.mocr, GLM-OCR, Surya 2, paddleocr-vl), which is distinct from
#:   transcription v1;
#: - ``datalab.convert``  — Datalab's hosted /convert(output_format=json) API;
#: - ``remote``           — a router-served ``provider/model`` VLM.
OcrTransport = Literal["local", "sidecar.ocr", "datalab.convert", "remote"]

#: The wires a to_markdown engine is spoken to over. ``datalab.convert`` is
#: spelled the same as OCR's because it IS the same endpoint — Datalab's
#: /convert — asked for a different output format. The two capabilities share a
#: venue and a wire and differ in the SKU they meter, which is why transport is
#: a target-row field and the capability is a separate declaration beside it.
ToMarkdownTransport = Literal["local", "sidecar.convert", "datalab.convert"]

#: The wires a translate engine is spoken to over. ``local`` covers both local
#: builds (opus_mt's CTranslate2 per-pair artifact and hy_mt2's GGUF); the two
#: hosted APIs get their own wire names because their request/response shapes
#: differ (DeepL reports ``billed_characters``; Google v2 does not, so its count
#: is the source length).
TranslateTransport = Literal["local", "deepl.v2", "google.translate.v2"]

#: The wires a geocode engine is spoken to over. Two providers, two contracts,
#: no local path: geocoding in this build always leaves the operator's box.
GeocodeTransport = Literal["opencage.v1", "nominatim.search"]

#: The wire the census capability is spoken to over: the batch coordinate
#: GeoLookup plus the ACS5 data call, which are one venue's two endpoints
#: reached in one row's work, not two engines.
CensusTransport = Literal["census.acs5"]

TranscriptionLanguageMode = Literal["auto_only", "fixed", "single", "multi"]
TranscriptionDiarizationMode = Literal["none", "optional", "intrinsic"]
TranscriptionSpeakerHint = Literal["none", "count"]


@dataclass(frozen=True)
class TranscriptionEngineCapabilities:
    """Fail-closed product capabilities for one transcription engine.

    This declaration describes the controls the product may accept and project,
    independently from runtime availability.  In particular, a sidecar engine's
    ``transport`` selects the wire contract without a request-only escape hatch,
    and each option defaults to unsupported rather than inheriting Whisper's
    controls.
    """

    transport: TranscriptionTransport
    language_mode: TranscriptionLanguageMode
    detects_language: bool
    fixed_language: str | None = None
    vad: bool = False
    diarization_mode: TranscriptionDiarizationMode = "none"
    diarization_default: bool = False
    speaker_hint: TranscriptionSpeakerHint = "none"
    max_speakers: int | None = None
    model_size: bool = False
    language_choices: dict[str, str] | None = None
    # Hotword/context prompt: a caller-supplied string of names and
    # jargon the engine biases recognition toward. Only the
    # ``frisket.transcription.v1`` wire carries a ``context`` field, so the
    # declaration is coherence-checked to that transport below.
    context: bool = False
    clean: bool = False

    def __post_init__(self) -> None:
        if self.language_mode == "fixed":
            if not self.fixed_language:
                raise ValueError("fixed language mode requires fixed_language")
            if self.detects_language:
                raise ValueError("a fixed-language engine cannot detect language")
        elif self.fixed_language is not None:
            raise ValueError("fixed_language is valid only in fixed language mode")
        if self.diarization_mode == "none" and (
            self.speaker_hint != "none"
            or self.max_speakers is not None
            or self.diarization_default
        ):
            raise ValueError("a non-diarizing engine cannot declare speaker controls")
        if self.max_speakers is not None and self.max_speakers < 1:
            raise ValueError("max_speakers must be positive")
        if (
            self.transport == "frisket.transcription.v1"
            and self.language_mode == "multi"
        ):
            raise ValueError(
                "transcription contract v1 accepts at most one language hint"
            )
        if self.context and self.transport not in {
            "local",
            "frisket.transcription.v1",
            "remote",
        }:
            # Generic remote engines remain false by default. A concrete
            # remote profile may opt in when its documented provider wire
            # carries vocabulary hints.
            raise ValueError(
                "context (hotwords) requires a supported transcription transport"
            )

    @property
    def accepts_language(self) -> bool:
        """Whether a caller-provided language hint crosses the boundary."""

        return self.language_mode in {"single", "multi"}


@dataclass(frozen=True)
class EngineDeclaration:
    """One symbolic engine in a family roster.

    ``provider`` is the receipt provider id (``provider_use`` grouping).
    ``billable`` marks engines that bill real money per call/page/token —
    they pass the cost-confirm gate and compare previews price them as
    unknown, never $0. ``run_scoped`` marks an engine that owns a process
    resource for one runner invocation (a width-one session or bounded pool,
    held by ``execution_scope``); the engine id keys its admission lease.
    ``listed=False``
    keeps an engine reachable by spec but out of the catalog/picker.
    ``resolves_to`` documents the
    one alias oddity: an engine id that the ops alias map rewrites to a
    ``provider/model`` id at execution time (transcribe's ``remote`` ->
    ``openai/whisper-1``) while the contract layer keeps the symbolic id
    canonical.

    There is no retirement window. At zero users a rename is a breaking
    change: the old spelling stops resolving and the
    ``*_DEAD_ENGINE_REPLACEMENTS`` maps below carry the teaching rejection
    that names the canonical replacement."""

    id: str
    label: str
    tier: EngineTier
    provider: str
    aliases: tuple[str, ...] = ()
    billable: bool = False
    run_scoped: bool = False
    listed: bool = True
    resolves_to: str | None = None
    transcription: TranscriptionEngineCapabilities | None = None


OCR_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        id="rapidocr",
        label="RapidOCR (local, default)",
        tier="local",
        provider="local",
        # Retired venue/tier words ("light"/"local") are listed in
        # OCR_DEAD_ENGINE_REPLACEMENTS below.
        run_scoped=True,
    ),
    EngineDeclaration(
        id="tesseract",
        label="Tesseract OCR (local, system binary)",
        tier="local",
        provider="local",
        aliases=("tess",),
    ),
    EngineDeclaration(
        id="paddleocr-vl",
        label="PaddleOCR-VL document VLM (sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
        aliases=("paddle", "paddleocr"),
    ),
    EngineDeclaration(
        id="dots.mocr",
        label="dots.mocr multilingual document parser (sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
        # Retired venue/tier words ("quality"/"sidecar") are listed in
        # OCR_DEAD_ENGINE_REPLACEMENTS below.
    ),
    EngineDeclaration(
        id="pp-ocrv6",
        label="PP-OCRv6 fast text OCR (sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
    ),
    EngineDeclaration(
        id="glm-ocr",
        label="GLM-OCR 0.9B multilingual OCR (sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
    ),
    EngineDeclaration(
        id="surya2",
        label="Surya 2 document OCR (local sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
    ),
    EngineDeclaration(
        id="datalab",
        label="Datalab hosted OCR (hosted, pay-per-call)",
        tier="hosted",
        provider="datalab",
        # Its old ``not_comparable`` reason ("bills per page; run it through
        # the cost-gated media.ocr action instead of the free-form compare")
        # was this same ruling applied to one engine by hand. The compare
        # surface now derives that refusal from ``billable`` for EVERY engine,
        # so the bespoke field is gone.
        billable=True,
    ),
)


TRANSCRIBE_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        # Local Faster-Whisper keeps its selectable size roster. The fixed
        # hosted Turbo model is the separate declaration below.
        id="faster_whisper",
        label="Whisper (multilingual transcription)",
        tier="local",
        provider="local",
        aliases=("whisper",),
        # Per-target sizes remain explicit in execution/definitions.py.
        transcription=TranscriptionEngineCapabilities(
            transport="local",
            language_mode="single",
            detects_language=True,
            vad=True,
            model_size=True,
            context=True,
        ),
    ),
    EngineDeclaration(
        id="whisper-turbo",
        label="Whisper Turbo",
        tier="sidecar",
        provider="self",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="single",
            detects_language=True,
            vad=True,
            context=True,
        ),
    ),
    EngineDeclaration(
        # Local ONNX and the hosted gateway are ONE engine — the same
        # fixed parakeet-tdt model
        # family on two execution targets. The id is the identity; the label
        # is friendly copy; ids and labels are distinct. The old spellings
        # completed their retirement window and were deleted —
        # see TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS below.
        id="parakeet-tdt",
        label="Parakeet (fast English transcription)",
        tier="local",
        provider="local-onnx",
        # run_scoped documents the LOCAL target's process-session behavior
        # (admission lease + width-one session); the gateway target is not
        # run-scoped — consumers resolve the per-target truth through the
        # route and static preference, never this flag alone.
        run_scoped=True,
        # Engine-level SEMANTIC union: diarization is a real capability of
        # this engine (the gateway target's Sortformer build, optional, fixed
        # 4-channel pass -> speaker_hint "none", cap 4); the local ONNX
        # build does not diarize — per-target support carries that
        # difference, and authoring accepts diarize=true because SOME
        # declared target supports it (union-of-declared-support).
        transcription=TranscriptionEngineCapabilities(
            transport="local",
            language_mode="fixed",
            fixed_language="en",
            detects_language=False,
            vad=True,
            diarization_mode="optional",
            speaker_hint="none",
            max_speakers=4,
        ),
    ),
    # The deprecated "remote" placement id was deleted (it was a
    # placement word masquerading as an engine; its ``resolves_to`` rewrote
    # it to ``openai/whisper-1`` at execution). Authors write the
    # provider-qualified id directly — under the fungibility test the
    # operator is constitutive of that computation, so the qualified name is
    # the honest one. TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS below teaches the
    # rejection; ``resolves_to`` itself stays for future use.
    EngineDeclaration(
        # First frisket.transcription.v1 engine. Diarization is intrinsic:
        # MOSS always emits speaker-labeled
        # segments in one pass and exposes no speaker knobs; a diarize/speaker
        # request field is rejected upstream rather than ignored. The pinned
        # build auto-detects language but does not report it, so no language
        # control is offered and result.language stays null (no-ignore rule).
        # The worker's `context` (hotword) option is declared here,
        # validated/projected by TranscriptionOptions, and
        # forwarded on the v1 wire by TranscriptionV1Adapter.
        # Self-host only until route-derived cost facts exist: billable
        # stays False because in the open composition the operator bears the
        # GPU cost; a hosted deployment without route facts would be
        # unbilled egress and is therefore fenced off.
        # Label names the engine and its gateway requirement without asserting
        # venue facts it cannot know (the worker URL is arbitrary — same box,
        # LAN, or elsewhere); truthful venue/egress labels arrive with routes
        # in the route.
        id="moss",
        label="MOSS diarizing transcription (via models gateway)",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="auto_only",
            detects_language=False,
            vad=False,
            diarization_mode="intrinsic",
            speaker_hint="none",
            context=True,
        ),
    ),
    EngineDeclaration(
        id="openrouter/microsoft/mai-transcribe-2",
        label="Microsoft MAI-Transcribe 2 (OpenRouter)",
        tier="hosted",
        provider="openrouter",
        billable=True,
        transcription=TranscriptionEngineCapabilities(
            transport="remote",
            language_mode="single",
            detects_language=True,
            language_choices={
                "af": "Afrikaans",
                "ar": "Arabic",
                "as": "Assamese",
                "az": "Azerbaijani",
                "bg": "Bulgarian",
                "bn": "Bengali",
                "bs": "Bosnian",
                "ca": "Catalan",
                "cs": "Czech",
                "da": "Danish",
                "de": "German",
                "el": "Greek",
                "en": "English",
                "es": "Spanish",
                "et": "Estonian",
                "fa": "Persian",
                "fi": "Finnish",
                "fil": "Filipino",
                "fr": "French",
                "gl": "Galician",
                "gu": "Gujarati",
                "he": "Hebrew",
                "hi": "Hindi",
                "hu": "Hungarian",
                "hy": "Armenian",
                "id": "Indonesian",
                "is": "Icelandic",
                "it": "Italian",
                "ja": "Japanese",
                "kk": "Kazakh",
                "kn": "Kannada",
                "ko": "Korean",
                "lt": "Lithuanian",
                "lv": "Latvian",
                "mk": "Macedonian",
                "ml": "Malayalam",
                "mr": "Marathi",
                "ms": "Malay",
                "nb": "Norwegian Bokmål",
                "ne": "Nepali",
                "nl": "Dutch",
                "or": "Odia",
                "pa": "Punjabi (Gurmukhi script)",
                "pl": "Polish",
                "pt": "Portuguese",
                "ro": "Romanian",
                "ru": "Russian",
                "sk": "Slovak",
                "sl": "Slovenian",
                "sv": "Swedish",
                "sw": "Swahili",
                "ta": "Tamil",
                "te": "Telugu",
                "th": "Thai",
                "tr": "Turkish",
                "uk": "Ukrainian",
                "ur": "Urdu",
                "vi": "Vietnamese",
                "yue": "Cantonese",
                "zh": "Chinese (simplified)",
            },
            diarization_mode="optional",
            diarization_default=True,
            speaker_hint="none",
            context=True,
            clean=True,
        ),
    ),
    EngineDeclaration(
        # Fixed VibeVoice-ASR gateway profile. It always returns
        # speaker-labelled, timestamped segments, auto-detects without
        # reporting a language, and only accepts the free-text hotword
        # context. No caller diarization, language, VAD, or size knob can be
        # truthfully offered.
        id="vibevoice-asr",
        label="VibeVoice-ASR diarizing transcription (via models gateway)",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="auto_only",
            detects_language=False,
            vad=False,
            diarization_mode="intrinsic",
            speaker_hint="none",
            context=True,
        ),
    ),
)

_missing_transcription_capabilities = tuple(
    entry.id for entry in TRANSCRIBE_ENGINE_TABLE if entry.transcription is None
)
if _missing_transcription_capabilities:
    raise RuntimeError(
        "transcription engine declarations are missing capabilities: "
        + ", ".join(_missing_transcription_capabilities)
    )


TO_MARKDOWN_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        id="markitdown",
        label="MarkItDown (local, default)",
        tier="local",
        provider="local",
        # Retired venue/tier words ("light"/"local") are listed in
        # TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS.
    ),
    EngineDeclaration(
        id="trafilatura_html",
        label="Trafilatura HTML extraction (local)",
        tier="local",
        provider="local",
    ),
    EngineDeclaration(
        id="docling",
        label="Docling layout/OCR quality tier (sidecar)",
        tier="sidecar",
        provider="frisket-sidecar",
        # Retired venue/tier words ("quality"/"sidecar") are listed in
        # TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS.
    ),
    EngineDeclaration(
        id="chandra",
        label="Chandra 2 document VLM (sidecar, experimental)",
        tier="sidecar",
        provider="frisket-sidecar",
    ),
    EngineDeclaration(
        id="datalab",
        label="Datalab Marker (hosted, pay-per-call, PDF/Office/image only)",
        tier="hosted",
        provider="datalab",
        billable=True,
    ),
)


#: The translate capability's SEAM roster. ``llm`` is deliberately ABSENT: the
#: LLM translate path is priced by the runner's token sampler in two units
#: (input and output tokens per million), and ``PricedCostBasis`` carries
#: exactly one (rate, quantity, unit) triple. Declaring ``llm`` here would let
#: a translate route resolve to a venue whose meter the cost basis cannot
#: express, which is a fabricated quote, not a missing feature. So an
#: ``engine="llm"`` translate spec canonicalizes to nothing and refuses
#: loudly — and ``TranslateRecipe`` cannot declare ``consumes_resolution``
#: until the multi-unit pricing ruling lands.
#:
#: ``maps.TRANSLATE_ENGINES`` stays the contract's authoritative axis; this
#: table is its non-LLM subset and a parity test pins the relationship.
TRANSLATE_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        id="deepl",
        label="DeepL API translation (hosted, pay-per-character)",
        tier="hosted",
        provider="deepl",
        billable=True,
    ),
    EngineDeclaration(
        id="google_translate",
        label="Google Cloud Translation v2 (hosted, pay-per-character)",
        tier="hosted",
        provider="google",
        billable=True,
    ),
    EngineDeclaration(
        id="opus_mt",
        label="OPUS-MT CTranslate2 (local, per language pair)",
        tier="local",
        provider="local",
    ),
    EngineDeclaration(
        id="hy_mt2",
        label="Hunyuan MT2 GGUF (local, experimental)",
        tier="local",
        provider="local",
    ),
)


#: The geocode capability's seam roster. ``auto`` is NOT an engine and is
#: deliberately absent: it is a selection sentinel the recipe resolves from
#: credential presence BEFORE the seam is asked, and an alias must name exactly
#: one canonical id — mapping ``auto`` to ``opencage`` would resolve an
#: unkeyed deployment onto a venue it cannot reach, and mapping it to
#: ``nominatim`` would silently decline a paid key the operator configured.
#: Declaration order is preference order: OpenCage first, Nominatim second.
GEOCODE_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        id="opencage",
        label="OpenCage geocoder (hosted, pay-per-row)",
        tier="hosted",
        provider="opencage",
        billable=True,
    ),
    EngineDeclaration(
        id="nominatim",
        label="Nominatim geocoder (hosted, rate-limited)",
        tier="hosted",
        provider="nominatim",
        # This request leaves the box, but Nominatim's public API is not a
        # priced engine.  ``tier`` carries the egress fact independently from
        # ``billable``.
        billable=False,
    ),
)


#: The census capability's seam roster: one venue, one engine. The recipe
#: spells its selector ``provider``, not ``engine`` — a same-name-different-
#: meaning hazard the seam does not inherit, because the seam asks for an
#: ENGINE symbol and this table is the only thing that answers.
CENSUS_ENGINE_TABLE: tuple[EngineDeclaration, ...] = (
    EngineDeclaration(
        id="us_census_acs",
        label="US Census ACS 5-year enrichment (hosted)",
        tier="hosted",
        provider="us_census_acs",
        billable=False,
    ),
)


# ---------------------------------------------------------------------------
# Dead engine names.
#
# The retirement cohort's one-release acceptance window is closed:
# these spellings no longer validate, canonicalize, or resolve ANYWHERE.
# The ``*_DEAD_ENGINE_REPLACEMENTS`` maps exist for exactly one purpose —
# the validation rejection names the canonical replacement so the refusal
# teaches ("engine 'parakeet_modal' was retired; use 'parakeet-tdt'") when
# an operator types an old name from muscle memory. They are UX copy, not
# compatibility machinery: nothing resolves, renders, or replays through
# them.
# ---------------------------------------------------------------------------

TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS: dict[str, str] = {
    "local": "faster_whisper",
    "sidecar": "faster_whisper",
    "quality": "faster_whisper",
    "faster-whisper": "faster_whisper",
    "parakeet": "parakeet-tdt",
    "parakeet_modal": "parakeet-tdt",
    "modal": "parakeet-tdt",
    "remote": "openai/whisper-1",
}

OCR_DEAD_ENGINE_REPLACEMENTS: dict[str, str] = {
    "light": "rapidocr",
    "local": "rapidocr",
    "quality": "dots.mocr",
    "sidecar": "dots.mocr",
}

TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS: dict[str, str] = {
    "light": "markitdown",
    "local": "markitdown",
    "quality": "docling",
    "sidecar": "docling",
}


def dead_engine_rejection(replacements: dict[str, str], engine: str) -> str | None:
    """The teaching copy for a dead engine name (None = not a dead name).

    Validation embeds this in its error so the rejection names the canonical
    replacement instead of a bare "invalid engine"."""
    replacement = replacements.get(engine)
    if replacement is None:
        return None
    return f"engine '{engine}' was retired; use '{replacement}'"


def engine_ids(
    table: tuple[EngineDeclaration, ...], *, tier: EngineTier | None = None
) -> tuple[str, ...]:
    """Canonical engine ids in table order, optionally one tier's."""
    return tuple(e.id for e in table if tier is None or e.tier == tier)


def symbolic_engine_names(table: tuple[EngineDeclaration, ...]) -> frozenset[str]:
    """Every name the contract accepts symbolically: ids + aliases."""
    names: set[str] = set()
    for entry in table:
        names.add(entry.id)
        names.update(entry.aliases)
    return frozenset(names)


def alias_map(table: tuple[EngineDeclaration, ...]) -> dict[str, str]:
    """alias -> canonical id (the contract-layer vocabulary; ``resolves_to``
    is NOT applied — the symbolic id stays canonical for validation).

    Every alias is advertised: the catalog, the picker, and the shipped JSON
    artifact all expose exactly this map. There is no unadvertised-but-
    accepted tier."""
    return {alias: entry.id for entry in table for alias in entry.aliases}


def execution_alias_map(table: tuple[EngineDeclaration, ...]) -> dict[str, str]:
    """The ops-layer alias map: aliases -> id, PLUS the ``resolves_to``
    oddity (an id rewritten to a ``provider/model`` id at execution)."""
    out = alias_map(table)
    for entry in table:
        if entry.resolves_to is not None:
            out[entry.id] = entry.resolves_to
    return out


def receipt_provider_map(table: tuple[EngineDeclaration, ...]) -> dict[str, str]:
    """id + aliases -> receipt provider id (executor provider_use grouping)."""
    out: dict[str, str] = {}
    for entry in table:
        out[entry.id] = entry.provider
        for alias in entry.aliases:
            out[alias] = entry.provider
    return out


def find_engine(
    table: tuple[EngineDeclaration, ...], engine: str
) -> EngineDeclaration | None:
    """The declaration for an id or alias, else None (provider/model ids and
    unknown names both return None)."""
    for entry in table:
        if engine == entry.id or engine in entry.aliases:
            return entry
    return None


def transcription_engine_capabilities(
    table: tuple[EngineDeclaration, ...], engine: str
) -> TranscriptionEngineCapabilities:
    """Resolve declared transcription capabilities for an id or alias.

    Free-form ``provider/model`` engines share the explicit remote transport
    family: they accept one language hint, and only Whisper-class responses
    report a detected language.  Every symbolic engine must carry its own
    declaration; an unknown symbolic id or a missing declaration fails closed.
    """

    entry = find_engine(table, engine)
    if entry is not None:
        if entry.transcription is None:
            raise ValueError(
                f"transcription engine '{entry.id}' has no capability declaration"
            )
        return entry.transcription
    if "/" in engine:
        _provider, _, model = engine.partition("/")
        return TranscriptionEngineCapabilities(
            transport="remote",
            language_mode="single",
            detects_language="whisper" in model,
        )
    raise ValueError(f"unknown transcription engine '{engine}'")


def project_transcription_engine_options(
    engine: str,
    spec: dict[str, Any],
    *,
    table: tuple[EngineDeclaration, ...] = TRANSCRIBE_ENGINE_TABLE,
) -> dict[str, Any]:
    """Project only options the selected engine declares it can consume.

    This declaration-only helper is intentionally in the stdlib-light engine
    roster rather than the Pydantic action facade: the per-row Faster-Whisper
    subprocess uses it as its second fail-closed fence without importing the
    application or LLM stack.
    """

    capabilities = transcription_engine_capabilities(table, engine)
    projected: dict[str, Any] = {}
    language = spec.get("language")
    if capabilities.accepts_language and language not in (None, [], ""):
        projected["language"] = language
    if capabilities.vad and "vad" in spec:
        projected["vad"] = spec["vad"]
    if capabilities.diarization_mode == "optional" and "diarize" in spec:
        projected["diarize"] = spec["diarize"]
    if capabilities.speaker_hint == "count":
        for field in ("num_speakers", "min_speakers", "max_speakers"):
            if spec.get(field) is not None:
                projected[field] = spec[field]
    if capabilities.model_size and spec.get("model_size") is not None:
        projected["model_size"] = spec["model_size"]
    if capabilities.context and spec.get("context") is not None:
        projected["context"] = spec["context"]
    if capabilities.clean and "clean" in spec:
        projected["clean"] = spec["clean"]
    return projected


for _family_table, _family_dead in (
    (TRANSCRIBE_ENGINE_TABLE, TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS),
    (OCR_ENGINE_TABLE, OCR_DEAD_ENGINE_REPLACEMENTS),
    (TO_MARKDOWN_ENGINE_TABLE, TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS),
):
    _collisions = symbolic_engine_names(_family_table) & set(_family_dead)
    if _collisions:
        # A name cannot be both dead (rejected with teaching copy) and live
        # (accepted): re-introducing a dead spelling requires deleting it
        # from the dead maps first — a deliberate product decision.
        raise RuntimeError(
            "dead engine names collide with live roster names: "
            + ", ".join(sorted(_collisions))
        )
del _family_table, _family_dead, _collisions


def is_billable_engine(table: tuple[EngineDeclaration, ...], engine: str) -> bool:
    """Whether an engine bills real money: any ``provider/model`` id, or a
    table entry declared billable. Replaces the bare ``"/" in engine``
    heuristic, which misclassified symbolic hosted engines (datalab) as
    free."""
    entry = find_engine(table, engine)
    if entry is not None:
        return entry.billable
    return "/" in engine
