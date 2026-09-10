"""Translate language reconciliation: the shared LanguageDeclaration
now drives map.translate's SOURCE language, exactly as it drives transcribe.

Covers the canonical list wire param (`language`), the per-engine
declarations in ui_hints, the no-auto (Opus-MT) contract expression, the recipe's
list-first read, and the action-level target preflight. Mirrors
tests/test_transcribe_language_declaration.py in style."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.translate import TranslateParams
from frisket.actions.translation_languages import translate_language_declaration
from frisket.contracts.actions.schemas._language import AUTO_SENTINEL
from frisket.ops.integrations.translate_common import (
    SUPPORTED_LANGUAGE_NAMES,
    _SUPPORTED_ISO,
)
from frisket.server.action_catalog_hints import (
    _recipe_engines,
    action_catalog_payload_with_launcher_hints,
)


# --------------------------------------------------------------------------- #
# per-engine declarations


def test_translate_engine_declarations_match_real_capability() -> None:
    # llm + hosted: single source or Auto, report detection, broad choices.
    for engine in ("llm", "deepl", "google_translate"):
        decl = translate_language_declaration(engine)
        assert decl.mode == "single"
        assert decl.detects is True
        assert decl.allows_auto is True
        assert decl.choices  # populated Auto-first picker source
        assert all(c.value != AUTO_SENTINEL for c in decl.choices)
    # opus_mt: pair-based, no language ID -> single, NO auto, no detection, and
    # (with zero pairs installed) no choices yet.
    opus = translate_language_declaration("opus_mt")
    assert opus.mode == "single"
    assert opus.allows_auto is False
    assert opus.detects is False
    assert opus.choices is None
    # hy_mt2: experimental local — consumes no source hint (its prompt names
    # only the target) and does not report detection: auto_only, honestly.
    hy = translate_language_declaration("hy_mt2")
    assert hy.mode == "auto_only"
    assert hy.detects is False
    assert hy.choices is None
    # unknown engine falls back to the open-ended LLM declaration
    assert translate_language_declaration("argos").mode == "single"


def test_supported_language_names_pin_the_roster() -> None:
    # The picker choices are seeded from this table; its keys must be exactly the
    # supported ISO set so the two never drift (translate_common).
    assert set(SUPPORTED_LANGUAGE_NAMES) == _SUPPORTED_ISO
    # DeepL sources are BARE codes (the supported-source contract): no regional variant in the roster.
    assert all("-" not in code for code in SUPPORTED_LANGUAGE_NAMES)


# --------------------------------------------------------------------------- #
# catalog projection


def test_recipe_engines_emit_translate_language() -> None:
    engines = {e["id"]: e for e in _recipe_engines("map.translate", {})}
    assert set(engines) == {"llm", "deepl", "google_translate", "opus_mt", "hy_mt2"}
    for engine_id, engine in engines.items():
        assert "language" in engine
        expected_mode = "auto_only" if engine_id == "hy_mt2" else "single"
        assert engine["language"]["mode"] == expected_mode
    assert engines["opus_mt"]["language"]["allows_auto"] is False
    assert engines["llm"]["language"]["allows_auto"] is True
    assert engines["deepl"]["language"]["detects"] is True


def test_translate_language_survives_catalog_projection() -> None:
    payload = action_catalog_payload_with_launcher_hints({})
    entry = next(a for a in payload["actions"] if a["kind"] == "map.translate")
    engines = {e["id"]: e for e in entry["ui_hints"]["engines"]}
    assert engines["llm"]["language"]["choices"]
    assert engines["opus_mt"]["language"]["allows_auto"] is False


# --------------------------------------------------------------------------- #
# wire param + canonicalization + engine-mode enforcement


def _params(**overrides):
    base = {
        "source": ["statement"],
        "model": "anthropic/claude-haiku-4-5",
        "target_language": "Spanish",
    }
    base.update(overrides)
    if base.get("model") == "":
        base["model"] = None
    return base


def test_params_language_canonicalized_to_list() -> None:
    p = TranslateParams.model_validate(_params(language=["es", "es"]))
    assert p.language == ["es"]
    for spelling in (None, [], ["auto"], ["auto", " "]):
        assert TranslateParams.model_validate(_params(language=spelling)).language == []
    # absent field -> [] (validate_default runs the canonicalizer)
    assert TranslateParams.model_validate(_params()).language == []


def test_single_engine_rejects_multi_source() -> None:
    with pytest.raises(ValidationError) as exc:
        TranslateParams.model_validate(_params(engine="llm", language=["en", "es"]))
    assert "invalid_language_selection" in str(exc.value)


def test_no_auto_engine_requires_explicit_source() -> None:
    # opus_mt cannot auto-detect: empty/auto is rejected, an explicit code is ok.
    with pytest.raises(ValidationError) as exc:
        TranslateParams.model_validate(_params(engine="opus_mt", model="", language=[]))
    assert "invalid_language_selection" in str(exc.value)
    ok = TranslateParams.model_validate(
        _params(engine="opus_mt", model="", language=["es"])
    )
    assert ok.language == ["es"]


def test_canonical_language_feeds_idempotency_hash() -> None:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_request_hash

    def h(**overrides):
        bound = typed_action_for_request(
            {
                "action_id": "map.translate",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": _params(**overrides),
                "idempotency_key": "translation-language@1",
            }
        )
        return typed_request_hash(bound)

    # absent / [] / ["auto"] all hash identically.
    base = h()  # language absent
    assert base == h(language=[]) == h(language=["auto"])
    # a real source differs from auto
    assert h(language=["fr"]) != base


def test_precheck_rejects_non_list_language() -> None:
    with pytest.raises(ValueError):
        TranslateParams.model_validate(_params(language="es"))


# --------------------------------------------------------------------------- #
# recipe list-first read


def test_recipe_source_language_extracts_list_first() -> None:
    from frisket.ops.integrations.translation_engine import TranslationEngine

    src = TranslationEngine._source_language
    assert src({"language": ["fr"]}) == "fr"
    assert src({"language": []}) is None
    assert src({}) is None
    # The legacy scalar `source_language` is gone, no back-compat on the wire.
    assert src({"source_language": "de"}) is None


# --------------------------------------------------------------------------- #
# action-level target preflight (one error, not per-row)


def test_target_preflight_fails_once_for_bad_hosted_target() -> None:
    with pytest.raises(ValueError, match="invalid_target"):
        TranslateParams.model_validate(
            _params(engine="deepl", model="", target_language="Klingon")
        )


def test_target_preflight_passes_a_supported_hosted_target() -> None:
    assert (
        TranslateParams.model_validate(
            _params(engine="deepl", model="", target_language="Spanish")
        ).target_language
        == "Spanish"
    )


def test_llm_target_is_open_ended_no_preflight() -> None:
    # LLM takes any language name — an exotic target must NOT be preflighted out.
    assert (
        TranslateParams.model_validate(
            _params(engine="llm", target_language="Klingon")
        ).target_language
        == "Klingon"
    )
