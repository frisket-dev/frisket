"""LIVE gate for transcript-grounded map.extract citations (Layer 3, skip-by-default).

Why this exists: the grounded-extract citation chain passed unit-green on stub
models three times while failing on the real Gemini path. Each stub returned a
tidy shape the resolver liked; real Gemini, on a minimal grounded call over the
free-text ``transcript`` column (never the sibling ``transcript_segments`` json),
returned ONE long multi-segment ``quote`` that aligned to no single segment at
threshold 0.8 -> ZERO evidence links on the cell. The fix makes the prompt number
the row's transcript segments and request ``segment_indices`` whenever grounding
is enabled and the row resolves a transcript stream (independent of which columns
were selected), so the model cites by index (strategy B, robust to multi-segment
passages) and the citation lands as TEMPORAL spans on the transcribe writer's
audio/video artifact.

The gate: a real Gemini call on a real
project row produces a citation whose viewer payload artifact is audio/video WITH
a blob and a temporal span. Parametrized to run TWICE on two fresh copies to prove
stability. Deselected from the default suite by the ``network`` marker
(pyproject ``addopts``); skips when GEMINI_API_KEY or the source project is absent.

Run it explicitly (spends sub-cent real money):
    set -a; source .secrets/frisket.env; set +a
    uv run pytest -m network tests/live/test_grounded_extract_live_probe.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.network, pytest.mark.live]

MODEL = "gemini/gemini-2.5-flash"
FIELD = "lane_probe_tip"


def _source_project() -> Path | None:
    explicit = os.environ.get("FRISKET_LIVE_GROUNDING_PROJECT")
    candidates = [explicit] if explicit else []
    candidates += [
        str(
            Path(__file__).resolve().parents[2]
            / "frisket-projects"
            / "live-grounding.frisket"
        ),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _first_transcript_row(project: Any, sheet_id: int) -> tuple[int, int] | None:
    """Return ``(row_id, transcript_column_id)`` for the first row that has both
    a text ``transcript`` value AND a resolvable transcript segment stream."""
    from frisket.engine.store.transcript_segment_stream import (
        resolve_transcript_segment_stream,
    )

    col = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='transcript'",
        (sheet_id,),
    ).fetchone()
    if col is None:
        return None
    column_id = int(col["id"])
    rows = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id", (sheet_id,)
    ).fetchall()
    for row in rows:
        row_id = int(row["id"])
        value = project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)
        if not (isinstance(value, str) and value.strip()):
            continue
        if resolve_transcript_segment_stream(project, sheet_id=sheet_id, row_id=row_id):
            return row_id, column_id
    return None


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"
)
@pytest.mark.skipif(_source_project() is None, reason="source .frisket project absent")
@pytest.mark.parametrize("run_index", [0, 1])
def test_grounded_extract_live_probe(tmp_path: Path, run_index: int) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.ai.llm import ModelRouter, ResponseCache
    from frisket.engine.store.evidence import (
        list_cell_evidence,
        resolve_evidence_viewer,
    )
    from frisket.engine.store.project import Project

    src = _source_project()
    assert src is not None
    dst = tmp_path / f"lane-probe-{run_index}.frisket"
    shutil.copytree(src, dst)

    sheet_id = 1
    project = Project(dst)

    # Capture the raw model response so we can prove the model returned
    # segment_indices (the fix's whole point), not just that a span exists.
    captured: dict[str, Any] = {}
    import frisket.ai.llm.adapters as adapters

    original = adapters.OpenAICompatAdapter.complete

    async def _capture(self, req, client):  # type: ignore[no-untyped-def]
        resp = await original(self, req, client)
        captured["content"] = getattr(resp, "content", None)
        return resp

    adapters.OpenAICompatAdapter.complete = _capture  # type: ignore[assignment]
    try:
        target = _first_transcript_row(project, sheet_id)
        if target is None:
            pytest.skip("no transcript row with a resolvable segment stream")
        row_id, _column_id = target

        action = {
            "schema_version": "frisket.action.v2",
            "kind": "map.extract",
            "capabilities": ["project:write", "model:complete"],
            "params": {
                "sheet_id": sheet_id,
                "input_columns": ["transcript"],
                "model": MODEL,
                "instruction": ("Extract one concrete tip stated in the transcript."),
                "fields": [
                    {
                        "name": FIELD,
                        "type": "text",
                        "description": "A single concrete tip stated in the transcript.",
                    }
                ],
                "grounding": {"enabled": True, "citation_required": False},
                "evidence_policy": {"citation_required": False},
                "confirmed": True,
                "row_ids": [row_id],
            },
            "idempotency_key": f"map_extract@sha256:live-probe-{run_index}",
        }

        router = ModelRouter(cache=ResponseCache(":memory:"), cache_mode="off")
        result = run_action_spec(
            project,
            action,
            project_id="lane-probe",
            router=router,
        )
        assert result.status == "completed", result.errors

        # The model returned per-field evidence carrying segment_indices.
        import json

        parsed = json.loads(captured["content"])
        evidence = parsed[FIELD]["evidence"]
        assert evidence, f"model returned no evidence: {parsed}"
        assert any(
            isinstance(item.get("segment_indices"), list) and item["segment_indices"]
            for item in evidence
        ), f"no segment_indices in evidence: {evidence}"

        # Exactly one NEW citation link on the extracted cell.
        col = project.db.execute(
            "SELECT id, current_run_id FROM columns WHERE sheet_id=? AND name=?",
            (sheet_id, FIELD),
        ).fetchone()
        assert col is not None
        value = project.get_values(sheet_id, int(col["id"]), row_ids=[row_id]).get(
            row_id
        )
        assert isinstance(value, str) and value.strip(), value

        # ``list_cell_evidence`` is current-value scoped, so the only active link
        # it returns is the one this extraction just wrote on the fresh cell.
        evidence_links = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=int(col["id"]),
            project_id="lane-probe",
        )["links"]
        assert len(evidence_links) == 1, evidence_links

        # The viewer payload: audio/video artifact WITH a blob and a temporal span.
        viewer = resolve_evidence_viewer(
            project, evidence_links[0]["stable_id"], project_id="lane-probe"
        )
        artifact = viewer["artifacts"][0]
        media_type = artifact["media_type"]
        assert media_type.startswith("audio/") or media_type.startswith("video/"), (
            artifact
        )
        assert artifact["artifact_ref"]["blob"] is not None, artifact
        spans = artifact["spans"]
        assert spans, viewer
        for span in spans:
            assert span["span_kind"] == "temporal", span
            selector = span["selector"]
            assert selector.get("start_ms") is not None, span
            assert selector.get("end_ms") is not None, span
    finally:
        adapters.OpenAICompatAdapter.complete = original  # type: ignore[assignment]
        project.close()
