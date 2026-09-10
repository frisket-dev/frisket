"""Shared helpers for the hosted translation engines (DeepL and Google Cloud
Translation), including their common validation and error-shaping rules.

Engine-agnostic vocabulary shared by the DeepL/Google clients, the recipe
dispatch, and the (held) action-level target preflight:

- ``TranslateEngineError`` — the per-row error taxonomy the clients raise and
  MapRunner surfaces as the row's ``error_code``.
- ``normalize_detected_bcp47`` — the ONE canonical STORED form for detected
  source language across engines: proper subtag casing, primary-subtag
  validation, LLM prose ("English") mapped via a name table, unknown → None
  (never stored raw), following BCP-47.
- ``normalize_language(engine, value, role)`` — free-text name / loose code →
  the provider's own code for that role. Source and target differ:
  an EXPLICIT region is preserved for a target (DeepL EN-GB stays EN-GB, PT-PT
  stays PT-PT; Google zh-TW stays zh-TW) but DeepL SOURCE takes only the bare
  language (the live API rejects EN-GB as a source), and
  bare targets pass through bare (EN/PT/ZH are all valid DeepL targets, verified
  — no invented EN-US/PT-BR/ZH-CN default).
"""

from __future__ import annotations

from typing import Any

from frisket.ops.integrations.hosted_error import HostedEngineError

# Same class object as ``HostedEngineError`` (shared with datalab.py's
# ``DatalabEngineError``), not a subclass, so every existing
# ``except TranslateEngineError`` / ``isinstance`` call site keeps working.
TranslateEngineError = HostedEngineError


# The hosted engines that bill per character (DeepL, Google) — the local
# opus_mt/hy_mt2 engines are free once provisioned. Used by the cost estimate,
# the char-count confirmation gate, and per-row spend recording.
HOSTED_TRANSLATE_ENGINES: tuple[str, ...] = ("deepl", "google_translate")

# Char-count confirmation threshold: no dollar estimate — gate large hosted
# runs on total source characters. TUNABLE: ~100k source chars ≈ a full novel
# chapter; below it a hosted run proceeds without a confirmation, at/above it
# the run confirms once. Overridable via env.
_DEFAULT_CHAR_CONFIRM_THRESHOLD = 100_000


def hosted_translate_response_accounting(
    *,
    engine: str,
    provider: str,
    credential_source: str,
    status_code: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """One unknown-cost fact for a concrete but unusable provider response.

    Receiving an HTTP response proves egress and provider ownership, but an
    error status or unusable body does not prove that the provider applied its
    successful per-character tariff.  Preserve the request while refusing to
    invent either a configured charge or a confident zero.
    """
    from frisket.ai.models.metadata import ModelCallMeta

    fact = ModelCallMeta.provider_call(
        capability="translate",
        engine=engine,
        provider=provider,
        provider_kind="platform_api",
        credential_source=credential_source,
        provider_reported_cost_usd=None,
        provider_cost_usd=None,
        cost_source="unknown",
        units={"requests": 1},
        request_id=request_id,
        warnings=[f"{provider} returned HTTP {status_code}; request cost is unknown"],
        duration_ms=None,
    ).as_dict()
    return {
        "cost": None,
        "cost_source": "unknown",
        "model_calls": [fact],
    }


def translate_char_confirm_threshold() -> int:
    import os

    raw = os.environ.get("FRISKET_TRANSLATE_CHAR_CONFIRM_THRESHOLD")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_CHAR_CONFIRM_THRESHOLD


def hosted_translate_cost(engine: str, char_count: int) -> float | None:
    """Estimated USD spend for ``char_count`` characters on a hosted engine,
    from the pinned per-character rate (external_pricing, marked estimated).
    ``None`` when the rate is unknown (engine unpriced) so callers record an
    honest unknown rather than a fabricated zero."""
    from frisket.ai.external_pricing import (
        DEEPL_TRANSLATE_CHAR,
        GOOGLE_TRANSLATE_CHAR,
        external_unit_price_usd,
    )

    key = {
        "deepl": DEEPL_TRANSLATE_CHAR,
        "google_translate": GOOGLE_TRANSLATE_CHAR,
    }.get(engine)
    if key is None:
        return None
    rate = external_unit_price_usd(key)
    if rate is None:
        return None
    return round(char_count * rate, 6)


# Common English language NAME -> ISO 639-1 base. Deliberately the ~"99% of use
# cases, worldwide" set, not exhaustive; an unknown target fails once at the
# (held) preflight with a clear message rather than being guessed.
_NAME_TO_ISO: dict[str, str] = {
    "arabic": "ar",
    "bulgarian": "bg",
    "chinese": "zh",
    "mandarin": "zh",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "norwegian": "nb",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
}

# Supported primary language bases (the "99%" set). ``nb`` (Norwegian Bokmål)
# rides along for DeepL/Google.
_SUPPORTED_ISO = set(_NAME_TO_ISO.values()) | {"nb"}


# Canonical ISO base -> English display name for the supported roster. This is
# the SEED for the per-engine translate `language` declarations (their picker
# `choices`), kept beside _NAME_TO_ISO so the roster drifts in one place. Keys
# are exactly _SUPPORTED_ISO (a test pins that); one canonical name per code
# (the name->code table has synonyms like mandarin->zh that don't round-trip).
SUPPORTED_LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "zh": "Chinese",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "et": "Estonian",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "nb": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "es": "Spanish",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
}


def _cased_subtag(sub: str) -> str:
    """BCP-47 subtag casing: script (4 alpha) Titlecase, region (2 alpha / 3
    digit) UPPER, everything else lower."""
    if len(sub) == 4 and sub.isalpha():
        return sub.capitalize()
    if len(sub) in (2, 3):
        return sub.upper()
    return sub.lower()


def normalize_detected_bcp47(raw: str | None) -> str | None:
    """Canonical BCP-47 for a detected source language, for storage.

    Accepts what the engines actually emit: DeepL upper codes (``EN``,
    ``PT-BR``, ``ZH``), Google region codes (``en``, ``zh-CN``), an already
    BCP-47 value, OR an LLM-produced language NAME (``English``). Names map
    through the table; a code's primary subtag is validated as a 2-3 letter
    language subtag. Anything unrecognized returns ``None`` so the caller stores
    null (with a warning) rather than a raw, non-conformant value.
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low in _NAME_TO_ISO:  # LLM prose, e.g. "English"
        return _NAME_TO_ISO[low]
    parts = [p for p in cleaned.replace("_", "-").split("-") if p]
    if not parts:
        return None
    primary = parts[0].lower()
    # A valid ISO 639 language subtag is 2-3 alpha chars; reject prose/garbage.
    if not (2 <= len(primary) <= 3 and primary.isalpha()):
        return None
    return "-".join([primary, *(_cased_subtag(s) for s in parts[1:])])


def _split(value: str) -> tuple[str, list[str]] | None:
    """(primary_iso, [subtags]) for a free-text name or code, or None if the
    primary language is not in the supported set. Subtags are the raw region/
    script parts as the caller typed them (case-normalized downstream)."""
    cleaned = value.strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low in _NAME_TO_ISO:  # a language NAME carries no region
        return _NAME_TO_ISO[low], []
    parts = [p for p in cleaned.replace("_", "-").split("-") if p]
    if not parts:
        return None
    primary = parts[0].lower()
    if primary not in _SUPPORTED_ISO:
        return None
    return primary, parts[1:]


def normalize_language(engine: str, value: str, role: str) -> str:
    """The provider's own code for ``value`` in the given ``role``
    (``"source"`` | ``"target"``).

    ``llm`` passes through unchanged (the model takes any language name). For
    DeepL/Google:

    - TARGET preserves an explicit region (``en-GB`` → DeepL ``EN-GB`` /
      Google ``en-GB``; ``zh-TW`` → Google ``zh-TW``); a bare language stays
      bare (``EN``/``PT``/``ZH`` are valid DeepL targets — verified live, no
      invented default).
    - DeepL SOURCE takes only the bare language (the API rejects a regional
      ``source_lang``), so a region is dropped HERE,
      by the provider's documented rule, not silently elsewhere. Google source
      accepts a region, so it is preserved.

    Raises ``TranslateEngineError`` (``invalid_target`` / ``invalid_source``)
    for an unsupported language so the preflight/row fails with a clear cause.
    """
    if engine == "llm":
        return value.strip()
    split = _split(value)
    if split is None:
        code = "invalid_target" if role == "target" else "invalid_source"
        raise TranslateEngineError(
            code=code,
            message=(
                f"{engine} does not recognize the {role} language "
                f"{value.strip()!r}. Use a common language name (e.g. Spanish, "
                "Japanese) or an ISO code (es, ja, pt-BR)."
            ),
        )
    primary, subtags = split
    if engine == "deepl":
        if role == "source":
            # DeepL source_lang is bare (regional source → 400, verified live).
            return primary.upper()
        return "-".join([primary.upper(), *(s.upper() for s in subtags)])
    if engine == "google_translate":
        # Google accepts a regional code for either role (zh-CN, zh-TW); keep it.
        return "-".join([primary.lower(), *(_cased_subtag(s) for s in subtags)])
    raise TranslateEngineError(
        code="bad_request",
        message=f"{engine} is not a hosted translation engine.",
    )
