from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    list_cell_evidence,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
    resolve_evidence_viewer,
)
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results

from helpers import stub_rapidocr_run_scope
from frisket.engine.executor import run_action_spec


PROJECT_ID = "project-evidence-stale-on-reprocess"

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --------------------------------------------------------------------------- #
# (1) staleness-on-reprocess -- OCR executor harness
# --------------------------------------------------------------------------- #


def _ocr_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    engine: str,
    idempotency_key: str,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "params": {
            "source": "media",
            "engine": engine,
            "dpi": 200,
        },
        "idempotency_key": idempotency_key,
    }
    if overwrite_existing:
        # a re-OCR into the SAME output column reuses the prior AI-generated
        # column (censusfix report 3b's overwrite_existing output intent) --
        # otherwise the precheck rejects it as a column collision.
        action["replace_existing"] = True
    return action


def _seed_image_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "ocr-reprocess.frisket", name="OCR reprocess")
    sheet_id = project.add_sheet("Scans")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="image"),
    }
    blob = project.add_blob(
        PNG_1X1,
        filename="scan.png",
        mime="image/png",
        source_url="https://cdn.example/scan.png",
        metadata=owned_media_metadata_document(
            probe={"width": 1, "height": 1, "kind": "image"}
        ),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Scan 1",
                "media": media_cell(
                    blob,
                    mime="image/png",
                    filename="scan.png",
                ),
            }
        ],
        cols,
    )
    return {"project": project, "sheet_id": sheet_id, "row_ids": row_ids, "blob": blob}


def _fake_page_images():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec, scratch
        return [path]

    return fake_page_images


def _fake_engine(text: str):
    async def fake_ocr(self, page_paths, scratch, language=None):
        del self, scratch, language
        return [
            {
                "text": text,
                "blocks": [
                    {
                        "text": text,
                        "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.98,
                    }
                ],
            }
        ]

    return fake_ocr


def _fake_sidecar_engine(text: str):
    async def fake_sidecar(self, engine, pages, ctx):
        del self, engine, pages, ctx
        return [
            {
                "text": text,
                "blocks": [
                    {
                        "text": text,
                        "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.95,
                    }
                ],
            }
        ]

    return fake_sidecar


def _text_column_id(project: Project, sheet_id: int) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='ocr_text'",
        (sheet_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_reocr_with_different_engine_marks_prior_link_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    seeded = _seed_image_project(tmp_path)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_id = seeded["row_ids"][0]
    monkeypatch.setattr(OcrEngines, "_page_images", _fake_page_images())
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", _fake_engine("invoice total 42"))
    stub_rapidocr_run_scope(monkeypatch)
    monkeypatch.setattr(
        OcrEngines, "_ocr_sidecar", _fake_sidecar_engine("invoice total 99")
    )
    # The sidecar engine resolves to the models-gateway target, whose
    # liveness is a real config read now.
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    try:
        first = run_action_spec(
            project,
            _ocr_action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                engine="rapidocr",
                idempotency_key="media_ocr@sha256:reprocess-rapidocr",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors

        text_column_id = _text_column_id(project, sheet_id)
        first_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            project_id=PROJECT_ID,
        )
        assert len(first_evidence["links"]) == 1
        first_stable_id = first_evidence["links"][0]["stable_id"]
        assert first_evidence["links"][0]["status"] == "active"

        # Re-OCR the SAME cell with a DIFFERENT engine (rapidocr -> the dots.mocr
        # sidecar engine): distinct params -> distinct params_hash -> a
        # genuine re-execution (not an idempotency replay), producing a
        # brand-new active evidence link.
        second_action = _ocr_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            engine="dots.mocr",
            idempotency_key="media_ocr@sha256:reprocess-dots.mocr",
            overwrite_existing=True,
        )
        second = run_action_spec(project, second_action, project_id=PROJECT_ID)
        assert second.status == "completed", second.errors

        all_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            include_stale=True,
            project_id=PROJECT_ID,
        )
        by_stable_id = {link["stable_id"]: link for link in all_evidence["links"]}
        assert len(by_stable_id) == 2
        # the FIRST engine's link is superseded by the re-OCR -- stale, not
        # silently kept "active" and not deleted (still resolvable/visible).
        assert by_stable_id[first_stable_id]["status"] == "stale"
        assert all_evidence["stale_count"] == 1
        active_links = [
            link for link in all_evidence["links"] if link["status"] == "active"
        ]
        assert len(active_links) == 1
        assert active_links[0]["stable_id"] != first_stable_id

        # the stale link is still resolvable (viewable), never deleted.
        stale_viewer = resolve_evidence_viewer(
            project, first_stable_id, project_id=PROJECT_ID
        )
        assert stale_viewer["link"]["status"] == "stale"
        assert stale_viewer["link"]["stale_reason"]
    finally:
        project.close()


def test_reocr_with_unchanged_params_replay_leaves_link_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_image_project(tmp_path)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_id = seeded["row_ids"][0]
    monkeypatch.setattr(OcrEngines, "_page_images", _fake_page_images())
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", _fake_engine("invoice total 42"))
    stub_rapidocr_run_scope(monkeypatch)
    try:
        action = _ocr_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            engine="rapidocr",
            idempotency_key="media_ocr@sha256:no-change-replay",
        )
        first = run_action_spec(project, action, project_id=PROJECT_ID)
        assert first.status == "completed", first.errors

        text_column_id = _text_column_id(project, sheet_id)
        before = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            include_stale=True,
            project_id=PROJECT_ID,
        )
        assert len(before["links"]) == 1
        stable_id = before["links"][0]["stable_id"]

        # SAME action, SAME idempotency key -> idempotency replay, never
        # re-enters the evidence writer. The existing link must stay fresh.
        replay = run_action_spec(project, action, project_id=PROJECT_ID)
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id

        after = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            include_stale=True,
            project_id=PROJECT_ID,
        )
        assert len(after["links"]) == 1
        assert after["links"][0]["stable_id"] == stable_id
        assert after["links"][0]["status"] == "active"
        assert after["stale_count"] == 0
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# (2) text_layer_hash mismatch flag at render time
# --------------------------------------------------------------------------- #


def _seed_text_layer_link(tmp_path: Path, *, initial_text: str) -> dict[str, Any]:
    project = Project.create(tmp_path / "hash-mismatch.frisket", name="Hash mismatch")
    sheet_id = project.add_sheet("Docs")
    source_col = project.add_column(sheet_id, "source", type="file")
    output_col = project.add_column(
        sheet_id, "markdown", type="text", ai_generated=True
    )
    row_id = project.add_rows(
        sheet_id, [{"source": "doc.pdf"}], {"source": source_col}
    )[0]
    op_id = project.append_op("media.to_markdown", {"kind": "media.to_markdown"})
    run_store = RunResultStore(project)
    run_id = run_store.start_run(
        op_id, sheet_id, "media.to_markdown", total_rows=1, row_ids=[row_id]
    )
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_id, "column_id": output_col, "value": initial_text}],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, output_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, output_col, row_ids=[row_id])

    artifact = record_source_artifact(
        project, artifact_kind="text", media_type="text/plain"
    )
    text_hash = _text_hash(initial_text)
    # The offsets index the output markdown cell, so declare that coordinate
    # surface as media.to_markdown now does.
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=text_hash,
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet_id,
        text_row_id=row_id,
        text_column_id=output_col,
        value_ref=refs[row_id],
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        char_start=0,
        char_end=len(initial_text),
        quote=initial_text,
        text_layer_hash=text_hash,
        text_surface_id=surface["id"],
    )
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=[{"span_id": span["id"]}],
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=output_col,
        run_id=run_id,
        op_id=op_id,
        link_role="media_to_markdown_grounding",
    )
    return {
        "project": project,
        "sheet_id": sheet_id,
        "row_id": row_id,
        "output_col": output_col,
        "op_id": op_id,
        "run_store": run_store,
        "link": link,
    }


def _text_hash(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_text_layer_hash_mismatch_flagged_when_cell_reprocessed_without_staleness_marking(
    tmp_path: Path,
) -> None:
    seeded = _seed_text_layer_link(tmp_path, initial_text="# Original heading")
    project: Project = seeded["project"]
    try:
        # sanity: unmodified cell resolves with no mismatch.
        viewer = resolve_evidence_viewer(
            project, seeded["link"]["stable_id"], project_id=PROJECT_ID
        )
        assert viewer["link"]["text_layer_hash_mismatch"] is False

        # Simulate a producer that reprocesses the SAME cell (a second run
        # overwrites the markdown column) WITHOUT going through
        # `mark_evidence_stale_for_cell_refs` -- exactly the documented gap
        # ("fires from only 2 of many mutation sites"). The render path must
        # independently catch the drift via the stored text_layer_hash.
        run_store: RunResultStore = seeded["run_store"]
        second_run_id = run_store.start_run(
            seeded["op_id"],
            seeded["sheet_id"],
            "media.to_markdown",
            total_rows=1,
            row_ids=[seeded["row_id"]],
        )
        write_claimed_test_results(
            project,
            second_run_id,
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": seeded["output_col"],
                    "value": "# Re-extracted heading (different engine)",
                }
            ],
        )
        run_store.finish_run(second_run_id)
        run_store.point_column_at_run(
            seeded["op_id"], seeded["output_col"], second_run_id
        )

        mismatched_viewer = resolve_evidence_viewer(
            project, seeded["link"]["stable_id"], project_id=PROJECT_ID
        )
        # the link's own status is untouched by this direct simulation (proving
        # the flag is an INDEPENDENT check, not a re-derivation of `status`);
        # a flag the viewer can show, not a silent hide, not a delete.
        assert mismatched_viewer["link"]["status"] == "active"
        assert mismatched_viewer["link"]["text_layer_hash_mismatch"] is True
        # additive: existing consumers of the span-level payload are unaffected.
        assert mismatched_viewer["artifacts"][0]["spans"][0]["quote"] == (
            "# Original heading"
        )
    finally:
        project.close()


def test_text_layer_hash_matches_when_cell_never_reprocessed(
    tmp_path: Path,
) -> None:
    seeded = _seed_text_layer_link(tmp_path, initial_text="# Stable heading")
    project: Project = seeded["project"]
    try:
        viewer = resolve_evidence_viewer(
            project, seeded["link"]["stable_id"], project_id=PROJECT_ID
        )
        assert viewer["link"]["text_layer_hash_mismatch"] is False
    finally:
        project.close()
