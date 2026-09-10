from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.ner import NerParams, NerModelOutput
from frisket.actions.system import validate_root_action
from typed_model_fixtures import model_request
from frisket.server.action_catalog_hints import project_action_catalog_launcher_hints


_UNCONFIGURED_SIDECAR = {
    "configured": False,
    "available": False,
    "engines": [],
    "error": None,
}


def _ner_engines(
    sidecar_capabilities=_UNCONFIGURED_SIDECAR,
    *,
    project=None,
    org_provider_keys=None,
):
    hints = project_action_catalog_launcher_hints(
        sidecar_capabilities,
        project=project,
        org_provider_keys=org_provider_keys,
    )
    return hints["map.ner"]["engines"]


def _by_id(engines, engine_id):
    return next(e for e in engines if e["id"] == engine_id)


def _base_params(**overrides):
    return {"source": ["body"], "labels": ["person", "organization"], **overrides}


@pytest.mark.parametrize("engine", ["spacy", "gliner", "llm"])
def test_engine_accepts_all_three_values(engine):
    options = {"engine": engine}
    if engine == "llm":
        options["model"] = "openai/gpt-4.1-mini"
    assert NerParams.model_validate(_base_params(**options)).engine.root == engine


def test_engine_default_is_spacy():
    assert NerParams.model_validate(_base_params()).engine.root == "spacy"


@pytest.mark.parametrize(
    "options",
    [
        {"engine": "unknown"},
        {"engine": "llm"},
        {"engine": "spacy", "model": "openai/gpt-4.1-mini"},
    ],
)
def test_engine_rejects_unsupported_or_incomplete_selection(options):
    with pytest.raises(ValidationError):
        NerParams.model_validate(_base_params(**options))


@pytest.mark.parametrize("labels", [[], [""], ["person", "person"]])
def test_empty_or_ambiguous_labels_fail_before_dispatch(labels):
    with pytest.raises(ValidationError):
        NerParams.model_validate(_base_params(labels=labels))


@pytest.mark.parametrize("label", ["address", "company", "city", "email", "phone"])
def test_spacy_rejects_labels_it_could_never_emit(label):
    with pytest.raises(ValidationError, match=label):
        NerParams.model_validate(_base_params(labels=["person", label]))


def test_spacy_accepts_every_type_it_can_actually_emit():
    from frisket.ops.spacy_ner import SPACY_CANONICAL_TYPES

    assert NerParams.model_validate(_base_params(labels=sorted(SPACY_CANONICAL_TYPES)))
    assert NerParams.model_validate(
        _base_params(labels=["PERSON", "ORG", "GPE", "WORK_OF_ART"])
    )


def test_spacy_supported_types_are_derived_from_the_pipeline_tag_set():
    from frisket.ops.spacy_ner import (
        SPACY_CANONICAL_TYPES,
        SPACY_ONTONOTES_TAGS,
        SPACY_ONTONOTES_TYPES,
    )

    assert SPACY_CANONICAL_TYPES == {
        SPACY_ONTONOTES_TYPES.get(tag, tag.lower()) for tag in SPACY_ONTONOTES_TAGS
    }


@pytest.mark.parametrize("engine", ["gliner", "llm"])
def test_zero_shot_engines_accept_any_label(engine):
    options = {"engine": engine, "labels": ["address", "company", "city"]}
    if engine == "llm":
        options["model"] = "anthropic/claude-haiku-4-5"
    assert NerParams.model_validate(_base_params(**options))


def test_ner_scope_and_confirmation_belong_to_request():
    request = model_request(
        {
            "action_kind": "map.ner",
            "sheet_id": 3,
            "input_columns": ["body"],
            "labels": ["person"],
            "row_ids": [7],
        }
    )
    assert request.scope.row_ids == (7,)
    assert request.confirmation is None
    assert "row_ids" not in request.params and "confirmed" not in request.params
    wrong = request.model_dump(mode="json")
    wrong["params"]["row_ids"] = [9]
    assert not validate_root_action(wrong).ok


def test_llm_request_schema_never_asks_model_for_fingerprint():
    schema = NerModelOutput.model_json_schema()
    assert "fingerprint" not in schema["$defs"]["EntityModelValue"]["properties"]


def test_extra_instructions_defaults_empty_and_strips():
    assert NerParams.model_validate(_base_params()).extra_instructions == ""
    assert (
        NerParams.model_validate(
            _base_params(extra_instructions="  People only. ")
        ).extra_instructions
        == "People only."
    )


def test_ner_catalog_lists_three_engines():
    engines = _ner_engines()
    ids = [e["id"] for e in engines]
    assert ids == ["spacy", "gliner", "llm"]


def test_spacy_catalog_entry_is_local_and_capabilities_driven(monkeypatch):
    """Uninstalled spaCy reports honest unavailable + an install hint (no
    heavy-downloads-in-lanes: the probe never downloads or even loads a
    model, it is a presence check)."""
    monkeypatch.delenv("FRISKET_SPACY_MODEL", raising=False)
    from frisket.ops.spacy_ner import spacy_available

    engines = _ner_engines()
    spacy_entry = _by_id(engines, "spacy")
    assert spacy_entry["tier"] == "local"
    assert spacy_entry["billable"] is False
    available, error = spacy_available()
    assert spacy_entry["available"] == available
    if not available:
        assert spacy_entry["error"]
        assert "spacy" in spacy_entry["error"].lower()


def test_spacy_catalog_advertises_runtime_model_when_library_is_installed(monkeypatch):
    from frisket.ai.models import artifact_manifest, spacy_model
    from frisket.ai.models.spacy_model import SpacyModelState
    from frisket.ops import spacy_ner

    monkeypatch.setattr(
        spacy_ner.importlib.util,
        "find_spec",
        lambda name: object() if name == "spacy" else None,
    )
    monkeypatch.setattr(
        spacy_model,
        "model_state",
        lambda: SpacyModelState(
            status="not_downloaded",
            source=None,
            ref=artifact_manifest.SPACY_MODEL_REF,
            detail="not downloaded",
        ),
    )

    spacy_entry = _by_id(_ner_engines(), "spacy")
    assert spacy_entry["available"] is False
    downloadable = spacy_entry["downloadable_models"]
    assert len(downloadable) == 1
    assert downloadable[0]["ref"] == artifact_manifest.SPACY_MODEL_REF
    assert downloadable[0]["license"] == "MIT"
    assert downloadable[0]["size"] == 12_806_118


def test_gliner_catalog_entry_unchanged_sidecar_shape():
    """gliner path stays behavior-identical (pinned) -- still the sidecar
    probe over /ner, unconfigured sidecar -> honest unavailable."""
    engines = _ner_engines()
    gliner_entry = _by_id(engines, "gliner")
    assert gliner_entry["tier"] == "sidecar"
    assert gliner_entry["billable"] is False
    assert gliner_entry["available"] is False
    assert gliner_entry["error"]


def test_llm_catalog_entry_reflects_all_effective_provider_keys(monkeypatch, tmp_path):
    for env in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    engines = _ner_engines()
    llm_entry = _by_id(engines, "llm")
    assert llm_entry["tier"] == "hosted"
    assert llm_entry["billable"] is True
    assert llm_entry["available"] is False
    assert llm_entry["error"]

    class _ProjectWithProviderKey:
        path = tmp_path / "catalog.frisket"

        def provider_model_keys(self):
            return {"openai": "project-key"}

    for engines in (
        _ner_engines(project=_ProjectWithProviderKey()),
        _ner_engines(org_provider_keys={"openai": "org-key"}),
    ):
        llm_entry = _by_id(engines, "llm")
        assert llm_entry["available"] is True
        assert llm_entry.get("error") is None
