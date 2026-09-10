"""Host rendering bounds precede clip work, without changing full-run batching."""

from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
    NormalizedRange,
)
from frisket.engine.executor.temporal_split import stage_temporal_split_plan
from frisket.engine.store import Project
from frisket.engine.store.media_splice import MediaSpliceError
from tests.engine.test_temporal_split import _fake_stage, _plan, _request, _seed_media


@pytest.fixture
def rendering(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "bounded.frisket")) as project:
        seeded = _seed_media(project)
        plan = _plan(
            project,
            _request(
                seeded,
                {
                    "kind": "draft_ranges",
                    "items": [
                        {"start_ms": index * 1000, "end_ms": (index + 1) * 1000}
                        for index in range(5)
                    ],
                },
            ),
        )
        scratch = tmp_path / "clips"
        scratch.mkdir()
        observed = {"renders": [], "leases": 0, "released": 0, "callbacks": []}
        original_materialize = project.materialize_blob

        @contextmanager
        def lease(blob):
            observed["leases"] += 1
            try:
                with original_materialize(blob) as path:
                    yield path
            finally:
                observed["released"] += 1

        async def render(_source_path, output_path, *, start_ms, end_ms, **kwargs):
            observed["renders"].append((start_ms, end_ms))
            observed["callbacks"].append(kwargs.get("should_cancel"))
            return _fake_stage(
                project, None, NormalizedRange(start_ms, end_ms, "test"), output_path
            )

        monkeypatch.setattr(project, "materialize_blob", lease)
        monkeypatch.setattr(
            "frisket.engine.executor.temporal_split.stage_media_splice", render
        )
        yield project, plan, scratch, observed


@pytest.mark.parametrize("limit, expected", [(None, 5), (0, 0), (1, 1), (3, 3), (8, 5)])
def test_budget_renders_exact_prefix_and_keeps_single_source_lease(
    rendering, limit, expected
):
    project, plan, scratch, observed = rendering
    staged = stage_temporal_split_plan(
        project,
        plan,
        scratch,
        materializer=CoreTemporalMediaMaterializer(),
        use_core_batch_renderer=True,
        row_limit=limit,
    )
    assert len(staged) == expected
    assert observed["renders"] == [
        (index * 1000, (index + 1) * 1000) for index in range(expected)
    ]
    assert observed["leases"] == observed["released"] == int(expected > 0)
    assert len(list(scratch.iterdir())) == expected


def test_budget_does_not_lease_later_sources(rendering):
    project, plan, scratch, observed = rendering
    # The later source cannot be opened; it is outside the admitted prefix.
    later = replace(
        plan.sources[0],
        source=replace(
            plan.sources[0].source,
            lease=replace(plan.sources[0].source.lease, blob_hash="unavailable"),
        ),
    )
    plan = replace(plan, sources=(*plan.sources, later), output_count=10)
    assert (
        len(
            stage_temporal_split_plan(
                project,
                plan,
                scratch,
                materializer=CoreTemporalMediaMaterializer(),
                use_core_batch_renderer=True,
                row_limit=2,
            )
        )
        == 2
    )
    assert observed["leases"] == observed["released"] == 1


@pytest.mark.parametrize("stop_after", [0, 1])
def test_cancel_stops_before_next_clip_and_releases_source(rendering, stop_after):
    project, plan, scratch, observed = rendering

    def cancelled():
        return len(observed["renders"]) >= stop_after

    with pytest.raises(MediaSpliceError) as failure:
        stage_temporal_split_plan(
            project,
            plan,
            scratch,
            materializer=CoreTemporalMediaMaterializer(),
            use_core_batch_renderer=True,
            cancelled=cancelled,
        )
    assert failure.value.code == "cancelled"
    assert len(observed["renders"]) == stop_after
    assert observed["leases"] == observed["released"] == int(stop_after > 0)
    assert all(callback is cancelled for callback in observed["callbacks"])


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_invalid_budget_refuses_before_lease(rendering, limit):
    project, plan, scratch, observed = rendering
    with pytest.raises(ValueError, match="row_limit"):
        stage_temporal_split_plan(
            project,
            plan,
            scratch,
            materializer=CoreTemporalMediaMaterializer(),
            row_limit=limit,
        )
    assert observed["leases"] == 0
