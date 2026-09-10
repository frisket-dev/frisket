from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_evidence_link, sheet_cited_column_ids


PROJECT_ID = "citedcols"


def _seed(ws: Path) -> dict[str, Any]:
    project = Project.create(ws / f"{PROJECT_ID}.frisket", name="Cited Columns")
    try:
        sheet_id = project.add_sheet("Filings")
        cols = {
            "name": project.add_column(sheet_id, "Name", type="text"),
            "amount": project.add_column(
                sheet_id, "Amount", type="text", ai_generated=True
            ),
            "notes": project.add_column(
                sheet_id, "Notes", type="text", ai_generated=True
            ),
        }
        row_ids = project.add_rows(
            sheet_id,
            [{"name": "Acme"}, {"name": "Beta"}],
            {"name": cols["name"]},
        )

        # A plain sheet with zero evidence links (the regression guard).
        plain_sheet_id = project.add_sheet("Plain")
        project.add_column(plain_sheet_id, "x", type="text")

        # ACTIVE evidence on 'amount' for both rows.
        for row_id in row_ids:
            record_evidence_link(
                project,
                subject_kind="cell_value",
                subject_ref={
                    "kind": "run_result",
                    "row_id": row_id,
                    "column_id": cols["amount"],
                    "op_id": None,
                    "run_id": None,
                },
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=cols["amount"],
                status="active",
                spans=[],
            )
        # A STALE link on 'notes' — must NOT count toward the signal.
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "run_result",
                "row_id": row_ids[0],
                "column_id": cols["notes"],
                "op_id": None,
                "run_id": None,
            },
            sheet_id=sheet_id,
            row_id=row_ids[0],
            column_id=cols["notes"],
            status="stale",
            spans=[],
        )
        # An evidence link on ANOTHER sheet's row/column — must not leak into
        # this sheet's signal even though it shares no FK constraint.
        other_sheet_id = project.add_sheet("Other")
        other_col = project.add_column(
            other_sheet_id, "y", type="text", ai_generated=True
        )
        other_row = project.add_rows(other_sheet_id, [{"y": "z"}], {"y": other_col})[0]
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "run_result",
                "row_id": other_row,
                "column_id": other_col,
                "op_id": None,
                "run_id": None,
            },
            sheet_id=other_sheet_id,
            row_id=other_row,
            column_id=other_col,
            status="active",
            spans=[],
        )
        project.db.commit()
        return {
            "sheet_id": sheet_id,
            "plain_sheet_id": plain_sheet_id,
            "other_sheet_id": other_sheet_id,
            "cols": cols,
            "row_ids": row_ids,
        }
    finally:
        project.close()


def _list_sheets(client: TestClient) -> dict[int, dict[str, Any]]:
    r = client.get(f"/api/projects/{PROJECT_ID}/sheets")
    assert r.status_code == 200, r.text
    return {int(s["id"]): s for s in r.json()}


def test_sheet_list_exposes_cited_column_ids_for_active_evidence_only(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    by_id = _list_sheets(client)

    # 'notes' carries only a STALE link, so it must not appear — only
    # 'amount' (active, both rows) is in the DISTINCT ascending signal.
    assert by_id[ids["sheet_id"]]["cited_column_ids"] == [ids["cols"]["amount"]]


def test_sheet_with_zero_evidence_links_reports_empty_list_not_absent_key(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    by_id = _list_sheets(client)

    assert "cited_column_ids" in by_id[ids["plain_sheet_id"]]
    assert by_id[ids["plain_sheet_id"]]["cited_column_ids"] == []


def test_helper_is_distinct_ascending_and_sheet_scoped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    project = Project(ws / f"{PROJECT_ID}.frisket")
    try:
        assert sheet_cited_column_ids(project, ids["sheet_id"]) == [
            ids["cols"]["amount"]
        ]
        assert sheet_cited_column_ids(project, ids["plain_sheet_id"]) == []
        # A different sheet's active link must not leak across sheet_id.
        assert sheet_cited_column_ids(project, ids["other_sheet_id"]) == [
            project.db.execute(
                "SELECT column_id FROM evidence_links WHERE sheet_id=?",
                (ids["other_sheet_id"],),
            ).fetchone()["column_id"]
        ]
    finally:
        project.close()
