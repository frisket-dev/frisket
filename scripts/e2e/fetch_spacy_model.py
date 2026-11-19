"""Install the pinned spaCy pipeline into Frisket's managed model cache.

Usage:
    python scripts/e2e/fetch_spacy_model.py

The entity specs run the managed default pipeline. An environment synced with
the `standard` extra has spaCy but not the model, and until the pinned wheel is
in the cache the entity action reports Setup needed. This downloads it through
the same checksum-verified provisioning the in-app setup uses, then derives the
loadable pipeline, and exits nonzero unless the cache reports it present.
"""

from __future__ import annotations

import sys

import httpx

from frisket.ai.models.artifact_manifest import spacy_model_artifact
from frisket.ai.models.spacy_model import ensure_loadable_model_path, model_state
from frisket.engine.jobs.artifact_pull import provision_pinned
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref


def main() -> int:
    pinned = spacy_model_artifact()
    if pinned is None:
        print("the pinned spaCy model manifest entry is missing", file=sys.stderr)
        return 1
    with httpx.Client() as client:
        provision_pinned(
            normalize_artifact_ref(pinned.ref),
            pinned,
            cache_root=None,
            client=client,
            verify_existing=True,
        )
    path = ensure_loadable_model_path()
    state = model_state(include_python_package=False)
    print(f"{pinned.ref}: {state.detail} ({path})")
    return 0 if state.status == "present" else 1


if __name__ == "__main__":
    raise SystemExit(main())
