"""The per-model structured-output capability table.

Replaces the per-adapter ``strict_schema`` booleans that used to be set at
router construction with **per-model** rows,
glob-matched, most-specific-first. This is now the SINGLE SOURCE for which
wire mechanism a model uses when a caller doesn't explicitly override it —
``llm/adapters.py`` consults :func:`resolve` when ``LLMRequest.mechanism`` is
``None``. This fallback is the standing contract for any caller — legacy or
completer-issued-auto — that leaves the field unset, and
``llm/structured.py``'s completer consults it to build the ``method_used``
audit tag and the pydantic-ai ``ModelProfile``.

**PARITY-ONLY seed.** Every row below byte-matches the CURRENT
behavior it replaces — no capability promotions. In particular
``openrouter/*`` and ``ollama/*`` stay ``JSON_LOOSE`` even though a
``openrouter/<vendor>/*`` glob *could* resolve more specifically (the
resolution algorithm below supports it) — promoting any vendor glob to
``NATIVE_STRICT`` requires an explicit recorded probe, never a static
assumption.

**Cache-key / cassette parity.**
The table's seed intentionally reproduces the SAME resolution every current
model already gets, so byte-identical wire bodies are the natural result —
but the resolved mechanism is deliberately kept OFF the wire request when a
caller leaves ``method="auto"``/``mechanism=None`` (``llm/structured.py``
still passes ``mechanism=None`` through to ``LLMRequest`` for that case).
``cache.py:request_key`` hashes ``req.mechanism``
verbatim, so stamping a resolved string onto every auto-resolved request
would flip every existing cassette's key from ``None`` to e.g.
``"native_strict"`` and invalidate the committed golden cache
(``tests/cache/llm_cache.db``) for no behavior change. Table consultation
therefore happens at the READ side (adapters resolving a wire shape,
completer resolving an audit tag / a ``ModelProfile``), never by writing the
resolved value back onto the cache-keyed request.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

# Mechanism ids -- the same closed vocabulary llm/structured.py's completer
# uses. Defined HERE now (the table is the single source of truth for
# mechanism identity); llm/structured.py imports these rather than keeping
# its own copy, so the two can't drift.
NATIVE_STRICT = "native_strict"  # OpenAI/Gemini json_schema strict mode
NATIVE_TOOL = "native_tool"  # Anthropic forced tool_use ("emit")
JSON_LOOSE = "json_loose"  # json_object + schema-in-prompt
PROSE_JSON = "prose_json"  # no response_format; JSON asked for in prose
MECHANISMS = (NATIVE_STRICT, NATIVE_TOOL, JSON_LOOSE, PROSE_JSON)


@dataclass(frozen=True)
class CapabilityRow:
    """One capability table row.

    ``additional_properties_false``/``nullable_optional_convention`` name the
    two Section 5 mechanics that only apply to the strict-mode wire path
    (``_strictify``, ``adapters.py:290-306``, demoted to a helper invoked only
    for ``NATIVE_STRICT``): the request body forces
    ``additionalProperties:false`` + all-required, and callers must therefore
    use the ``{"type": [T, "null"]}`` nullable-optional convention (Section 5)
    for any field that's optional in spirit but must survive strict mode's
    forced ``required``. Both are ``False`` for every non-``NATIVE_STRICT``
    mechanism today (parity-only: nothing else forces this).
    """

    provider: str  # e.g. "openai", "anthropic", "*" (conservative fallback)
    model_pattern: str  # glob matched against the full "provider/model" id
    structured_mode: str  # a MECHANISMS id -- what "auto" resolves to
    additional_properties_false: bool
    nullable_optional_convention: bool
    source: str  # provenance referent for this row

    def as_profile_dict(self) -> dict:
        """This row's shape as the dict ``FrisketRouterModel``'s
        ``_profile_from_capability`` already consumes:
        ``default_mechanism``/``supports_native_strict``/
        ``supports_native_tool``/``supports_json_object``. Derived, not
        stored twice: in this parity-only seed every provider supports
        exactly the ONE mechanism it defaults to, except that every
        OpenAI-compat dialect (anything not ``NATIVE_TOOL``) also always
        speaks plain ``json_object`` -- matching the original capability table
        (``openai``/``gemini`` rows carry ``json_obj=True`` even though their
        default is ``NATIVE_STRICT``)."""
        return {
            "default_mechanism": self.structured_mode,
            "supports_native_strict": self.structured_mode == NATIVE_STRICT,
            "supports_native_tool": self.structured_mode == NATIVE_TOOL,
            "supports_json_object": self.structured_mode != NATIVE_TOOL,
        }


# ---------------------------------------------------------------------------
# PARITY-ONLY seed. Each row's `source` field cites the exact adapter
# behavior it reproduces -- verified byte-match, not promotion. No
# vendor-glob strict promotion for openrouter here; that stays a deliberate,
# separate opt-in (see RECORDED_PROMOTIONS below).
# ---------------------------------------------------------------------------
SEED: tuple[CapabilityRow, ...] = (
    CapabilityRow(
        provider="openai",
        model_pattern="openai/*",
        structured_mode=NATIVE_STRICT,
        additional_properties_false=True,
        nullable_optional_convention=True,
        source="parity:router.py:75-83 (openai adapter, default strict_schema=True)",
    ),
    CapabilityRow(
        provider="gemini",
        model_pattern="gemini/*",
        structured_mode=NATIVE_STRICT,
        additional_properties_false=True,
        nullable_optional_convention=True,
        source="parity:router.py:84-91 (gemini adapter, strict_schema=True)",
    ),
    CapabilityRow(
        provider="anthropic",
        model_pattern="anthropic/*",
        structured_mode=NATIVE_TOOL,
        additional_properties_false=False,
        nullable_optional_convention=False,
        source="parity:adapters.py:80-88 (AnthropicAdapter, unconditional forced tool_use)",
    ),
    CapabilityRow(
        provider="openrouter",
        model_pattern="openrouter/*",
        structured_mode=JSON_LOOSE,
        additional_properties_false=False,
        nullable_optional_convention=False,
        source="parity:router.py:92-97 (openrouter adapter, strict_schema=False)",
    ),
    CapabilityRow(
        provider="ollama",
        model_pattern="ollama/*",
        structured_mode=JSON_LOOSE,
        additional_properties_false=False,
        nullable_optional_convention=False,
        source="parity:router.py:98-108 (ollama adapter, strict_schema=False)",
    ),
)

# Conservative fallback: an unknown model gets the WEAKEST row -- JSON_LOOSE,
# never assume a capability we haven't seen. This is a static default, not
# the probe-and-cache mechanism (see RECORDED_PROMOTIONS).
FALLBACK = CapabilityRow(
    provider="*",
    model_pattern="*",
    structured_mode=JSON_LOOSE,
    additional_properties_false=False,
    nullable_optional_convention=False,
    source="conservative-fallback (unknown model -> weakest row, never assumed)",
)


def _specificity(glob: str) -> tuple[int, int]:
    """Resolution specificity: (1) count of non-wildcard path
    segments, descending; (2) tie-break on literal-prefix length (chars
    before the first wildcard), descending. Returned as a tuple so sorting
    on it applies both criteria in one pass."""
    segments = glob.split("/")
    non_wildcard_segments = sum(1 for s in segments if "*" not in s and "?" not in s)
    first_wildcard = min(
        (i for i, c in enumerate(glob) if c in "*?"), default=len(glob)
    )
    return (non_wildcard_segments, first_wildcard)


def resolve(
    model: str,
    table: tuple[CapabilityRow, ...] = SEED,
    *,
    promotions: tuple[CapabilityRow, ...] = (),
) -> CapabilityRow:
    """The model id (e.g. ``"openai/gpt-5-mini"``) -> its capability row.

    Glob-matched, most-specific-first. No match at all ->
    :data:`FALLBACK`. ``table`` is overridable so tests can exercise the
    specificity algorithm against synthetic overlapping globs (e.g.
    ``"openrouter/openai/*"`` vs ``"openrouter/*"``) without touching the
    real parity seed.

    ``promotions`` defaults to ``()`` for zero behavior change in every
    existing caller. It is where
    :data:`RECORDED_PROMOTIONS` and any per-model rows a
    ``llm/structured_probe.py`` :class:`~llm.structured_probe.ProbeStore` has
    recorded get merged into the candidate pool -- opt-in, per-call, never a
    change to this function's default output. A promoted/probed row is just
    another candidate in the SAME specificity contest ``table`` rows already
    run in (a literal ``"openrouter/openai/gpt-5"`` probe row or the
    ``"openrouter/openai/*"`` recorded-promotion glob both out-specify the
    bare ``"openrouter/*"`` SEED row automatically, no algorithm change
    needed) because the capability table's resolution order already reserves
    the slot.
    """
    candidates = [
        row
        for row in (*table, *promotions)
        if fnmatch.fnmatchcase(model, row.model_pattern)
    ]
    if not candidates:
        return FALLBACK
    return max(candidates, key=lambda row: _specificity(row.model_pattern))


# ---------------------------------------------------------------------------
# Which mechanisms each wire DIALECT can structurally speak at all -- a hard
# fact about llm/adapters.py's two adapter classes, NOT a per-model
# capability guess (that's SEED/FALLBACK/promotions above). Used by
# llm/structured.py's explicit-override validation to validate the model
# actually supports the pinned mechanism where knowable, honest error where
# not: an override is checked against this map regardless of what "auto"
# would have resolved to.
#
# AnthropicAdapter: the Messages API has no `response_format` concept at all
# -- no json_schema strict mode, no json_object mode -- so NATIVE_STRICT/
# JSON_LOOSE are genuinely, structurally absent, not a policy choice. It
# speaks exactly its native forced tool_use dialect (NATIVE_TOOL) plus
# PROSE_JSON (its schema branch skips `tools` entirely and asks in the
# `system` prompt instead, mirroring OpenAICompatAdapter's PROSE_JSON
# branch).
#
# OpenAICompatAdapter (covers openai/gemini/openrouter/ollama): speaks
# NATIVE_STRICT (`response_format: json_schema`), JSON_LOOSE
# (`response_format: json_object`), and PROSE_JSON (no `response_format` at
# all) -- all three branch on `mechanism` in `complete()`. It has never
# wired a forced-single-tool "emit" dialect for structured output
# (NATIVE_TOOL is Anthropic-only) -- pinning NATIVE_TOOL there is a real,
# currently-true gap, not a guess.
_ANTHROPIC_DIALECT_MECHANISMS = frozenset({NATIVE_TOOL, PROSE_JSON})
_OPENAI_COMPAT_DIALECT_MECHANISMS = frozenset({NATIVE_STRICT, JSON_LOOSE, PROSE_JSON})
# The 5 real provider prefixes the router actually constructs an adapter for
# -- the ONLY prefixes whose dialect is a hard, knowable fact. Anything else
# (a test double's "mock"/"m" provider, a future provider not yet wired, a
# typo) is genuinely UNKNOWN dialect, not "assume OpenAI-compat" --
# conflating "not anthropic" with "must be OpenAI-compat" would reject
# legitimate non-provider model ids (tests pin `method=native_tool` against
# a synthetic "mock/model"/"mock/m" id to drive the Anthropic wire SHAPE
# generically, with no real adapter behind it).
_KNOWN_OPENAI_COMPAT_PROVIDERS = frozenset({"openai", "gemini", "openrouter", "ollama"})


def dialect_mechanisms(model: str) -> frozenset[str]:
    """The mechanisms ``model``'s wire dialect can structurally express --
    only for the 5 provider prefixes ``router.py`` actually constructs a real
    adapter for (the same ``model.split("/", 1)[0]`` prefix
    ``router.py:_call_with_retry`` uses to pick an adapter instance).
    ``"openrouter/anthropic/claude-x"`` gets OpenAICompatAdapter's mechanisms
    here (it rides OpenRouter's OpenAI-compatible endpoint, NOT
    AnthropicAdapter), exactly matching which adapter class actually handles
    the wire call. An UNRECOGNIZED provider prefix (not one of the 5) is
    NOT knowable -- returns every mechanism (never rejects), the same
    record-don't-experiment posture :data:`FALLBACK` takes for per-model
    resolution: silence about an unfamiliar provider is not the same as a
    known dialect incompatibility.
    """
    provider = model.split("/", 1)[0]
    if provider == "anthropic":
        return _ANTHROPIC_DIALECT_MECHANISMS
    if provider in _KNOWN_OPENAI_COMPAT_PROVIDERS:
        return _OPENAI_COMPAT_DIALECT_MECHANISMS
    return frozenset(MECHANISMS)


# ---------------------------------------------------------------------------
# RECORDED PROMOTIONS. Not part of SEED (parity-only, forever -- tests pin
# `resolve(model)` with no `promotions=` to still return JSON_LOOSE for
# "openrouter/openai/gpt-5" and must keep doing so). Applied only when a
# caller explicitly opts in by passing `promotions=RECORDED_PROMOTIONS` (or,
# in practice, by constructing `StructuredCompleter` with a `ProbeStore`) --
# for "auto" resolution the promotion only takes effect behind that same
# opt-in, never unconditionally from the glob.
#
# openrouter/openai/* -> NATIVE_STRICT: OpenRouter's `openai/*`-family models
# proxy OpenAI's own `/chat/completions` wire dialect 1:1 (the reason
# OpenAICompatAdapter handles openrouter with zero special-casing already --
# same class, same `response_format` shapes). The evidence this promotion
# needs is a recorded `probe_mechanism()` run (real completer code, real
# jsonschema validation) against a fixture reproducing OpenRouter's
# documented `openai/*` strict-json_schema response body, recorded
# `supported=True`. Not a live production API call (this sandbox has no
# OpenRouter key); the fixture is the recorded evidence.
RECORDED_PROMOTIONS: tuple[CapabilityRow, ...] = (
    CapabilityRow(
        provider="openrouter",
        model_pattern="openrouter/openai/*",
        structured_mode=NATIVE_STRICT,
        additional_properties_false=True,
        nullable_optional_convention=True,
        source=(
            "probe:tests/test_structured_probe.py::"
            "test_recorded_promotion_evidence (mocked-wire "
            "probe_mechanism() run confirming NATIVE_STRICT accepted for an "
            "openrouter/openai/* model; OpenRouter's openai/* proxy speaks "
            "the identical OpenAI json_schema-strict wire dialect "
            "OpenAICompatAdapter already implements uniformly)"
        ),
    ),
)
