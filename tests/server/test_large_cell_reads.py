from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.current_value_windows import read_current_value_window
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.value_codec import PreparedContentRef
from frisket.engine.store.runs import RunResultStore
from frisket.server.app import create_app
from frisket.server.services.project_qa_sources import (
    find_source_text,
    read_source_text,
)
from frisket.server.services.sheet_grid import (
    _sheet_data_payload,
    sheet_columns_payload,
)
from helpers import write_claimed_test_results


def _project(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "large"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("documents")
    text_column = project.add_column(sheet_id, "body", type="text")
    json_column = project.add_column(sheet_id, "boxes", type="json")
    return client, project_id, project, sheet_id, text_column, json_column


def test_bounded_grid_omits_large_values_but_exact_api_and_filter_stay_exact(
    tmp_path: Path,
) -> None:
    client, project_id, project, sheet_id, text_column, json_column = _project(tmp_path)
    text = "😀" + "a" * (20 * 1024 * 1024) + "TAIL-UNIQUE"
    boxes = {"boxes": "b" * (5 * 1024 * 1024)}
    [row_id] = project.add_rows(
        sheet_id,
        [{"body": text, "boxes": boxes}],
        {"body": text_column, "boxes": json_column},
    )

    bounded = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"value_mode": "bounded"},
    )
    assert bounded.status_code == 200, bounded.text
    body = bounded.json()
    [row] = body["rows"]
    assert str(text_column) not in row["cells"]
    assert str(json_column) not in row["cells"]
    text_meta = row["meta"][str(text_column)]["bounded_value"]
    json_meta = row["meta"][str(json_column)]["bounded_value"]
    assert text_meta == {
        "preview": text[:2048],
        "value_kind": "text",
        "total_characters": len(text),
        "total_bytes": len(text.encode()),
        "retrieval_revision": text_meta["retrieval_revision"],
        "truncated": True,
        "manual_editable": False,
        "manual_edit_byte_limit": 65_536,
    }
    assert text_meta["retrieval_revision"] > 0
    assert len(text_meta["preview"].encode()) <= 8192
    assert json_meta["value_kind"] == "json"
    assert json_meta["preview"].startswith('{"boxes":')
    assert len(bounded.content) < 20_000

    exact = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"row_ids": str(row_id)},
    )
    assert exact.status_code == 200, exact.text
    exact_row = exact.json()["rows"][0]
    assert exact_row["cells"][str(text_column)] == text
    assert exact_row["cells"][str(json_column)] == boxes
    assert "bounded_value" not in exact_row["meta"][str(text_column)]

    filtered = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={
            "value_mode": "bounded",
            "filter": json.dumps({"body": {"contains": "TAIL-UNIQUE"}}),
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()["rows"]] == [row_id]


def test_text_windows_are_revision_bound_and_download_is_exact(tmp_path: Path) -> None:
    client, project_id, project, sheet_id, text_column, _json_column = _project(
        tmp_path
    )
    text = ("ab😀cd\n" * 20_000) + "tail"
    [row_id] = project.add_rows(sheet_id, [{"body": text}], {"body": text_column})
    bounded = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"value_mode": "bounded", "column_ids": str(text_column)},
    ).json()
    revision = bounded["rows"][0]["meta"][str(text_column)]["bounded_value"][
        "retrieval_revision"
    ]

    first = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window",
        params={"limit": 37},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["text"] == text[:37]
    assert first_body["range"] == {"start": 0, "end": 37}
    assert first_body["next_start"] == 37
    assert first_body["retrieval_revision"] == revision

    missing_revision = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window",
        params={"start": 37, "limit": 41},
    )
    assert missing_revision.status_code == 400
    second = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window",
        params={"start": 37, "limit": 41, "revision": revision},
    )
    assert second.status_code == 200, second.text
    assert second.json()["text"] == text[37:78]

    download = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/download",
        params={"revision": revision},
    )
    assert download.status_code == 200, download.text
    assert download.content == text.encode()
    assert download.headers["content-disposition"].startswith("attachment;")

    project.apply_edits(
        [{"row_id": row_id, "column_id": text_column, "value": "replacement"}]
    )
    stale = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window",
        params={"start": 37, "limit": 41, "revision": revision},
    )
    assert stale.status_code == 409
    stale_download = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/download",
        params={"revision": revision},
    )
    assert stale_download.status_code == 409


def test_prepared_text_windows_preserve_page_separators_without_full_assembly(
    tmp_path: Path,
) -> None:
    client, project_id, project, sheet_id, text_column, _json_column = _project(
        tmp_path
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        page_count=4,
    )
    op_id = project.append_op("media.ocr")
    project.db.execute("BEGIN IMMEDIATE")
    ref = PreparedContentStore(project).stage_reference(
        source_artifact_id=int(artifact["id"]),
        producing_op_id=op_id,
        pages=[
            PreparedPageDraft(1, "a" * (64 * 1024)),
            PreparedPageDraft(2, "😀middle\0after"),
            PreparedPageDraft(3, ""),
            PreparedPageDraft(4, "tail"),
        ],
    )
    project.db.commit()
    [row_id] = project.add_rows(
        sheet_id,
        [{"body": PreparedContentRef(ref.ref_id)}],
        {"body": text_column},
    )
    logical = "a" * (64 * 1024) + "\n\n😀middle\0after\n\n\n\ntail"

    bounded = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"value_mode": "bounded", "column_ids": str(text_column)},
    )
    assert bounded.status_code == 200, bounded.text
    descriptor = bounded.json()["rows"][0]["meta"][str(text_column)]["bounded_value"]
    assert descriptor["value_kind"] == "text"
    assert descriptor["total_characters"] == len(logical)
    assert descriptor["total_bytes"] == len(logical.encode())

    start = 64 * 1024 - 1
    window = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window",
        params={
            "start": start,
            "limit": 14,
            "revision": descriptor["retrieval_revision"],
        },
    )
    assert window.status_code == 200, window.text
    assert window.json()["text"] == logical[start : start + 14]
    source = read_source_text(
        project, (sheet_id, row_id, text_column), start=start, limit=40
    )
    assert source["length"] == len(logical)
    assert source["text"] == logical[start : start + 40]

    download = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/download",
        params={"revision": descriptor["retrieval_revision"]},
    )
    assert download.content == logical.encode()


def test_hidden_or_non_text_cells_fail_closed(tmp_path: Path) -> None:
    client, project_id, project, sheet_id, text_column, _json_column = _project(
        tmp_path
    )
    number_column = project.add_column(sheet_id, "count", type="integer")
    [row_id] = project.add_rows(
        sheet_id,
        [{"body": "visible", "count": 3}],
        {"body": text_column, "count": number_column},
    )
    revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row_id, number_column),
        ).fetchone()[0]
    )
    non_text = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{number_column}/text-window"
    )
    assert non_text.status_code == 422
    non_text_download = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{number_column}/download",
        params={"revision": revision},
    )
    assert non_text_download.status_code == 422

    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
    project.db.commit()
    hidden = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{text_column}/text-window"
    )
    assert hidden.status_code == 404


def test_nul_and_split_utf8_characters_keep_absolute_offsets_and_exact_download(
    tmp_path: Path,
) -> None:
    client, project_id, project, sheet_id, column, _json_column = _project(tmp_path)
    # The emoji crosses the 64 KiB BLOB fallback chunk boundary.
    text = "ab\0" + "x" * 65_532 + "😀tail\0NEEDLE" + "z" * 10_000
    [row] = project.add_rows(sheet_id, [{"body": text}], {"body": column})
    bounded = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"value_mode": "bounded", "column_ids": str(column)},
    ).json()
    descriptor = bounded["rows"][0]["meta"][str(column)]["bounded_value"]
    assert descriptor["preview"] == text[:2048]
    assert descriptor["total_characters"] == len(text)
    assert descriptor["total_bytes"] == len(text.encode())
    revision = descriptor["retrieval_revision"]
    start = text.index("😀") - 1
    window = client.get(
        f"/api/projects/{project_id}/cells/{row}/{column}/text-window",
        params={"start": start, "limit": 20, "revision": revision},
    )
    assert window.status_code == 200, window.text
    assert window.json()["text"] == text[start : start + 20]
    assert window.json()["range"] == {"start": start, "end": start + 20}
    source = read_source_text(project, (sheet_id, row, column), start=start, limit=20)
    assert source["length"] == len(text)
    assert source["text"] == text[start : start + 20]
    found = find_source_text(project, (sheet_id, row, column), "NEEDLE")
    assert found["matches"][0]["start"] == text.index("NEEDLE")
    anchored = read_source_text(
        project, (sheet_id, row, column), anchor="NEEDLE", limit=30
    )
    assert "NEEDLE" in anchored["text"]
    download = client.get(
        f"/api/projects/{project_id}/cells/{row}/{column}/download",
        params={"revision": revision},
    )
    assert download.content == text.encode()


def test_window_reader_does_not_allocate_a_whole_large_python_value(
    tmp_path: Path,
) -> None:
    _client, _pid, project, sheet_id, column, _json_column = _project(tmp_path)
    [row] = project.add_rows(
        sheet_id, [{"body": "x" * (5 * 1024 * 1024) + "😀tail"}], {"body": column}
    )
    tracemalloc.start()
    try:
        window = read_current_value_window(
            project.db, sheet_id=sheet_id, row_id=row, column_id=column, limit=31
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert window.text == "x" * 31
    assert window.total_characters == 5 * 1024 * 1024 + 5
    assert peak < 1024 * 1024


def test_manual_edit_limit_is_utf8_bytes_and_small_cells_keep_exact_values(
    tmp_path: Path,
) -> None:
    client, pid, project, sheet_id, column, _json_column = _project(tmp_path)
    exact = "😀" * 16_384
    large = exact + "a"
    rows = project.add_rows(
        sheet_id, [{"body": exact}, {"body": large}], {"body": column}
    )
    body = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"value_mode": "bounded"},
    ).json()
    assert body["rows"][0]["id"] == rows[0]
    assert body["rows"][0]["cells"][str(column)] == exact
    assert "bounded_value" not in body["rows"][0]["meta"][str(column)]
    descriptor = body["rows"][1]["meta"][str(column)]["bounded_value"]
    assert descriptor["manual_editable"] is False
    assert descriptor["manual_edit_byte_limit"] == 65_536
    assert descriptor["total_bytes"] == 65_537
    assert len(descriptor["preview"].encode()) == 8192


def test_generated_grid_metadata_does_not_decode_large_results(tmp_path: Path) -> None:
    _client, _pid, project, sheet_id, column, _json_column = _project(tmp_path)
    row, small_row = project.add_rows(
        sheet_id, [{"body": "input"}, {"body": "small"}], {"body": column}
    )
    output = project.add_column(sheet_id, "generated", type="json", ai_generated=True)
    op = project.append_op("map.extract", label="Fixture OCR")
    run = RunResultStore(project).start_run(
        op, sheet_id, "map.extract", row_ids=[row, small_row]
    )
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row,
                "column_id": output,
                "value": {"boxes": "x" * (5 * 1024 * 1024)},
                "outcome": "ok",
                "confidence": 0.7,
            },
            {
                "row_id": small_row,
                "column_id": output,
                "value": {"small": 1},
                "outcome": "ok",
            },
        ],
    )
    columns = [col for col in project.columns(sheet_id) if col["id"] == output]
    tracemalloc.start()
    try:
        payload = _sheet_data_payload(
            project,
            sheet_id,
            columns,
            [row],
            total=None,
            include_scope_metadata=False,
            cell_projection=True,
            value_mode="bounded",
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    meta = payload["rows"][0]["meta"][str(output)]
    assert meta["confidence"] == 0.7
    assert meta["bounded_value"]["value_kind"] == "json"
    assert str(output) not in payload["rows"][0]["cells"]
    assert peak < 1024 * 1024

    project.apply_edits(
        [
            {"row_id": row, "column_id": output, "value": {"manual": True}},
            {"row_id": small_row, "column_id": output, "value": {"small": 2}},
        ]
    )
    tracemalloc.start()
    try:
        payload = _sheet_data_payload(
            project,
            sheet_id,
            columns,
            [row, small_row],
            total=2,
            cell_projection=True,
            value_mode="bounded",
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    assert payload["columns"][0]["replay_pending_count"] is None
    assert payload["columns"][0]["replay_comparison_available"] is False
    listing = next(
        col for col in sheet_columns_payload(project, sheet_id) if col["id"] == output
    )
    assert listing["replay_pending_count"] is None
    assert listing["replay_comparison_available"] is False
    large_meta = payload["rows"][0]["meta"][str(output)]
    small_meta = payload["rows"][1]["meta"][str(output)]
    assert large_meta["replay_comparison_unavailable"] is True
    assert "pending_value" not in large_meta
    assert small_meta["pending_value"]["fresh_value"] == {"small": 1}
    exact = _sheet_data_payload(project, sheet_id, columns, [row, small_row], total=2)
    assert exact["columns"][0]["replay_pending_count"] == 2
    assert (
        len(
            exact["rows"][0]["meta"][str(output)]["pending_value"]["fresh_value"][
                "boxes"
            ]
        )
        == 5 * 1024 * 1024
    )


def test_windows_reassemble_long_lines_tables_and_line_boundaries_exactly(
    tmp_path: Path,
) -> None:
    client, pid, project, sheet_id, column, _json_column = _project(tmp_path)
    text = "line😀\n" * 500 + "| x " * 250_000 + "tail"
    [row] = project.add_rows(sheet_id, [{"body": text}], {"body": column})
    start = 0
    revision = None
    fragments = []
    while True:
        params = {"start": start, "limit": 16_384}
        if revision is not None:
            params["revision"] = revision
        response = client.get(
            f"/api/projects/{pid}/cells/{row}/{column}/text-window", params=params
        )
        assert response.status_code == 200, response.text
        window = response.json()
        assert len(window["text"].encode()) <= 65_536
        assert window["text"].count("\n") <= 400
        assert window["range"]["start"] == start
        fragments.append(window["text"])
        revision = window["retrieval_revision"]
        if window["reached_end"]:
            assert window["next_start"] is None
            break
        start = window["next_start"]
        assert start == window["range"]["end"]
    assert "".join(fragments) == text
