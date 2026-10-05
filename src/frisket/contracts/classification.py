"""Shared identities and limits for Frisket's private local classifiers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


GLICLASS_ENGINE_ID = "gliclass"
GLICLASS_MODEL_REPO = "knowledgator/gliclass-base-v3.0"
GLICLASS_MODEL_REVISION = "77a70e6cd52e602ed18184ef37d18bdd3741e3d5"
GLICLASS_SETUP_REF = "engine-setup:gliclass.local@1"

JEFF_ENGINE_ID = "jeff"
JEFF_MODEL_REPO = "mstrasser/Jeff-Qwen3.5-0.8B"
JEFF_MODEL_REVISION = "f0a2b523f1b64c567d4628fadd60caea03cc6847"
JEFF_SETUP_REF = "engine-setup:jeff.local@1"

LOCAL_CLASSIFIER_ENGINE_IDS = frozenset({GLICLASS_ENGINE_ID, JEFF_ENGINE_ID})
CLEF_ENGINE_IDS = frozenset({"clef", "clef-flash"})
CATEGORY_ONLY_CLASSIFIER_ENGINE_IDS = frozenset(
    {"local_semantic", *LOCAL_CLASSIFIER_ENGINE_IDS}
)


@dataclass(frozen=True)
class LocalClassifierSpec:
    engine_id: str
    model_repo: str
    revision: str
    setup_ref: str
    token_limit: int
    label_limit: int | None

    @property
    def model_identity(self) -> str:
        return f"{self.model_repo}@{self.revision}"


LOCAL_CLASSIFIERS = {
    GLICLASS_ENGINE_ID: LocalClassifierSpec(
        engine_id=GLICLASS_ENGINE_ID,
        model_repo=GLICLASS_MODEL_REPO,
        revision=GLICLASS_MODEL_REVISION,
        setup_ref=GLICLASS_SETUP_REF,
        token_limit=512,
        label_limit=255,
    ),
    JEFF_ENGINE_ID: LocalClassifierSpec(
        engine_id=JEFF_ENGINE_ID,
        model_repo=JEFF_MODEL_REPO,
        revision=JEFF_MODEL_REVISION,
        setup_ref=JEFF_SETUP_REF,
        token_limit=8192,
        label_limit=254,
    ),
}


@dataclass(frozen=True)
class ClassificationCapabilities:
    field_types: tuple[str, ...]
    max_fields: int = 64
    min_labels: int = 1
    max_labels: int | None = None
    include_confidence: bool = False
    include_justification: bool = False
    behavior_note: str = ""


_CLEF_CAPABILITIES = ClassificationCapabilities(
    field_types=("category", "boolean", "score"),
    min_labels=2,
    max_labels=254,
    include_confidence=True,
    behavior_note=(
        "Always selects an answer, even when the input is inconclusive. "
        "Does not return ‘not found.’"
    ),
)

CLASSIFICATION_CAPABILITIES = {
    "local_semantic": ClassificationCapabilities(("category",), max_fields=1),
    **{
        engine: ClassificationCapabilities(
            ("category",), min_labels=2, max_labels=spec.label_limit
        )
        for engine, spec in LOCAL_CLASSIFIERS.items()
    },
    **{engine: _CLEF_CAPABILITIES for engine in CLEF_ENGINE_IDS},
    "llm": ClassificationCapabilities(
        ("category", "score", "integer", "number", "boolean", "text"),
        include_confidence=True,
        include_justification=True,
    ),
}


def classification_options(engine: str) -> dict[str, Any]:
    """Project the same declared limits used by request validation into UI hints."""
    spec = CLASSIFICATION_CAPABILITIES[engine]
    return {
        "field_types": list(spec.field_types),
        "max_fields": spec.max_fields,
        "min_labels": spec.min_labels,
        "max_labels": spec.max_labels,
        "include_confidence": spec.include_confidence,
        "include_justification": spec.include_justification,
        "behavior_note": spec.behavior_note,
    }


def validate_classification(
    engine: str,
    fields: Sequence[Mapping[str, Any]],
    include_confidence: bool = False,
    include_justification: bool = False,
) -> None:
    """Validate flat decision fields without depending on action parameter types."""
    spec = CLASSIFICATION_CAPABILITIES[engine]
    if engine == "local_semantic" and (
        len(fields) != spec.max_fields
        or any(field["type"] not in spec.field_types for field in fields)
    ):
        raise ValueError("local_semantic requires exactly one category field")
    if not 1 <= len(fields) <= spec.max_fields:
        label = "Clef" if engine in CLEF_ENGINE_IDS else engine
        raise ValueError(f"{label} requires between 1 and {spec.max_fields} fields")
    if any(field["type"] not in spec.field_types for field in fields):
        if engine in CLEF_ENGINE_IDS:
            raise ValueError("Clef supports category, boolean, and score fields only")
        raise ValueError(f"{engine} supports only category fields")
    for field in fields:
        if field["type"] != "category":
            continue
        labels = field.get("labels", ())
        if any(not isinstance(label, str) or not label.strip() for label in labels):
            raise ValueError("category fields require non-empty labels")
        if len(labels) != len(set(labels)):
            raise ValueError("labels must be unique")
        if len(labels) < spec.min_labels or (
            spec.max_labels is not None and len(labels) > spec.max_labels
        ):
            if engine in CLEF_ENGINE_IDS:
                raise ValueError("Clef category fields require 2 to 254 labels")
            if len(labels) < spec.min_labels:
                raise ValueError(f"{engine} requires at least two labels per field")
            raise ValueError(
                f"{engine} supports at most {spec.max_labels} labels per field"
            )
    if (include_confidence and not spec.include_confidence) or (
        include_justification and not spec.include_justification
    ):
        if engine in CLEF_ENGINE_IDS:
            raise ValueError("Clef returns decisions, not written justifications")
        if engine == "local_semantic":
            raise ValueError("local_semantic emits only the winning label")
        raise ValueError(f"{engine} does not declare companion outputs")


class ClassifierError(Exception):
    """A safe local-classifier failure that may cross the worker boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
