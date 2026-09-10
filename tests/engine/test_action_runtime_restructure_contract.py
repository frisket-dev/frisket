from __future__ import annotations

from frisket.actions.registry import ACTION_REGISTRY, NEW_ACTION_IDS
from frisket.engine.executor.action_specs import (
    ACCEPTED_EXECUTION_OUTLIERS,
    _RUN_BOUND_ACTION_KINDS,
    _RUNLESS_ACTION_KINDS,
    ActionExecutionSpec,
    PlacementPolicy,
    RunCoordinatePolicy,
    declared_queued_project_run_kinds,
    execution_registry,
    queued_action_job_kinds,
    queued_project_run_kinds,
)
from frisket.engine.executor.queue_policy import (
    INTENTIONALLY_DIRECT_V1_ACTIONS,
    queue_policy_candidate_action_kinds,
)


EXECUTION_REGISTRY = execution_registry()


# --- registry coverage -----------------------------------------------------


def test_execution_registry_and_outliers_partition_all_v1_kinds() -> None:
    covered = set(EXECUTION_REGISTRY) | set(ACCEPTED_EXECUTION_OUTLIERS)
    assert covered == set(ACTION_REGISTRY.action_ids), {
        "uncovered": sorted(set(ACTION_REGISTRY.action_ids) - covered),
        "unknown": sorted(covered - set(ACTION_REGISTRY.action_ids)),
    }
    # registry and outliers must be disjoint; an action is one or the other.
    assert not (set(EXECUTION_REGISTRY) & set(ACCEPTED_EXECUTION_OUTLIERS))


def test_run_coordinate_policies_partition_all_v1_kinds() -> None:
    run_bound = set(_RUN_BOUND_ACTION_KINDS)
    runless = set(_RUNLESS_ACTION_KINDS)
    assert not (run_bound & runless)
    assert run_bound | runless == set(ACTION_REGISTRY.action_ids)
    assert set(ACCEPTED_EXECUTION_OUTLIERS) <= runless


def test_outliers_are_named_and_principled() -> None:
    # Outliers are a small, explicit set with real reasons (the blank-mind test).
    assert set(ACCEPTED_EXECUTION_OUTLIERS) == set()
    for kind, reason in ACCEPTED_EXECUTION_OUTLIERS.items():
        assert isinstance(reason, str) and len(reason) > 20, kind


def test_every_spec_is_an_action_execution_spec() -> None:
    for kind, spec in EXECUTION_REGISTRY.items():
        assert isinstance(spec, ActionExecutionSpec)
        assert spec.kind == kind
        assert isinstance(spec.lifecycle.placement, PlacementPolicy)
        assert isinstance(spec.run_coordinate, RunCoordinatePolicy)


def test_known_runless_actions_allow_runless_provider_effect_facts() -> None:
    assert (
        EXECUTION_REGISTRY["web.capture_page"].run_coordinate
        is RunCoordinatePolicy.RUNLESS
    )


def test_run_backed_actions_guarantee_run_bound_provider_effect_facts() -> None:
    # The recipe registry is retired: every project-run program is now typed,
    # so the run-bound roster is the declared queued placement plus the
    # inline run-backed lifecycle kinds.
    run_bound_kinds = set(queued_project_run_kinds()) | {
        "embedding.index_create",
        "embedding.index_refresh",
        "run.backfill",
        "cluster.values",
    }
    assert {"map.classify", "map.translate", "research.answer"} <= run_bound_kinds
    for kind in run_bound_kinds:
        assert EXECUTION_REGISTRY[kind].run_coordinate is RunCoordinatePolicy.RUN_BOUND


# --- params model identity (no drift / no duplicate model) -----------------


def test_execution_spec_params_model_matches_catalog_object() -> None:
    for kind, spec in EXECUTION_REGISTRY.items():
        assert kind in NEW_ACTION_IDS, f"{kind}: not a typed action"
        expected = ACTION_REGISTRY.get(kind).definition.run.params_model
        assert spec.params_model is expected, (
            f"{kind}: execution spec params model "
            f"{spec.params_model!r} drifted from catalog "
            f"{expected!r}"
        )


# --- placement is declared; queued registry is an invariant over it --------


def test_queued_placement_is_source_and_registry_is_invariant() -> None:
    declared = queued_project_run_kinds()
    # the registry's queued placements must reflect the DECLARED source set.
    assert declared == set(declared_queued_project_run_kinds())
    assert declared <= NEW_ACTION_IDS
    assert declared & NEW_ACTION_IDS == {
        "cluster.values",
        "join.semantic",
        "enrich.geocode",
        "enrich.census_demographics",
        "map.api_call",
        "map.ask",
        "map.classify",
        "map.extract",
        "map.find_topic_sections",
        "map.find_visual_cuts",
        "map.judge",
        "map.mcp_extract",
        "map.ner",
        "map.python",
        "map.regex_extract",
        "map.summarize",
        "map.to_geo_point",
        "map.translate",
        "media.extract_metadata",
        "media.extract_pdf_tables",
        "media.fetch_url",
        "media.ytdlp_download",
        "media.video_frames",
        "media.extract_faces",
        "media.to_markdown",
        "media.ocr",
        "media.transcribe",
        "research.answer",
        "research.web_search",
        "web.capture_screenshot",
    }


def test_queue_policy_is_invariant_over_declared_placement() -> None:
    queued = queued_project_run_kinds()
    action_jobs = queued_action_job_kinds()
    direct = set(INTENTIONALLY_DIRECT_V1_ACTIONS)
    # no queued action is also declared intentionally-direct.
    assert not ((queued | action_jobs) & direct)
    assert not (queued & action_jobs)
    # every catalog queue-candidate is accounted for by exactly one of
    # declared project-run queued, declared action-job queued, or declared-direct;
    # queue_policy is an invariant, not a second source of truth.
    candidates = queue_policy_candidate_action_kinds()
    declared = queued | action_jobs | direct
    assert candidates == declared, {
        "unaccounted": sorted(candidates - declared),
        "over_declared": sorted(declared - candidates),
    }
    # intentionally-direct kinds that have an execution spec must not declare a
    # queued placement.
    for kind in direct:
        spec = EXECUTION_REGISTRY.get(kind)
        if spec is not None:
            assert spec.lifecycle.placement is not PlacementPolicy.QUEUED_PROJECT_RUN
            assert spec.lifecycle.placement is not PlacementPolicy.QUEUED_ACTION_JOB
