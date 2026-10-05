"""Shared identities and limits for Frisket's private local classifiers."""

from __future__ import annotations

from dataclasses import dataclass


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


class ClassifierError(Exception):
    """A safe local-classifier failure that may cross the worker boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
