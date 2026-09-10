"""The translate engine axis: contract validation, the
`_recipe_engines("map.translate")` catalog branch, and its key-gated + pair-level
availability. No live provider calls (tests never touch the live network by
default); credential presence
is simulated via env / a stub project secrets store."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.translate import TranslateParams, translation_outputs
from frisket.actions.translation_languages import TRANSLATE_ENGINES
from frisket.server.action_catalog_hints import _recipe_engines


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


# ---------------------------------------------------------------------------
# contract


def test_engine_defaults_to_llm_and_preserves_today_behaviour() -> None:
    params = TranslateParams.model_validate(_params())
    assert params.engine.root == "llm"


def test_engine_accepts_the_full_roster() -> None:
    for engine in TRANSLATE_ENGINES:
        model = "anthropic/claude-haiku-4-5" if engine == "llm" else ""
        # opus_mt is no-auto (allows_auto=False): it REQUIRES an explicit source.
        extra = {"language": ["es"]} if engine == "opus_mt" else {}
        params = TranslateParams.model_validate(
            _params(engine=engine, model=model, **extra)
        )
        assert params.engine.root == engine


def test_unknown_engine_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TranslateParams.model_validate(_params(engine="argos"))


def test_llm_engine_still_requires_a_slash_model() -> None:
    with pytest.raises(ValidationError, match="provider/model"):
        TranslateParams.model_validate(_params(engine="llm", model="haiku"))


def test_non_llm_engine_accepts_a_blank_model() -> None:
    # deepl/google/opus_mt/hy_mt2 carry no model; the moved `/`-check must not
    # fire for them.
    for engine in ("deepl", "google_translate", "opus_mt", "hy_mt2"):
        # opus_mt is no-auto: it needs an explicit source to validate.
        extra = {"language": ["es"]} if engine == "opus_mt" else {}
        params = TranslateParams.model_validate(
            _params(engine=engine, model="", **extra)
        )
        assert params.model is None


def test_hosted_translate_char_pricing_entries_and_cost() -> None:
    # The translation contract: per-character pricing entries exist (marked estimated) and the
    # cost helper scales with the character count.
    from frisket.ai.external_pricing import (
        DEEPL_TRANSLATE_CHAR,
        GOOGLE_TRANSLATE_CHAR,
        external_pricing_entry,
    )
    from frisket.ops.integrations.translate_common import hosted_translate_cost

    for key in (DEEPL_TRANSLATE_CHAR, GOOGLE_TRANSLATE_CHAR):
        entry = external_pricing_entry(key)
        assert entry["unit"] == "character"
        assert entry["billable"] is True
        assert entry["external_api"] is True
        assert entry["unit_price_usd"] and entry["unit_price_usd"] > 0

    assert hosted_translate_cost("deepl", 1000) > 0
    assert hosted_translate_cost("deepl", 0) == 0.0
    # not a hosted char-billed engine -> no rate
    assert hosted_translate_cost("opus_mt", 1000) is None


def test_detected_language_output_tracks_the_flag() -> None:
    assert translation_outputs(TranslateParams(**_params())) == ("translation",)
    assert translation_outputs(
        TranslateParams(**_params(save_detected_language=True))
    ) == ("translation", "detected_language")


@pytest.mark.parametrize("text", ["hi", "x" * 50])
def test_hosted_translation_requires_consent_even_below_character_threshold(
    tmp_path, monkeypatch, text
):
    from frisket.engine.executor import run_action_spec
    from frisket.ops.integrations.translation_engine import TranslationEngine
    from frisket.engine.store import Project

    async def forbidden(*args):
        pytest.fail("unconfirmed hosted translation dispatched")

    monkeypatch.setattr(TranslationEngine, "execute", forbidden)
    # The point of this test is that a hosted translation gates on consent
    # regardless of how short the text is, so it pins zero standing
    # preapproval. Under the $2 product default the quoted cost falls under
    # the limit and dispatches, which would make `forbidden` unreachable.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    monkeypatch.setenv("FRISKET_TRANSLATE_CHAR_CONFIRM_THRESHOLD", "10")
    project = Project.create(tmp_path / "gate.frisket")
    try:
        sheet = project.add_sheet("text")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": text}], {"text": column})
        result = run_action_spec(
            project,
            {
                "action_id": "map.translate",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": ["text"], "engine": "deepl"},
                "idempotency_key": "translation-gate",
            },
            project_id="translation-gate",
        )
        assert result.status == "needs_confirmation", result
        assert result.errors[0].details["promise_set_hash"]
        assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


# ---------------------------------------------------------------------------
# catalog branch + availability


def _engines_by_id(project=None, caps=None):
    engines = _recipe_engines("map.translate", caps or {}, project=project)
    return {engine["id"]: engine for engine in engines}


def test_catalog_engine_ids_match_the_contract_roster() -> None:
    engines = _recipe_engines("map.translate", {})
    assert [engine["id"] for engine in engines] == list(TRANSLATE_ENGINES)


def test_hosted_engines_unavailable_without_keys(monkeypatch) -> None:
    for var in ("DEEPL_API_KEY", "GOOGLE_TRANSLATE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    by_id = _engines_by_id()
    assert by_id["deepl"]["available"] is False
    assert "DEEPL_API_KEY" in by_id["deepl"]["error"]
    assert by_id["google_translate"]["available"] is False
    assert "GOOGLE_TRANSLATE_API_KEY" in by_id["google_translate"]["error"]


def test_hosted_engine_available_from_env_key(monkeypatch) -> None:
    monkeypatch.setenv("DEEPL_API_KEY", "env-key")
    by_id = _engines_by_id()
    assert by_id["deepl"]["available"] is True
    assert "error" not in by_id["deepl"]


def test_hosted_engine_available_from_project_secret(monkeypatch) -> None:
    # A key saved via Settings → Secrets (project secrets store) must flip
    # availability, not just an env var; this improves on OCR's env-only
    # precedent.
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)

    class _StubProject:
        def secret_plaintext(self, name: str):
            return "project-secret" if name == "DEEPL_API_KEY" else None

    by_id = _engines_by_id(project=_StubProject())
    assert by_id["deepl"]["available"] is True
    assert by_id["google_translate"]["available"] is False


def test_opus_mt_availability_tracks_runtime_not_pairs(monkeypatch) -> None:
    # Engine availability equals runtime availability (the ``translate``
    # extra is present), NOT whether a pair is installed. Deterministic here by
    # pinning runtime_available (and installed_pairs) so the assertion does not
    # depend on whether the dev/CI machine happens to have ctranslate2 installed.
    from frisket.ops.integrations import opus_mt

    monkeypatch.setattr(opus_mt, "installed_pairs", lambda cache_root=None: [])

    # runtime ABSENT -> engine unavailable with the pip-install remediation.
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: False)
    opus = _engines_by_id()["opus_mt"]
    assert opus["tier"] == "local"
    assert opus["billable"] is False
    assert opus["available"] is False
    assert "frisket-data[standard]" in opus["error"]
    assert "models" not in opus  # no pairs installed -> no models list
    assert "downloadable_pairs" in opus  # the roster is always present

    # runtime PRESENT -> engine SELECTABLE even with zero pairs installed; the
    # missing pair is a run-gating state (surfaced by the pair picker), not
    # engine unavailability (the first-install-deadlock fix).
    monkeypatch.setattr(opus_mt, "runtime_available", lambda: True)
    opus2 = _engines_by_id()["opus_mt"]
    assert opus2["available"] is True
    assert opus2.get("error") is None


def test_hy_mt2_is_experimental_opt_in_without_quality_claims(monkeypatch) -> None:
    # Availability equals runtime availability (the translate-gguf extra),
    # matching the opus_mt semantics. Pin it so the assertion does
    # not depend on whether the machine has llama-cpp-python installed.
    from frisket.ops.integrations import hy_mt2

    monkeypatch.setattr(hy_mt2, "is_installed", lambda cache_root=None: False)
    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: False)
    hy = _engines_by_id()["hy_mt2"]
    assert hy["tier"] == "local"
    assert hy["billable"] is False
    assert hy["available"] is False  # runtime absent -> unavailable + remediation
    assert "frisket-data[translate-gguf]" in hy["error"]
    assert "experimental" in hy["label"].lower()
    # Experimental copy makes no unsupported "higher quality"/"better" claim.
    blob = f"{hy['label']} {hy.get('error', '')}".lower()
    assert "higher quality" not in blob
    assert "better" not in blob

    # runtime PRESENT -> selectable (the model is a run-gating pull, not engine
    # unavailability); still no quality claim.
    monkeypatch.setattr(hy_mt2, "runtime_available", lambda: True)
    hy2 = _engines_by_id()["hy_mt2"]
    assert hy2["available"] is True
    assert hy2.get("error") is None
    assert "experimental" in hy2["label"].lower()


def _clear_provider_env(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_llm_engine_available_with_a_provider_key(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    assert _engines_by_id()["llm"]["available"] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert _engines_by_id()["llm"]["available"] is True


def test_llm_availability_consults_project_keys_not_just_env(
    monkeypatch, tmp_path
) -> None:
    # Keys configured through the product UI (per-project keys /
    # workspace AI-Providers file) must flip llm availability, exactly like
    # execution resolves them — env-only regressed this.
    from frisket.server.action_catalog_hints import _configured_llm_providers

    _clear_provider_env(monkeypatch)

    class _ProjectWithKeys:
        path = tmp_path / "proj.frisket"

        def provider_model_keys(self):
            return {"anthropic": "sk-proj"}

    assert _configured_llm_providers(None) == set()
    assert "anthropic" in _configured_llm_providers(_ProjectWithKeys())
    by_id = _recipe_engines("map.translate", {}, project=_ProjectWithKeys())
    assert next(e for e in by_id if e["id"] == "llm")["available"] is True


def test_llm_availability_consults_workspace_provider_file(
    monkeypatch, tmp_path
) -> None:
    # A key saved to the workspace AI-Providers file
    # (<root>/.frisket/provider_keys.json) also flips availability.
    from frisket.server import provider_config
    from frisket.server.action_catalog_hints import _configured_llm_providers

    _clear_provider_env(monkeypatch)
    provider_config.save_local_provider_key(tmp_path, "openai", "sk-file")

    class _ProjectInWorkspace:
        path = tmp_path / "proj.frisket"  # parent == workspace root == tmp_path

        def provider_model_keys(self):
            return {}

    assert "openai" in _configured_llm_providers(_ProjectInWorkspace())
