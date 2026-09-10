from __future__ import annotations

import importlib.util

import pytest

from frisket.ops.ner_local import LocalNerExtractor
from frisket.ops.base import OpContext
from frisket.ops.spacy_ner import (
    SPACY_ONTONOTES_TYPES,
    requested_spacy_types,
    run_spacy_ner,
    spacy_available,
)
from frisket.ai.models.spacy_model import SpacyModelState


class _FakeEnt:
    def __init__(self, text: str, label: str, start_char: int, end_char: int) -> None:
        self.text = text
        self.label_ = label
        self.start_char = start_char
        self.end_char = end_char


class _FakeDoc:
    def __init__(self, ents: list[_FakeEnt]) -> None:
        self.ents = ents


class _FakePipeline:
    """Stands in for a real spaCy `Language`: callable, returns a doc with
    `.ents`. No spaCy import anywhere in this class."""

    def __init__(self, ents_by_text: dict[str, list[_FakeEnt]]) -> None:
        self._ents_by_text = ents_by_text
        self.calls: list[str] = []

    def __call__(self, text: str) -> _FakeDoc:
        self.calls.append(text)
        return _FakeDoc(self._ents_by_text.get(text, []))


# ---------- availability: honest, cheap, no downloads ----------


def test_spacy_availability_needs_library_and_model(monkeypatch):
    available, error = spacy_available()
    assert isinstance(available, bool)
    if not available:
        assert error
        assert "spacy" in error.lower() or "spaCy" in error


def test_spacy_availability_never_imports_spacy_module():
    """The probe must be a presence check only (find_spec), never a real
    `import spacy` / `spacy.load(...)` -- otherwise every catalog fetch pays
    a model-load cost and the no-heavy-downloads rule is at risk of a lane
    accidentally triggering a first-use download."""
    import sys

    was_present = "spacy" in sys.modules
    spacy_available(model="definitely-not-a-real-spacy-model-xyz")
    if not was_present:
        assert "spacy" not in sys.modules


def test_spacy_unavailable_reports_install_hint_for_missing_library():
    available, error = spacy_available(model="en_core_web_sm")
    if importlib.util.find_spec("spacy") is None:
        assert available is False
        assert "standard" in error
        assert "pip install" in error


def test_spacy_availability_reports_managed_model_hash_mismatch(monkeypatch):
    from frisket.ai.models import spacy_model
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
            status="hash_mismatch",
            source="managed_cache",
            ref="spacy:en_core_web_sm@3.8.0",
            detail="the cached spaCy model does not match the pinned SHA256",
        ),
    )

    available, error = spacy_available()
    assert available is False
    assert "SHA256" in (error or "")
    assert "spacy:en_core_web_sm@3.8.0" in (error or "")


# ---------- pure logic: fake pipeline, no spaCy required ----------


def test_run_spacy_ner_maps_ontonotes_types_to_canonical():
    pipeline = _FakePipeline(
        {
            "Ada Lovelace visited London.": [
                _FakeEnt("Ada Lovelace", "PERSON", 0, 12),
                _FakeEnt("London", "GPE", 21, 27),
            ]
        }
    )
    out = run_spacy_ner(pipeline, ["Ada Lovelace visited London."])
    assert out == [
        [
            {
                "text": "Ada Lovelace",
                "type": "person",
                "start": 0,
                "end": 12,
                "score": None,
            },
            {
                "text": "London",
                "type": "location",
                "start": 21,
                "end": 27,
                "score": None,
            },
        ]
    ]


def test_run_spacy_ner_score_always_null():
    """spaCy has no per-span confidence -- score is honestly null, never a
    fabricated number."""
    pipeline = _FakePipeline({"x": [_FakeEnt("x", "ORG", 0, 1)]})
    out = run_spacy_ner(pipeline, ["x"])
    assert out[0][0]["score"] is None


def test_run_spacy_ner_filters_by_requested_labels():
    pipeline = _FakePipeline(
        {
            "text": [
                _FakeEnt("Acme", "ORG", 0, 4),
                _FakeEnt("2024", "DATE", 5, 9),
            ]
        }
    )
    out = run_spacy_ner(pipeline, ["text"], labels=["organization"])
    assert [e["type"] for e in out[0]] == ["organization"]


def test_run_spacy_ner_no_filter_keeps_every_type():
    pipeline = _FakePipeline(
        {"text": [_FakeEnt("Acme", "ORG", 0, 4), _FakeEnt("2024", "DATE", 5, 9)]}
    )
    out = run_spacy_ner(pipeline, ["text"])
    assert {e["type"] for e in out[0]} == {"organization", "date"}


def test_run_spacy_ner_empty_text_skips_pipeline_call():
    pipeline = _FakePipeline({})
    out = run_spacy_ner(pipeline, ["", "   "])
    assert out == [[], []]
    assert pipeline.calls == []


def test_requested_spacy_types_canonicalizes_and_none_means_no_filter():
    assert requested_spacy_types(None) is None
    assert requested_spacy_types([]) is None
    assert requested_spacy_types(["People", "place"]) == {"person", "location"}


def test_ontonotes_map_covers_core_pinned_types():
    assert SPACY_ONTONOTES_TYPES["PERSON"] == "person"
    assert SPACY_ONTONOTES_TYPES["ORG"] == "organization"
    assert SPACY_ONTONOTES_TYPES["GPE"] == "location"
    assert SPACY_ONTONOTES_TYPES["LOC"] == "location"


# ---------- NerRecipe wiring: honest failure when spacy is unavailable ----------


@pytest.mark.asyncio
async def test_ner_recipe_spacy_engine_raises_honest_error_when_unavailable(
    monkeypatch,
):
    from frisket.ops import spacy_ner as spacy_ner_module

    monkeypatch.setattr(
        spacy_ner_module, "spacy_available", lambda model=None: (False, "not installed")
    )
    with pytest.raises(RuntimeError, match="not installed"):
        await LocalNerExtractor("spacy", OpContext()).extract(
            "Ada Lovelace wrote notes.",
            labels=["person"],
            threshold=0.5,
        )


@pytest.mark.asyncio
async def test_ner_recipe_spacy_engine_calls_run_spacy_ner_default(monkeypatch):
    monkeypatch.setattr(
        "frisket.ops.spacy_ner.spacy_available", lambda model=None: (True, None)
    )
    captured = {}

    def fake_run(texts, labels=None, *, model=None):
        captured["texts"] = texts
        captured["labels"] = labels
        return [
            [{"text": "Ada", "type": "person", "start": 0, "end": 3, "score": None}]
        ]

    monkeypatch.setattr("frisket.ops.spacy_ner.run_spacy_ner_default", fake_run)

    out = await LocalNerExtractor("spacy", OpContext()).extract(
        "Ada wrote notes.",
        labels=["person"],
        threshold=0.5,
    )
    # The stored shape: canonicalization derives `fingerprint` server-side.
    assert out == [
        {
            "text": "Ada",
            "type": "person",
            "start": 0,
            "end": 3,
            "score": None,
            "fingerprint": "ada",
        }
    ]
    assert captured["texts"] == ["Ada wrote notes."]
    assert captured["labels"] == ["person"]


# ---------- installed-state (skips without spacy + the model) ----------


def test_spacy_reads_fixture_real():
    """INSTALLED-STATE (skips without spacy + en_core_web_sm): a real spaCy
    pipeline tags a well-known person entity. Un-gap by installing
    frisket-data[standard] + `python -m spacy download en_core_web_sm` (a
    separate explicit action, never triggered by this test)."""
    pytest.importorskip("spacy")
    if not spacy_available()[0]:
        pytest.skip("spacy model not installed")
    from frisket.ops.spacy_ner import run_spacy_ner_default

    out = run_spacy_ner_default(["Barack Obama was born in Hawaii."])
    types = {e["type"] for e in out[0]}
    assert "person" in types or "location" in types
