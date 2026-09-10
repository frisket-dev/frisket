from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    case_env,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

PROJECT_ID = "project-media-to-markdown"
HTML = (
    "<html><body><h1>Quarterly Report</h1>"
    "<p>Revenue doubled this year.</p></body></html>"
)


def _patch_markitdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from frisket.engine.executor.document_convert import _BoundDocumentConverter

    async def fake_markitdown(
        self: _BoundDocumentConverter, path: Path, scratch: Path
    ) -> str:
        return f"# Converted {path.name}\n\nDocument body"

    monkeypatch.setattr(_BoundDocumentConverter, "_convert_markitdown", fake_markitdown)


def _to_markdown_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    engine: str = "markitdown",
    output_name: str = "markdown",
    idempotency_key: str = "media_to_markdown@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": (input_columns or ["doc"])[0],
        "engine": engine,
    }
    if input_columns is not None and len(input_columns) != 1:
        params["source"] = input_columns
    scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.to_markdown",
        "scope": scope,
        "params": params,
        "output_names": {"markdown": output_name},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Documents")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "doc": project.add_column(sheet_id, "doc", type="file"),
    }
    blobs = [
        project.add_blob(
            f"{HTML}<p>{label}</p>".encode("utf-8"),
            filename=f"{label}.html",
            mime="text/html",
            source_url=f"https://docs.example/{label}.html",
        )
        for label in ("report1", "report2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Report 1",
                "doc": media_cell(
                    blobs[0],
                    mime="text/html",
                    filename="report1.html",
                ),
            },
            {
                "title": "Report 2",
                "doc": media_cell(
                    blobs[1],
                    mime="text/html",
                    filename="report2.html",
                ),
            },
        ],
        cols,
    )
    return {"sheet_id": sheet_id, "row_ids": row_ids, "blobs": blobs}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _to_markdown_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _to_markdown_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["missing"],
        idempotency_key="media_to_markdown@sha256:missing-source",
    )


def _multi_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _to_markdown_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["doc", "title"],
        idempotency_key="media_to_markdown@sha256:multi-input",
    )


def _bad_engine_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _to_markdown_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        engine="not_a_real_engine",
        idempotency_key="media_to_markdown@sha256:bad-engine",
    )


def _source_collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _to_markdown_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        output_name="title",
        idempotency_key="media_to_markdown@sha256:collision",
    )


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    assert [output.name for output in result.outputs] == ["markdown"]
    output = result.outputs[0]
    assert output.kind == "column"
    assert output.ref["kind"] == "map_result_column"
    assert output.ref["value_hash"].startswith("sha256:")
    assert output.ref["format"] == "markdown"

    columns = _columns(project, sheet_id)
    assert columns["markdown"]["type"] == "text"
    assert columns["markdown"]["format"] == "markdown"
    values = project.get_values(
        sheet_id, columns["markdown"]["id"], row_ids=seeded["row_ids"]
    )
    assert all(value.startswith("# Converted") for value in values.values())

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.status == "completed"
    assert receipt.provider_use == [
        {
            "provider": "local",
            "model": "markitdown",
            "engine": "markitdown",
            "service": "document.convert",
            "external_api": False,
            "operation_call_count": len(seeded["row_ids"]),
            "model_call_count": 0,
            "cost_actual": 0.0,
        }
    ]
    refs = [item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence]
    assert any(
        ref.get("kind") == "source_column"
        and ref.get("name") == "doc"
        and ref.get("row_ids") == seeded["row_ids"]
        for ref in refs
    )
    reads = [ref for ref in refs if ref.get("kind") == "document_convert_read"]
    assert [ref["document_read"]["blob_hash"] for ref in reads] == seeded["blobs"]
    assert all(ref["engine"] == "markitdown" for ref in reads)


def _conflicting_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary action, different output name.
    return _to_markdown_action(
        sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"], output_name="doc_md"
    )


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='markdown'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


CASES = [
    ExecutorCase(
        kind="media.to_markdown",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_external_provider",
                    "create_generated_columns",
                    "write_run_results",
                    "write_model_calls",
                    "write_trace",
                    "write_map_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "external_rows_failed",
                    "stale_replay",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                }
            ),
            cost_policy_kind="external_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_markitdown,
        gates=(
            Gate(
                "missing_source",
                _missing_source_action,
                "invalid_input_ref",
            ),
            Gate("invalid_input_ref", _multi_input_action, "invalid_action_request"),
            Gate(
                "invalid_to_markdown_engine",
                _bad_engine_action,
                "invalid_action_request",
            ),
            Gate(
                "output_column_exists",
                _source_collision_action,
                "output_column_exists",
            ),
        ),
        expect_counts={
            "columns": 1,
            "runs": 1,
            "results": 2,
            "model_calls": 0,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            make_conflict=_conflicting_output_action,
            go_stale=_go_stale,
        ),
    )
]


def _mutate_markdown_output_for_replay(project: Project, receipt_id: str) -> None:
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    body = json.loads(receipt_row["body"])
    ref = next(
        item["ref"]
        for item in body["outputs"]
        if item["ref"]["kind"] == "map_result_column"
    )
    assert ref["value_hash"].startswith("sha256:")
    store = RunResultStore(project)
    op_id = project.append_op("map")
    run_id = store.start_run(op_id, ref["sheet_id"], "media.to_markdown")
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": ref["row_ids"][0],
                "column_id": ref["column_id"],
                "value": "structurally drifted markdown",
            }
        ],
    )
    store.finish_run(run_id)
    store.point_column_at_run(op_id, ref["column_id"], run_id)
    project.db.commit()


def test_media_to_markdown_replay_rejects_stored_value_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        _mutate_markdown_output_for_replay(env.project, first.receipt_id)

        replay = env.run_primary()
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"


def test_media_to_markdown_replay_preserves_identity_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_replay_error

    defects = (
        "invalid_ref",
        "missing",
        "renamed",
        "type_changed",
        "format_changed",
        "run_changed",
        "no_output",
    )
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        base = json.loads(receipt_row["body"])
        bound = typed_action_for_request(_make_action(env.seeded))

        for defect in defects:
            body = json.loads(json.dumps(base))
            if defect == "no_output":
                body["outputs"] = [
                    item
                    for item in body["outputs"]
                    if item["ref"]["kind"] != "map_result_column"
                ]
            else:
                ref = next(
                    item["ref"]
                    for item in body["outputs"]
                    if item["ref"]["kind"] == "map_result_column"
                )
                if defect == "invalid_ref":
                    ref["column_id"] = "not-an-integer"
                elif defect == "missing":
                    ref["column_id"] = 2_147_483_647
                elif defect == "renamed":
                    ref["name"] = "renamed_markdown"
                elif defect == "type_changed":
                    ref["type"] = "json"
                elif defect == "format_changed":
                    ref["format"] = "plain"
                else:
                    assert defect == "run_changed"
                    ref["run_id"] += 1

            error = _typed_replay_error(env.project, bound)(
                Receipt.model_validate(body)
            )
            assert error is not None, defect
            assert error.code == "stale_replay", defect
            assert error.message, defect


def test_media_to_markdown_trafilatura_html_engine_converts_html_blob(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "to-markdown.frisket", name="To Markdown v1")
    try:
        seeded = _seed(project, tmp_path)
        action = _to_markdown_action(
            sheet_id=seeded["sheet_id"],
            row_ids=[seeded["row_ids"][0]],
            engine="trafilatura_html",
            output_name="page_markdown",
            idempotency_key="media_to_markdown@sha256:trafilatura-html",
        )
        typed_action_for_request(action)

        result = run_action_spec(project, action, project_id=PROJECT_ID)
        assert result.status == "completed", result.errors
        columns = _columns(project, seeded["sheet_id"])
        values = project.get_values(
            seeded["sheet_id"], int(columns["page_markdown"]["id"])
        )
        markdown = values[seeded["row_ids"][0]]
        assert "Quarterly Report" in markdown
        assert "Revenue doubled this year" in markdown
        assert "<html" not in markdown.lower()
    finally:
        project.close()


def test_media_to_markdown_params_reject_non_integer_sheet_id() -> None:
    from frisket.actions.types import ActionRequest

    with pytest.raises(ValueError):
        ActionRequest.model_validate(_to_markdown_action(sheet_id="1"))
