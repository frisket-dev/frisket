"""The ONE output-retirement mechanism: recipes declare their
dropped-role output names (`Recipe.retired_output_names`) and the generic
`retire_dropped_output_columns` hides any that still exist as ai_generated
columns outside the current output set — hide-by-default + provenance on
the op. Typed actions only touch their explicitly declared output names."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from frisket.ops.base import Recipe
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import typed_queued_map_spec
from frisket.engine.runner.column_retirement import (
    retirement_owns_column,
    retire_dropped_output_columns,
)
from frisket.engine.store import Project


@pytest.fixture
def project(tmp_path: Path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


class _FakeRecipe(Recipe):
    """Declares two retired names regardless of spec (the map_runner hook does
    the existence/current-output filtering)."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    def retired_output_names(self, spec: dict) -> list[str]:
        return ["out_confidence", "out_justification"]


@dataclass
class _AbsoluteDetectedRecipe(Recipe):
    """A (fake) translate-like recipe declaring the SHARED ABSOLUTE name
    detected_language — the F5 collision surface with transcribe."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    name: str = "translate"

    def retired_output_names(self, spec: dict) -> list[str]:
        return ["detected_language"]


@dataclass
class _DerivedDetectedRecipe(Recipe):
    """A (fake) translate-like recipe whose retired name is DERIVED from its
    (lowercased) output — the F7 case-mismatch surface."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    name: str = "translate"

    def retired_output_names(self, spec: dict) -> list[str]:
        out = (spec.get("output_name") or "").strip().lower()
        return [f"{out}_detected_language"] if out else []


def _ai_column(p: Project, sheet_id: int, name: str) -> int:
    return p.add_column(sheet_id, name, type="text", ai_generated=True)


def _column(p: Project, cid: int):
    return p.db.execute("SELECT * FROM columns WHERE id=?", (cid,)).fetchone()


# --------------------------------------------------------------------------- #
# The generic hook


def test_declared_dropped_roles_are_retired(project: Project):
    sheet = project.add_sheet("data")
    op_id = project.append_op("map", {"recipe": "fake"})
    live = _ai_column(project, sheet, "out")  # the current output (reused)
    conf = _ai_column(project, sheet, "out_confidence")  # dropped
    just = _ai_column(project, sheet, "out_justification")  # dropped

    retired = retire_dropped_output_columns(
        project,
        op_id=op_id,
        recipe=_FakeRecipe(),
        spec={"action_kind": "map.fake"},
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"out"},
    )

    assert set(retired) == {conf, just}
    # hide-by-default (values preserved, revealable); the live output untouched
    assert _column(project, conf)["default_hidden"] == 1
    assert _column(project, just)["default_hidden"] == 1
    assert _column(project, live)["default_hidden"] == 0
    # provenance recorded on the op
    info = json.loads(
        project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()[
            "undo_info"
        ]
        or "{}"
    )
    names = {e["name"] for e in info["retired_output_columns"]}
    assert names == {"out_confidence", "out_justification"}


def test_a_role_still_in_the_current_output_is_not_retired(project: Project):
    sheet = project.add_sheet("data")
    op_id = project.append_op("map", {"recipe": "fake"})
    _ai_column(project, sheet, "out")
    conf = _ai_column(project, sheet, "out_confidence")

    # out_confidence is STILL emitted this run -> not an orphan
    retired = retire_dropped_output_columns(
        project,
        op_id=op_id,
        recipe=_FakeRecipe(),
        spec={"action_kind": "map.fake"},
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"out", "out_confidence"},
    )
    assert retired == []
    assert _column(project, conf)["default_hidden"] == 0


def _run_with_params(
    p: Project, sheet_id: int, action_kind: str, params: dict[str, object]
) -> int:
    """A completed run row attributed to one canonical action kind."""
    op_id = p.append_op("map", {"action_kind": action_kind})
    p.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, params, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            op_id,
            sheet_id,
            action_kind,
            json.dumps(params),
            "completed",
        ),
    )
    run_id = int(p.db.execute("SELECT last_insert_rowid()").fetchone()[0])
    p.db.commit()
    return run_id


def _run(p: Project, sheet_id: int, action_kind: str, output_name: str) -> int:
    return _run_with_params(p, sheet_id, action_kind, {"output_name": output_name})


def _point(p: Project, cid: int, run_id: int) -> None:
    p.db.execute("UPDATE columns SET current_run_id=? WHERE id=?", (run_id, cid))
    p.db.commit()


def test_single_output_ownership_ignores_an_unrelated_prior_field(project: Project):
    sheet = project.add_sheet("data")
    prior = _run_with_params(
        project,
        sheet,
        "map.translate",
        {
            "output_name": "interview",
            "fields": [{"name": "transcript"}],
        },
    )

    assert not retirement_owns_column(
        project,
        prior,
        "map.translate",
        "transcript",
        "detected_language",
        {"transcript", "transcript_segments"},
    )


def test_multi_output_ownership_uses_the_prior_declared_field(project: Project):
    sheet = project.add_sheet("data")
    prior = _run_with_params(
        project,
        sheet,
        "map.classify",
        {"fields": [{"name": "topic"}]},
    )

    assert retirement_owns_column(
        project,
        prior,
        "map.classify",
        None,
        "topic_similarity",
        {"topic"},
    )


def test_output_instance_guard_across_two_generic_outputs(project: Project):
    # Transcribe declares the absolute name ``detected_language``,
    # so recipe-match alone is not enough. Two transcribe outputs on one sheet:
    #   A = "transcript" (Whisper, detecting) -> has detected_language
    #   B = "interview"  (rerun onto Parakeet, non-detecting)
    # B's rerun must NOT hide A's detected_language.
    sheet = project.add_sheet("data")
    run_a = _run(project, sheet, "map.translate", "transcript")
    _point(project, _ai_column(project, sheet, "transcript"), run_a)
    a_detected = _ai_column(project, sheet, "detected_language")
    _point(project, a_detected, run_a)

    run_b = _run(project, sheet, "map.translate", "interview")
    _point(project, _ai_column(project, sheet, "interview"), run_b)

    # B (Parakeet) reruns onto its own output "interview"
    b_op = project.append_op("map", {"recipe": "transcribe"})
    retired = retire_dropped_output_columns(
        project,
        op_id=b_op,
        recipe=_AbsoluteDetectedRecipe(),
        spec={
            "action_kind": "map.translate",
            "engine": "parakeet-tdt",
            "output_name": "interview",
        },
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"interview", "interview_segments"},
    )
    assert retired == []  # A's detected_language is a DIFFERENT output instance
    assert _column(project, a_detected)["default_hidden"] == 0


def test_output_instance_guard_retires_own_detected_language(project: Project):
    # A's OWN rerun onto a non-detecting engine still retires A's detected_language.
    sheet = project.add_sheet("data")
    run_a = _run(project, sheet, "map.translate", "transcript")
    _point(project, _ai_column(project, sheet, "transcript"), run_a)
    a_detected = _ai_column(project, sheet, "detected_language")
    _point(project, a_detected, run_a)

    a_rerun_op = project.append_op("map", {"recipe": "transcribe"})
    retired = retire_dropped_output_columns(
        project,
        op_id=a_rerun_op,
        recipe=_AbsoluteDetectedRecipe(),
        spec={
            "action_kind": "map.translate",
            "engine": "parakeet-tdt",
            "output_name": "transcript",
        },
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"transcript", "transcript_segments"},
    )
    assert retired == [a_detected]
    assert _column(project, a_detected)["default_hidden"] == 1


def test_unattributed_absolute_name_survives_a_cross_action_rerun(project: Project):
    # A regression: an unattributed (NULL run) transcribe detected_language must NOT
    # be hidden by a translate rerun declaring the same ABSOLUTE legacy name —
    # it is not provably anyone's, so it stays visible.
    sheet = project.add_sheet("data")
    _ai_column(project, sheet, "es")  # translate's live output
    detected = _ai_column(project, sheet, "detected_language")  # NULL pointer, absolute
    retired = retire_dropped_output_columns(
        project,
        op_id=project.append_op("map", {"recipe": "translate"}),
        recipe=_AbsoluteDetectedRecipe(),
        spec={"action_kind": "map.translate", "output_name": "es"},
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"es"},
    )
    assert retired == []
    assert _column(project, detected)["default_hidden"] == 0


def test_unattributed_absolute_name_survives_same_action_other_output(project: Project):
    # A regression: even a transcribe rerun of a DIFFERENT output must not hide an
    # unattributed detected_language (absolute name, no run pointer).
    sheet = project.add_sheet("data")
    _ai_column(project, sheet, "other")
    detected = _ai_column(project, sheet, "detected_language")  # NULL, absolute
    retired = retire_dropped_output_columns(
        project,
        op_id=project.append_op("map", {"recipe": "transcribe"}),
        recipe=_AbsoluteDetectedRecipe(),
        spec={
            "action_kind": "map.translate",
            "engine": "parakeet-tdt",
            "output_name": "other",
        },
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"other", "other_segments"},
    )
    assert retired == []
    assert _column(project, detected)["default_hidden"] == 0


def test_output_name_comparison_is_case_folded(project: Project):
    # A regression: a prior run persisted output_name "ES"; the rerun uses "es".
    # They target the same (lowercased) output, so the derived detected_language
    # column IS retired despite the case mismatch.
    sheet = project.add_sheet("data")
    prior = _run(project, sheet, "map.translate", "ES")
    _point(project, _ai_column(project, sheet, "es"), prior)
    detected = _ai_column(project, sheet, "es_detected_language")
    _point(project, detected, prior)

    retired = retire_dropped_output_columns(
        project,
        op_id=project.append_op("map", {"recipe": "translate"}),
        recipe=_DerivedDetectedRecipe(),
        spec={"action_kind": "map.translate", "output_name": "es"},
        columns=project.columns(sheet, include_hidden=True),
        current_output_names={"es"},
    )
    assert retired == [detected]
    assert _column(project, detected)["default_hidden"] == 1


def test_recipe_with_nothing_to_retire_is_a_noop(project: Project):
    sheet = project.add_sheet("data")
    op_id = project.append_op("map", {"recipe": "plain"})
    _ai_column(project, sheet, "out")
    assert (
        retire_dropped_output_columns(
            project,
            op_id=op_id,
            recipe=Recipe(),  # base: retired_output_names == []
            spec={},
            columns=project.columns(sheet, include_hidden=True),
            current_output_names={"out"},
        )
        == []
    )


# --------------------------------------------------------------------------- #
# The recipe declarations both consumers provide


def test_typed_transcribe_only_touches_declared_outputs(project: Project):
    sheet = project.add_sheet("audio")
    project.add_column(sheet, "media", type="audio")
    detected = _ai_column(project, sheet, "detected_language")
    action = ACTION_REGISTRY.get("media.transcribe")
    for engine, detects in (
        ("parakeet-tdt", False),
        ("faster_whisper", True),
        ("whisper", True),
    ):
        params = action.definition.run.params_model(source="media", engine=engine)
        keys = {
            field.key for field in action.definition.run.resolve_output_fields(params)
        }
        assert ("detected_language" in keys) is detects

    request = ActionRequest(
        action_id=action.action_id,
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={"source": "media", "engine": "parakeet-tdt"},
        output_names={"text": "transcript", "segments": "transcript_segments"},
        idempotency_key="explicit-transcription-outputs",
    )
    bound = BoundTypedActionRequest.bind(action, request)
    _, queued, program = typed_queued_map_spec(bound)
    spec = queued.runner_spec_fn(bound.params)
    assert program.retired_output_names(spec) == []
    assert (
        retire_dropped_output_columns(
            project,
            op_id=project.append_op("map", spec),
            recipe=program,
            spec=spec,
            columns=project.columns(sheet, include_hidden=True),
            current_output_names=set(request.output_names.values()),
        )
        == []
    )
    assert _column(project, detected)["default_hidden"] == 0
