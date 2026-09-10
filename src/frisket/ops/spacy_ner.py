from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Callable

DEFAULT_SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL = os.environ.get("FRISKET_SPACY_MODEL", DEFAULT_SPACY_MODEL)

# OntoNotes 5 NER tag set (the label set `en_core_web_sm`/`en_core_web_trf`
# ship) -> a lowercase canonical type. PERSON/ORG/GPE/LOC route through the
# same words the shared alias table in `ops/entities.py` already knows
# (person/organization/location); the rest are OntoNotes-only concepts that
# alias table has no opinion on, so they pass through as their own lowercase
# type via `ops.entities.canonicalize_entity_type`'s fallback -- this map
# exists only to give GPE/LOC/PERSON/ORG the SAME canonical spelling
# gliner/llm produce, not to relabel everything.
SPACY_ONTONOTES_TYPES: dict[str, str] = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
}

# Every raw tag an `en_core_web_*` pipeline can emit. spaCy's schema is CLOSED:
# it is a trained tagger, not a zero-shot extractor, so a requested type
# outside this set can never match and the run would write `[]` into every row
# -- indistinguishable from clean data. This is the list `map.ner` validates
# `labels` against when engine="spacy".
SPACY_ONTONOTES_TAGS: tuple[str, ...] = (
    "PERSON",
    "NORP",
    "FAC",
    "ORG",
    "GPE",
    "LOC",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
    "DATE",
    "TIME",
    "PERCENT",
    "MONEY",
    "QUANTITY",
    "ORDINAL",
    "CARDINAL",
)

# The canonical types spaCy can actually produce, derived by the SAME
# tag -> canonical transform `run_spacy_ner` applies to a live span, so a
# validator built on this can never accept a label the extractor would drop.
SPACY_CANONICAL_TYPES: frozenset[str] = frozenset(
    SPACY_ONTONOTES_TYPES.get(tag, tag.lower()) for tag in SPACY_ONTONOTES_TAGS
)

_PIPELINE_CACHE: dict[str, Any] = {}
_PIPELINE_LOCK = threading.Lock()


def spacy_available(model: str | None = None) -> tuple[bool, str | None]:
    """Whether the local spaCy engine can actually run: the `spacy` library
    must import AND the named pipeline model must be available. Either half
    alone is not a working engine -- honest-fail with an install hint naming
    exactly what is missing, never a bare/opaque unavailable (mirrors
    `tesseract_available`).

    The default model is either Frisket's checksum-verified managed artifact
    or a manually installed Python package. Custom ``FRISKET_SPACY_MODEL``
    values retain the package-only behavior. This never calls ``spacy.load``
    and never reaches the network."""
    target_model = model or SPACY_MODEL
    library = importlib.util.find_spec("spacy") is not None
    if not library:
        return False, (
            "spaCy is not installed. Install with "
            "pip install 'frisket-data[standard]', then confirm the managed "
            "download for the pinned pipeline model."
        )
    if target_model == DEFAULT_SPACY_MODEL:
        from frisket.ai.models.spacy_model import model_state

        state = model_state()
        if state.status == "present":
            return True, None
        return False, (
            f"spaCy is installed but the '{target_model}' model is unavailable: "
            f"{state.detail}. Pinned artifact: {state.ref}. For a manual "
            f"installation, run: python -m spacy download {target_model}."
        )
    if importlib.util.find_spec(target_model) is None:
        return False, (
            f"spaCy is installed but the custom '{target_model}' model is not "
            f"installed. Run: python -m spacy download {target_model}."
        )
    return True, None


def _load_pipeline(model: str) -> Any:
    load_target: Any = model
    cache_key = model
    if model == DEFAULT_SPACY_MODEL:
        from frisket.ai.models.spacy_model import (
            ensure_loadable_model_path,
            model_state,
        )

        state = model_state()
        if state.status != "present":
            from frisket.ai.models.spacy_model import SpacyModelUnavailable

            raise SpacyModelUnavailable(state.detail)
        if state.source == "managed_cache":
            load_target = ensure_loadable_model_path()
            cache_key = str(load_target)
    with _PIPELINE_LOCK:
        pipeline = _PIPELINE_CACHE.get(cache_key)
        if pipeline is None:
            import spacy

            pipeline = spacy.load(load_target)
            _PIPELINE_CACHE[cache_key] = pipeline
        return pipeline


def requested_spacy_types(labels: list[str] | None) -> set[str] | None:
    """Canonicalize the requested `labels` filter into a set of canonical
    types. spaCy's schema is FIXED: labels filter the built-in OntoNotes set
    rather than naming arbitrary zero-shot labels. None (no filter) means
    "keep every type the pipeline reports"."""
    if not labels:
        return None
    from frisket.ops.entities import canonicalize_entity_type

    return {canonicalize_entity_type(label) for label in labels if str(label).strip()}


def run_spacy_ner(
    nlp: Callable[[str], Any], texts: list[str], labels: list[str] | None = None
) -> list[list[dict[str, Any]]]:
    """Pure, injectable-pipeline core (HARD RULE: tests use a tiny fake/mocked
    pipeline for this logic, never a real download). `nlp` is anything
    callable as `nlp(text) -> doc` where `doc.ents` yields objects with
    `.text`/`.label_`/`.start_char`/`.end_char` (a real spaCy `Language`, or a
    fake with that shape). spaCy assigns no per-span confidence, so `score`
    is honestly `None` rather than a fabricated number."""
    wanted = requested_spacy_types(labels)
    results: list[list[dict[str, Any]]] = []
    for text in texts:
        entities: list[dict[str, Any]] = []
        if text and text.strip():
            doc = nlp(text)
            for ent in doc.ents:
                raw_label = str(ent.label_)
                canonical = SPACY_ONTONOTES_TYPES.get(raw_label, raw_label.lower())
                if wanted is not None and canonical not in wanted:
                    continue
                entities.append(
                    {
                        "text": str(ent.text),
                        "type": canonical,
                        "start": int(ent.start_char),
                        "end": int(ent.end_char),
                        "score": None,
                    }
                )
        results.append(entities)
    return results


def run_spacy_ner_default(
    texts: list[str], labels: list[str] | None = None, *, model: str | None = None
) -> list[list[dict[str, Any]]]:
    """Real-pipeline entrypoint: loads (and caches) the available spaCy model
    then runs `run_spacy_ner`. Raises if the engine is not available -- callers
    (NerRecipe.execute) should check `spacy_available()` first and surface an
    honest per-row error instead of calling this blind."""
    target_model = model or SPACY_MODEL
    nlp = _load_pipeline(target_model)
    return run_spacy_ner(nlp, texts, labels)
