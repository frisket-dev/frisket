from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

import frisket.preview.topic_segmentation as topic_compare
from frisket.actions.system import root_action_catalog
from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.features.temporal.transcript_boundaries import lock_transcript_boundaries
from frisket.features.topic_segmentation import (
    BetweenUnits,
    BoundaryCandidate,
    EngineDefinition,
    ExactTime,
    PreflightResult,
    SegmentationContext,
    SegmentationResult,
    SegmentationSnapshot,
    WithinUnit,
)


MUTATION_TABLES = (
    "rows",
    "columns",
    "blobs",
    "runs",
    "results",
    "model_calls",
    "ops",
    "receipts",
)

SRT_BYTES = b"""1
00:00:00,000 --> 00:00:01,000
Apples grow in orchards.

2
00:00:01,000 --> 00:00:02,000
Pears grow there too.

3
00:00:02,000 --> 00:00:03,000
Rockets travel through space.

4
00:00:03,000 --> 00:00:04,000
Astronauts work in orbit.
"""

OVERLAPPING_SRT_BYTES = b"""1
00:00:00,000 --> 00:00:02,000
A

2
00:00:01,000 --> 00:00:03,000
B

3
00:00:03,000 --> 00:00:04,000
C
"""

CHAINED_OVERLAPPING_SRT_BYTES = b"""1
00:00:00,000 --> 00:00:01,300
w0

2
00:00:01,000 --> 00:00:02,300
w1

3
00:00:02,000 --> 00:00:03,300
w2

4
00:00:03,000 --> 00:00:04,300
w3

5
00:00:04,000 --> 00:00:05,300
w4
"""


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in MUTATION_TABLES
    }


class _FakeSegmenter:
    def __init__(
        self,
        engine_id: str,
        boundaries: tuple[BoundaryCandidate, ...],
    ) -> None:
        self.definition = EngineDefinition(
            id=engine_id,
            version="fixture-1",
            label="Fixture",
            description="Topic Compare fixture engine.",
        )
        self._boundaries = boundaries

    def validate_settings(
        self, settings: Mapping[str, JsonValue] | None
    ) -> Mapping[str, JsonValue]:
        return {"detail": (settings or {}).get("detail", "balanced")}

    def preflight(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
    ) -> PreflightResult:
        del snapshot, settings
        return PreflightResult.passed()

    def segment(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
        context: SegmentationContext,
    ) -> SegmentationResult:
        del snapshot
        context.raise_if_cancelled()
        return SegmentationResult(
            engine_id=self.definition.id,
            engine_version=self.definition.version,
            boundaries=self._boundaries,
            resolved_settings=dict(settings or {}),
            diagnostics={"native_metric": "fixture"},
        )


def test_scratch_parsers_produce_ephemeral_ordered_units() -> None:
    txt = topic_compare.parse_scratch_transcript(
        b"First paragraph\n\nSecond paragraph\n",
        filename="sample.txt",
        language="en",
    )
    assert txt.snapshot.source_kind == "untimed_transcript"
    assert [(unit.ordinal, unit.text) for unit in txt.snapshot.units] == [
        (0, "First paragraph"),
        (1, "Second paragraph"),
    ]
    assert txt.snapshot.units[0].start_ms is None

    srt = topic_compare.parse_scratch_transcript(
        SRT_BYTES,
        filename="sample.srt",
    )
    assert srt.snapshot.source_kind == "timestamped_transcript"
    assert (srt.snapshot.units[0].start_ms, srt.snapshot.units[0].end_ms) == (
        0,
        1_000,
    )

    vtt = topic_compare.parse_scratch_transcript(
        b"WEBVTT\n\nintro\n00:00.000 --> 00:01.250\n<v Jane>Opening line\n",
        filename="sample.vtt",
    )
    assert len(vtt.snapshot.units) == 1
    assert vtt.snapshot.units[0].speaker == "Jane"
    assert vtt.snapshot.units[0].text == "Opening line"
    assert vtt.snapshot.units[0].end_ms == 1_250

    cue_identifier = topic_compare.parse_scratch_transcript(
        b"WEBVTT\n\nNOTEBOOK\n00:00.000 --> 00:01.000\nA real cue\n",
        filename="identifier.vtt",
    )
    assert [unit.text for unit in cue_identifier.snapshot.units] == ["A real cue"]


def test_compare_returns_canonical_boundaries_and_shared_within_unit_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundaries = (
        BoundaryCandidate(
            id="between",
            locator=BetweenUnits("cue-000000", "cue-000001"),
            strength=0.7,
            diagnostics={"gap": 0.7},
        ),
        BoundaryCandidate(
            id="within",
            locator=WithinUnit("cue-000002"),
            strength=0.9,
            diagnostics={"word_offset": 3},
        ),
        BoundaryCandidate(
            id="same-place-time",
            locator=ExactTime(2_500),
            diagnostics={"native_time_ms": 2_500},
        ),
    )
    fake = _FakeSegmenter("texttiling", boundaries)
    monkeypatch.setattr(topic_compare, "get_segmenter", lambda engine: fake)

    payload = asyncio.run(
        topic_compare.compare_topic_segmentation_scratch(
            SRT_BYTES,
            {
                "filename": "sample.srt",
                "variants": [
                    {
                        "id": "texttiling-more",
                        "engine": "texttiling",
                        "settings": {"detail": "more"},
                    }
                ],
            },
        )
    )

    result = payload["results"][0]
    assert result["status"] == "completed"
    assert result["settings"] == {"detail": "more"}
    assert result["diagnostics"] == {"native_metric": "fixture"}
    assert result["canonical_boundaries"] == [
        {"key": 1, "kind": "point", "candidate_ids": ["between"]},
        {
            "key": 2,
            "kind": "span",
            "candidate_ids": ["same-place-time", "within"],
        },
    ]
    assert [section["unit_ids"] for section in result["sections"]] == [
        ["cue-000000"],
        ["cue-000001", "cue-000002"],
        ["cue-000002", "cue-000003"],
    ]
    assert [set(section) for section in result["sections"]] == [
        {"index", "unit_ids"},
        {"index", "unit_ids"},
        {"index", "unit_ids"},
    ]
    assert set(payload["source"]) == {
        "scratch",
        "filename",
        "mime",
        "size",
        "source_kind",
        "snapshot_hash",
        "language",
    }
    membership = {
        item["unit_id"]: item["section_indexes"] for item in result["unit_membership"]
    }
    assert membership["cue-000001"] == [1]
    assert membership["cue-000002"] == [1, 2]
    assert result["boundaries"][1]["diagnostics"] == {"word_offset": 3}

    topic_action = next(
        action
        for action in root_action_catalog().model_dump(mode="json")["actions"]
        if action["kind"] == "map.find_topic_sections"
    )
    stable_keys = {
        "id",
        "label",
        "description",
        "version",
        "tier",
        "recommended",
    }
    assert [
        {key: engine[key] for key in stable_keys} for engine in payload["engines"]
    ] == topic_action["ui_hints"]["engines"]
    assert all(
        "available" in engine and "error" in engine for engine in payload["engines"]
    )


def test_compare_overlap_membership_matches_durable_ranges_and_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = BoundaryCandidate(
        id="between-a-b",
        locator=BetweenUnits("cue-000000", "cue-000001"),
    )
    fake = _FakeSegmenter("texttiling", (boundary,))
    monkeypatch.setattr(topic_compare, "get_segmenter", lambda engine: fake)
    parsed = topic_compare.parse_scratch_transcript(
        OVERLAPPING_SRT_BYTES,
        filename="overlap.srt",
    )

    durable = lock_transcript_boundaries(
        parsed.snapshot,
        (boundary,),
        timeline={
            "artifact_stable_id": "source_artifact:overlap-fixture",
            "fingerprint": "sha256:" + "a" * 64,
            "duration_ms": 4_000,
        },
    )
    durable_ranges = [
        (item.start_ms, item.end_ms) for item in durable.timeline_ranges.items
    ]
    durable_membership = durable.section_unit_ids

    payload = asyncio.run(
        topic_compare.compare_topic_segmentation_scratch(
            OVERLAPPING_SRT_BYTES,
            {
                "filename": "overlap.srt",
                "variants": [{"id": "overlap", "engine": "texttiling"}],
            },
        )
    )
    result = payload["results"][0]

    assert durable_ranges == [(0, 3_000), (0, 4_000)]
    assert durable_membership == (
        ("cue-000000", "cue-000001"),
        ("cue-000000", "cue-000001", "cue-000002"),
    )
    assert tuple(tuple(section["unit_ids"]) for section in result["sections"]) == (
        durable_membership
    )
    assert result["canonical_boundaries"] == [
        {
            "key": 1,
            "kind": "span",
            "candidate_ids": ["between-a-b"],
        }
    ]
    assert result["boundaries"][0]["canonical_key"] == 1
    assert result["boundaries"][0]["locking_kind"] == "span"


def test_compare_does_not_reselect_a_chunk_touching_only_the_widened_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = BoundaryCandidate(
        id="inside-c2",
        locator=WithinUnit("cue-000002"),
    )
    monkeypatch.setattr(
        topic_compare,
        "get_segmenter",
        lambda engine: _FakeSegmenter(engine, (boundary,)),
    )

    payload = asyncio.run(
        topic_compare.compare_topic_segmentation_scratch(
            CHAINED_OVERLAPPING_SRT_BYTES,
            {
                "filename": "chained-overlap.srt",
                "variants": [{"id": "chained", "engine": "texttiling"}],
            },
        )
    )

    sections = payload["results"][0]["sections"]
    assert [section["unit_ids"] for section in sections] == [
        ["cue-000000", "cue-000001", "cue-000002", "cue-000003"],
        ["cue-000001", "cue-000002", "cue-000003", "cue-000004"],
    ]


def test_scratch_route_is_read_only_and_keeps_engine_failures_in_their_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSegmenter("texttiling", ())

    def resolve(engine: str) -> _FakeSegmenter:
        if engine == "deep_tiling":
            raise RuntimeError("api_key=sk-topic-compare-secret")
        return fake

    monkeypatch.setattr(topic_compare, "get_segmenter", resolve)
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Topic Compare"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    before = _counts(project)

    response = client.post(
        f"/api/projects/{pid}/topic-segmentation/compare-scratch",
        files={"file": ("sample.srt", SRT_BYTES, "application/x-subrip")},
        data={
            "payload": json.dumps(
                {
                    "variants": [
                        {
                            "id": "local-success",
                            "engine": "texttiling",
                            "settings": {"detail": "balanced"},
                        },
                        {
                            "id": "local-failure",
                            "engine": "deep_tiling",
                            "settings": {"detail": "balanced"},
                        },
                    ],
                    "language": "en",
                }
            )
        },
    )

    assert response.status_code == 200, response.text
    assert _counts(project) == before
    assert "sk-topic-compare-secret" not in response.text
    payload = response.json()
    assert payload["source"]["scratch"] is True
    assert payload["source"]["source_kind"] == "timestamped_transcript"
    assert [result["status"] for result in payload["results"]] == [
        "completed",
        "failed",
    ]
    assert payload["results"][0]["sections"][0]["unit_ids"] == [
        "cue-000000",
        "cue-000001",
        "cue-000002",
        "cue-000003",
    ]
    assert payload["errors"][0]["variant_id"] == "local-failure"
    assert "cost" not in payload


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("sample.csv", b"text", "accepts .txt, .srt, and .vtt"),
        ("sample.srt", b"1\nnot a timestamp\ntext\n", "valid timestamp"),
        ("sample.vtt", b"not webvtt\n", "must begin with WEBVTT"),
    ],
)
def test_scratch_parser_rejects_unsupported_or_malformed_transcripts(
    filename: str,
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(topic_compare.TopicSegmentationCompareError, match=message):
        topic_compare.parse_scratch_transcript(content, filename=filename)


def test_topic_compare_route_schema_and_editor_policy_are_explicit(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "ws")
    operation = app.openapi()["paths"][
        "/api/projects/{pid}/topic-segmentation/compare-scratch"
    ]["post"]
    body_schema: dict[str, Any] = operation["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    if "$ref" in body_schema:
        body_schema = app.openapi()["components"]["schemas"][
            body_schema["$ref"].rsplit("/", 1)[-1]
        ]
    assert set(body_schema["required"]) == {"file", "payload"}
    assert body_schema["properties"]["file"] == {
        "type": "string",
        "contentMediaType": "application/octet-stream",
        "title": "File",
    }
    assert body_schema["properties"]["payload"]["type"] == "string"

    policy = next(
        entry
        for entry in BASE_ENDPOINT_CATALOG
        if entry.route_name == "topic_segmentation_compare_scratch"
    )
    assert policy.route_owner == "tenant"
    assert policy.method == "POST"
    assert policy.auth == "session_or_pat"
    assert policy.project_role == "editor"
    assert policy.resolvers == ()
    assert policy.reserves_funding is False
