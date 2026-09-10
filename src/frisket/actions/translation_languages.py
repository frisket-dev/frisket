"""Source language declarations owned by the translation action domain."""

from frisket.contracts.actions.schemas._language import (
    LanguageDeclaration,
    choices_from_codes,
)
from frisket.ops.integrations.translate_common import SUPPORTED_LANGUAGE_NAMES


# Catalog tests keep this roster aligned with availability hints.
TRANSLATE_ENGINES: tuple[str, ...] = (
    "llm",
    "deepl",
    "google_translate",
    "opus_mt",
    "hy_mt2",
)


# Runtime normalization accepts valid free-text and regional codes beyond the picker.
_TRANSLATE_SOURCE_CHOICES = choices_from_codes(SUPPORTED_LANGUAGE_NAMES)

_LLM_SOURCE = LanguageDeclaration(
    mode="single", default="auto", choices=_TRANSLATE_SOURCE_CHOICES, detects=True
)
_HOSTED_SOURCE = LanguageDeclaration(
    mode="single", default="auto", choices=_TRANSLATE_SOURCE_CHOICES, detects=True
)
_OPUS_MT_SOURCE = LanguageDeclaration(
    mode="single", default="", choices=None, detects=False, allows_auto=False
)
_HY_MT2_SOURCE = LanguageDeclaration(mode="auto_only", detects=False)

TRANSLATE_ENGINE_LANGUAGE: dict[str, LanguageDeclaration] = {
    "llm": _LLM_SOURCE,
    "deepl": _HOSTED_SOURCE,
    "google_translate": _HOSTED_SOURCE,
    "opus_mt": _OPUS_MT_SOURCE,
    "hy_mt2": _HY_MT2_SOURCE,
}


def translate_language_declaration(engine: str) -> LanguageDeclaration:
    """Return source-language support, defaulting unknown ids to LLM support."""
    return TRANSLATE_ENGINE_LANGUAGE.get(engine, _LLM_SOURCE)
