from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Callable

from frisket.engine._workers.spacy_entities import (
    SPACY_CANONICAL_TYPES as SPACY_CANONICAL_TYPES,
    SPACY_ONTONOTES_TAGS as SPACY_ONTONOTES_TAGS,
    SPACY_ONTONOTES_TYPES as SPACY_ONTONOTES_TYPES,
    extract_entities,
)

DEFAULT_SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL = os.environ.get("FRISKET_SPACY_MODEL", DEFAULT_SPACY_MODEL)

_PIPELINE_CACHE: dict[str, Any] = {}
_PIPELINE_LOCK = threading.Lock()


def spacy_available(model: str | None = None) -> tuple[bool, str | None]:
    """Passively check host/private runtime and model readiness without loading.

    Custom models require host-installed spaCy. The managed default can use
    the private runtime together with Frisket's checksum-verified model cache.
    """
    target_model = model or SPACY_MODEL
    from frisket.runtime.spacy_install import runtime_present

    library = runtime_present()
    if not library:
        return False, (
            "spaCy is not installed. Set up spaCy in the engine picker. "
            "For a command-line installation, install frisket-data[standard]."
        )
    if target_model == DEFAULT_SPACY_MODEL:
        from frisket.ai.models.spacy_model import model_state

        state = (
            model_state()
            if importlib.util.find_spec("spacy") is not None
            else model_state(include_python_package=False)
        )
        if state.status == "present":
            return True, None
        return False, (
            f"spaCy is installed but the '{target_model}' model is unavailable: "
            f"{state.detail}. Pinned artifact: {state.ref}. For a manual "
            f"installation, run: python -m spacy download {target_model}."
        )
    if (
        importlib.util.find_spec("spacy") is None
        or importlib.util.find_spec(target_model) is None
    ):
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
    """Apply the shared pure extractor with canonicalized requested labels."""
    return extract_entities(nlp, texts, requested_spacy_types(labels))


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
