"""E-3: ``consumes_resolution`` is a REQUIRED declaration, and it drives
real behavior at its consumer sites.

Before this guard, the marker had no direct coverage: six consumers each
asked ``getattr(recipe, "consumes_resolution", False)``, so a recipe that
never declared it silently answered "no" — a resolution-aware recipe could
ship with its route/promise/consent machinery dormant and every test would
still pass. Two things are pinned here:

1. **The declaration is required.** An undeclared recipe raises at the first
   consumer instead of being read as "no".
2. **The declaration is load-bearing.** The two most consequential consumers
   (``resolve_for_action``'s compile branch and the ``MapRunner`` effect-site
   fence via the worker's verification) are exercised with the SAME synthetic
   recipe declared both ways, so the marker's value — not some other
   difference between recipes — is what changes the behavior.

The registry closure test (every registered recipe declares it explicitly)
lives with the recipe registry in tests/ops/test_first_party_runner_registry.py.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from dataclasses import dataclass
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.runs import verify_execution_route
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.resolve_for_action import resolve_for_action
from frisket.execution.resolver import Refusal, ResolvedExecution
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.ops.base import Recipe
from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)


@pytest.fixture()
def audio_project(tmp_path: Path):
    project = Project.create(tmp_path / "p.frisket")
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"media": col},
    )
    try:
        yield project, sheet_id
    finally:
        project.close()


def _spec(sheet_id: int) -> dict:
    return transcription_spec(sheet_id, vad=True)


@dataclass
class _UndeclaredRecipe(Recipe):
    """A recipe that forgot to declare — the dormant-wiring specimen.

    Deliberately UNDECLARED (the one recipe in the tree that is): this is
    the state the required declaration exists to make loud.
    """

    name: str = "undeclared"


def _synthetic(consumes: bool) -> Recipe:
    """A seam specimen whose sole varying declaration is the marker."""

    class _Marked(Recipe):
        name = "example.resolution_marker"
        execution_capability = "transcribe"
        cost_class = "free"
        consumes_resolution = consumes

    marked = _Marked()
    assert marked.consumes_resolution is consumes
    return marked


# ---------------------------------------------------------------------------
# 1. The declaration is required
# ---------------------------------------------------------------------------


def test_undeclared_recipe_raises_instead_of_defaulting_to_no(audio_project):
    project, sheet_id = audio_project
    with pytest.raises(AttributeError, match="consumes_resolution"):
        resolve_for_action(
            project,
            _spec(sheet_id),
            _UndeclaredRecipe(),
            composition=open_execution_composition(
                project, ModelRouter(), ExecutionCompositionContext.direct()
            ),
        )


def test_base_recipe_declares_nothing_to_inherit():
    # The annotation carries no value: there is nothing for a subclass to
    # inherit by accident.
    assert "consumes_resolution" not in vars(Recipe)
    assert "consumes_resolution" in Recipe.__annotations__


# ---------------------------------------------------------------------------
# 2. The declaration drives the consumers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("consumes", [True, False])
def test_marker_drives_the_resolve_for_action_compile_branch(audio_project, consumes):
    """Consumer 1 (execution/resolve_for_action.py): the marker decides
    whether an invocation resolves a route and compiles promises at all."""
    project, sheet_id = audio_project
    resolved = resolve_for_action(
        project,
        _spec(sheet_id),
        _synthetic(consumes),
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )
    if consumes:
        assert isinstance(resolved, (ResolvedExecution, Refusal))
        assert isinstance(resolved, ResolvedExecution)
        assert resolved.promise_set.promises  # compiled, not skipped
    else:
        assert resolved is None


@pytest.mark.parametrize("consumes", [True, False])
def test_marker_drives_the_effect_site_verification(
    audio_project, monkeypatch, consumes
):
    """Consumer 2 (the MapRunner fence's other half, engine/jobs/runs.py):
    the marker decides whether a run's persisted consent is verified before
    any row executes.

    The run really is routed (the seam wrote its artifacts), and its route
    artifacts are then deleted — the fail-closed case. With the marker
    declared, verification refuses ``consent_missing``; declared the other
    way, the same run is waved through, which is exactly the dormant state
    six ``getattr`` defaults could produce silently.
    """
    project, sheet_id = audio_project
    spec = _spec(sheet_id)
    from frisket.ai.llm import ModelRouter
    from tests.execution_composition_helpers import open_attempt_authority
    from frisket.engine.runner import MapRunner

    run_id = (
        MapRunner(
            project,
            ModelRouter(),
            authority=open_attempt_authority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        .prepare_run(spec, program=transcription_program(spec))
        .run_id
    )
    assert RouteStore.for_run(project, run_id).head() is not None
    for table in ("routes", "promise_sets", "consents"):
        project.db.execute(
            f"DELETE FROM {table} WHERE subject_kind='run' AND subject_id=?",
            (str(run_id),),
        )
    project.db.commit()

    monkeypatch.setattr(
        "frisket.engine.executor.map_rows_action.typed_program_from_runner_spec",
        lambda _project, _spec: _synthetic(consumes),
    )
    if consumes:
        with pytest.raises(ExecutionRouteVerificationFailed) as exc:
            verify_execution_route(
                project,
                run_id,
                spec,
                composition=open_execution_composition(
                    project, None, ExecutionCompositionContext.direct()
                ),
            )
        assert exc.value.code == "consent_missing"
    else:
        verify_execution_route(
            project,
            run_id,
            spec,
            composition=open_execution_composition(
                project, None, ExecutionCompositionContext.direct()
            ),
        )  # never verified
