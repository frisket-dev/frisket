"""Seam tests for src/frisket/engine/runner/grounding.py (CP3 of the map_runner
split): pin the ``image_part`` explicit ``max_image_bytes`` threading, the
mime-detection branches, and the segment-list skip in
``inject_grounding_segments`` that a coverage review found untested at the
old ``MapRunner`` call sites."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.runner.grounding import (
    grounding_enabled,
    image_part,
    inject_grounding_segments,
    unwrap_grounded_value,
)
from frisket.engine.store import Project

# A minimal but valid 1x1 PNG (same fixture shape as
# tests/test_extract_list_item_grounding.py's PNG_1X1).
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def project(tmp_path: Path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


# --------------------------------------------------------------------------- #
# image_part
# --------------------------------------------------------------------------- #


def test_image_part_blob_branch_returns_b64_and_mime(project: Project):
    digest = project.add_blob(PNG_1X1, filename="x.png", mime="image/png")
    val = {"blob": digest, "mime": "image/png"}
    result = image_part(project, val, max_image_bytes=len(PNG_1X1) + 1)
    assert result is not None
    assert result["mime"] == "image/png"
    assert result["__image_b64__"]


def test_image_part_blob_branch_over_limit_returns_none(project: Project):
    digest = project.add_blob(PNG_1X1, filename="x.png", mime="image/png")
    val = {"blob": digest, "mime": "image/png"}
    # A LOW explicit limit: the fixture (well over 10 bytes) must be rejected.
    assert image_part(project, val, max_image_bytes=10) is None


@pytest.mark.parametrize(
    "suffix,expected_mime",
    [
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
    ],
)
def test_image_part_path_branch_mime_by_suffix(
    project: Project, tmp_path: Path, suffix: str, expected_mime: str
):
    path = tmp_path / f"img{suffix}"
    path.write_bytes(PNG_1X1)
    result = image_part(project, str(path), max_image_bytes=len(PNG_1X1) + 1)
    assert result is not None
    assert result["mime"] == expected_mime
    assert result["__image_b64__"]


def test_image_part_path_branch_over_limit_returns_none(
    project: Project, tmp_path: Path
):
    path = tmp_path / "img.png"
    path.write_bytes(PNG_1X1)
    assert image_part(project, str(path), max_image_bytes=10) is None


# --------------------------------------------------------------------------- #
# inject_grounding_segments: segment-list skip
# --------------------------------------------------------------------------- #


def test_inject_grounding_segments_skips_when_segment_list_already_present(
    project: Project,
):
    sheet = project.add_sheet("data")
    (row_id,) = project.add_rows(sheet, [{}], {})
    spec = {
        "action_kind": "map.extract",
        "sheet_id": sheet,
        "grounding": {"enabled": True},
    }
    existing_segments = [{"text": "hi", "start": 0.0}, {"text": "yo", "start": 1.0}]
    values = {"already_segmented": existing_segments}
    before = dict(values)
    inject_grounding_segments(project, spec, row_id, values)
    # No new key added (in particular no "transcript_segments" injected) --
    # the existing segment-list column already satisfies the model-facing
    # numbered-segments contract, so injection must be skipped outright.
    assert values == before
    assert "transcript_segments" not in values


# --------------------------------------------------------------------------- #
# grounding_enabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec",
    [
        {"action_kind": "map.classify", "grounding": {"enabled": True}},
        {"action_kind": "map.extract", "grounding": "not-a-dict"},
        {"action_kind": "map.extract", "grounding": {"enabled": False}},
    ],
)
def test_grounding_enabled_false_cases(spec: dict) -> None:
    assert grounding_enabled(spec) is False


def test_grounding_enabled_true_case() -> None:
    assert grounding_enabled(
        {"action_kind": "map.extract", "grounding": {"enabled": True}}
    )


# --------------------------------------------------------------------------- #
# unwrap_grounded_value
# --------------------------------------------------------------------------- #


def test_unwrap_grounded_value_none_raw_value_returns_none_none() -> None:
    assert unwrap_grounded_value("field", None) == (None, None)


def test_unwrap_grounded_value_bare_value_synthesizes_evidence_missing() -> None:
    value, sidecar = unwrap_grounded_value("field", "a bare string")
    assert value == "a bare string"
    assert sidecar == {
        "field": "field",
        "evidence": [],
        "warnings": ["evidence_missing"],
        "raw": {},
    }
