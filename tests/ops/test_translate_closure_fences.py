"""Closure fences for ``map.translate``.

No-ignore rule: every accepted knob is either consumed or rejected — never
accepted, hashed into identity, receipted, and silently dropped.

- C2: `context` joins the params hash and the receipt stamps `context_hash`,
  so the rendered LLM prompt MUST actually carry it (like map.summarize).
- C3: hy_mt2 declares `auto_only` — it consumes no source hint — and the
  contract rejects a supplied `language` instead of ignoring it.
- L7: a non-blank `model` on a non-LLM engine and `save_detected_language`
  on an engine that can never honour it are rejected, not hash-split no-ops.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from frisket.actions.translate import TranslateParams, translate_prompt
from frisket.actions.types import Row
from frisket.actions.translation_languages import translate_language_declaration
from frisket.actions.translation_languages import TRANSLATE_ENGINES
from frisket.ops.integrations.translation_engine import TranslationEngine


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


# --------------------------------------------------------------------------- #
# C2 — context is consumed by the LLM prompt


def _rendered_text(call) -> str:
    parts = []
    for message in call.messages:
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.append(json.dumps(content))
    return "\n".join(parts)


def test_llm_render_carries_spec_context():
    context = "Rows are countries from the tariff search workflow."
    spec = _params(context=context)
    call = translate_prompt(TranslateParams(**spec), Row({"statement": "Hola"}))
    assert context in _rendered_text(call)


def test_llm_render_without_context_is_unchanged():
    call = translate_prompt(TranslateParams(**_params()), Row({"statement": "Hola"}))
    text = _rendered_text(call)
    assert "Dataset context" not in text
    # the base system prompt is intact
    assert "precise document translator" in text


def test_nonblank_context_rejected_on_every_non_llm_engine():
    # Only the LLM prompt path consumes `context`. The shipped deepl/google adapters
    # send no context/glossary field and opus_mt/hy_mt2 have no prompt, so
    # a nonblank context on those engines is accept-and-ignore — identity
    # and receipt provenance change with no behavior change. Fenced per
    # engine, fail-closed.
    for engine in TRANSLATE_ENGINES:
        if engine == "llm":
            continue
        overrides: dict = {"engine": engine, "model": "", "context": "tariff rows"}
        if engine == "opus_mt":
            overrides["language"] = ["es"]
        with pytest.raises(ValidationError) as exc:
            TranslateParams.model_validate(_params(**overrides))
        assert "require the LLM" in str(exc.value), engine


def test_blank_context_stays_valid_on_non_llm_engines():
    # Blank/absent context is the stored-spec default — identity preserved.
    p = TranslateParams.model_validate(_params(engine="deepl", model=""))
    assert p.context == ""
    p = TranslateParams.model_validate(_params(engine="deepl", model="", context="   "))
    assert p.context == "   "
    # ...and the LLM engine still accepts a nonblank context (consumed by
    # render, per the C2 fence above).
    p = TranslateParams.model_validate(_params(context="tariff rows"))
    assert p.context == "tariff rows"


# --------------------------------------------------------------------------- #
# C3 — declared language mode matches consumption, per engine


def test_auto_only_engine_rejects_source_language():
    # hy_mt2 consumes no source hint (its prompt names only the target): a
    # supplied language is a stale selection, rejected loudly.
    with pytest.raises(ValidationError) as exc:
        TranslateParams.model_validate(
            _params(engine="hy_mt2", model="", language=["es"])
        )
    assert "invalid_language_selection" in str(exc.value)
    ok = TranslateParams.model_validate(_params(engine="hy_mt2", model=""))
    assert ok.language == []


def test_every_engine_mode_is_enforced_not_advisory():
    # single: >1 sources rejected; auto_only: any source rejected. This is the
    # per-engine "declared mode matches consumption" fence — a future engine
    # whose declaration drifts from its params handling fails here.
    for engine in TRANSLATE_ENGINES:
        decl = translate_language_declaration(engine)
        model = "anthropic/claude-haiku-4-5" if engine == "llm" else ""
        save = {}
        if decl.mode == "auto_only":
            with pytest.raises(ValidationError):
                TranslateParams.model_validate(
                    _params(engine=engine, model=model, language=["es"], **save)
                )
        elif decl.mode == "single":
            with pytest.raises(ValidationError):
                TranslateParams.model_validate(
                    _params(engine=engine, model=model, language=["es", "fr"], **save)
                )
        else:  # pragma: no cover - no fixed/multi translate engines exist
            pytest.fail(f"unexpected translate language mode {decl.mode!r}")


# --------------------------------------------------------------------------- #
# L7 — identity-splitting no-op knobs are rejected


def test_non_llm_engine_rejects_supplied_model():
    for engine in ("deepl", "google_translate", "hy_mt2"):
        with pytest.raises(ValidationError) as exc:
            TranslateParams.model_validate(
                _params(engine=engine, model="anthropic/claude-haiku-4-5")
            )
        assert "require the LLM" in str(exc.value)


def test_non_llm_engine_accepts_blank_or_absent_model():
    # Blank/absent stays valid — existing saved specs keep their identity.
    p = TranslateParams.model_validate(_params(engine="deepl", model=""))
    assert p.model is None
    params = _params(engine="deepl")
    del params["model"]
    assert TranslateParams.model_validate(params).model is None


def test_save_detected_language_rejected_where_unhonourable():
    # hy_mt2 auto-detects but cannot report and takes no explicit source: the
    # column could only ever be blank -> rejected.
    with pytest.raises(ValidationError) as exc:
        TranslateParams.model_validate(
            _params(engine="hy_mt2", model="", save_detected_language=True)
        )
    assert "detected_language_unsupported_engine" in str(exc.value)


@pytest.mark.asyncio
async def test_opus_mt_execution_writes_explicit_source_as_detected(monkeypatch):
    """The validator keeps
    `save_detected_language=True` for OPUS-MT (detects=False) on theory
    that its REQUIRED explicit source is the honest value. This proves the
    execution side of that theory: the opus_mt branch passes the resolved
    src_code as detected_raw into _shaped_result, so the opt-in
    `{output}_detected_language` column really carries the explicit source
    — the exception is exercised end to end."""
    from frisket.ops.base import OpContext
    from frisket.ops.integrations import opus_mt

    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    monkeypatch.setattr(
        opus_mt,
        "translate_texts",
        lambda src, tgt, texts, *, cache_root=None: ["Hola Mundo."],
    )
    out = await TranslationEngine().execute(
        {"statement": "Hello World."},
        {
            "engine": "opus_mt",
            "target_language": "Spanish",
            "language": ["en"],
            "output_name": "es",
            "save_detected_language": True,
        },
        OpContext(project=None, http=None),
    )
    assert out == {"translation": "Hola Mundo.", "detected_language": "en"}


def test_save_detected_language_valid_where_honourable():
    # Detecting engines report it; opus_mt requires an explicit source, which
    # is the honest recorded value.
    for overrides in (
        {"engine": "llm", "save_detected_language": True},
        {"engine": "deepl", "model": "", "save_detected_language": True},
        {
            "engine": "opus_mt",
            "model": "",
            "language": ["es"],
            "save_detected_language": True,
        },
    ):
        p = TranslateParams.model_validate(_params(**overrides))
        assert p.save_detected_language is True
