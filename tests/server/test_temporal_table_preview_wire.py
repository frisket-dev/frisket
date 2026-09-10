"""Temporal samples expose scratch clocks/files, never persisted artifact aliases."""

from pathlib import Path

import pytest

from frisket.actions.temporal_segments import TemporalSegmentsParams
from frisket.engine.executor.temporal_materialization import NormalizedRange
from tests.engine.test_temporal_split import _fake_stage, _request, _seed_media
from tests.server.test_table_action_previews import _run, preview_host as preview_host


def test_http_temporal_preview_wire_and_artifact_lifetime(preview_host, monkeypatch):
    workspace, registry, _service, client = preview_host
    project = workspace.get("preview")
    seeded = _seed_media(project)
    calls = []

    async def render(_source, path, *, start_ms, end_ms, **kwargs):
        calls.append(Path(path))
        return _fake_stage(
            project, None, NormalizedRange(start_ms, end_ms, "clip"), path
        )

    monkeypatch.setattr(
        "frisket.engine.executor.temporal_split.stage_media_splice", render
    )
    request = _request(
        seeded, {"kind": "draft_ranges", "items": [{"start_ms": 1000, "end_ms": 2000}]}
    ).model_dump(mode="json")
    before = tuple(project.db.iterdump())
    preview_id, result = _run(client, request)
    assert tuple(project.db.iterdump()) == before
    row = result["rows"][0]
    clock = row["source_range"]["value"]
    assert clock["schema_version"] == "frisket.preview_temporal.v1"
    assert "artifact_stable_id" not in clock["timeline"]
    assert clock["item"]["start_ms"] == 1000
    with pytest.raises(ValueError):
        TemporalSegmentsParams.model_validate(
            {"source": "video", "selection": {"kind": "typed_value", "value": clock}}
        )
    artifact_url = row["clip"]["value"]
    opened = client.get(artifact_url)
    assert opened.status_code == 200
    assert opened.content == b"clip:1000:2000"
    assert calls and all(not path.exists() for path in calls)
    cancelled = client.delete(f"/api/projects/preview/actions/v1/preview/{preview_id}")
    assert cancelled.status_code in {200, 204}
    # Cancelling completed work is intentionally a no-op. Registry disposal
    # owns finished sample lifetime, just as for other table preview artifacts.
    registry.shutdown()
    assert client.get(artifact_url).status_code in {404, 410}
