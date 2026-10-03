"""Current-name consumers must never attach to an undone column's identity."""

import pytest

from frisket.engine.executor.page_capture import (
    CapturePlan,
    _resolve_web_capture_page_inputs,
    _web_capture_page_add_output_column,
)
from frisket.contracts.action import ActionError
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.output_families import OutputFamilyStore
from frisket.engine.store.op_log import append_op
from frisket.engine.store.runs import RunResultStore


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "active-columns.frisket")
    try:
        yield project
    finally:
        project.close()


def _undone_column(project, sheet, name, *, type="text"):
    old = project.add_column(sheet, name, type=type, ai_generated=True)
    op = append_op(project, "add_column", undo_info={"created_columns": [old]})
    assert project.undo() == op
    return old


def test_output_claims_bind_only_current_identity(project):
    sheet = project.add_sheet("Data")
    old = _undone_column(project, sheet, "result")
    store = OutputColumnClaimStore(project)
    claims, conflict = store.acquire(
        sheet_id=sheet,
        output_names=["result"],
        action_kind="map.regex_extract",
        claim_token="fresh-output",
    )
    assert conflict is None
    assert claims[0]["column_id"] is None
    assert store.active_for_columns([old]) is None

    current = project.add_column(sheet, "result", ai_generated=True, hidden=True)
    assert current != old
    assert store.active_for_columns([current])["id"] == claims[0]["id"]
    op = append_op(project, "map", undo_info={"created_columns": [current]})
    run = RunResultStore(project).start_run(op, sheet, "map.regex_extract")
    assert store.bind_to_run(claim_token="fresh-output", run_id=run) == 1
    claim = store.active_for_output_name(sheet_id=sheet, output_name="result")
    assert claim["column_id"] == current
    assert store.active_for_columns([old]) is None


def test_output_family_ignores_inactive_history_even_with_different_type(project):
    sheet = project.add_sheet("Data")
    old = _undone_column(project, sheet, "result")
    columns, created = OutputFamilyStore(project).create_or_reuse(
        sheet_id=sheet,
        fields=[{"name": "result", "column_type": "json"}],
    )
    assert columns["result"] != old
    assert created == [columns["result"]]
    assert project.get_column(old)["type"] == "text"
    assert project.get_column(old)["active"] == 0


@pytest.mark.parametrize("undone", [False, True])
def test_capture_distinguishes_live_hidden_from_undone_name(project, undone):
    sheet = project.add_sheet("Data")
    source = project.add_column(sheet, "url", type="link")
    rows = project.add_rows(sheet, [{"url": "https://example.test"}], {"url": source})
    if undone:
        old = _undone_column(project, sheet, "page", type="file")
    else:
        old = project.add_column(sheet, "page", type="file", hidden=True)
    plan = CapturePlan(
        sheet_id=sheet,
        input_column="url",
        row_ids=rows,
        output_name="page",
        output_mode="page",
        render_mode="static",
        include_warc=False,
        max_bytes=1000,
        timeout_ms=1000,
        full_page=True,
        output_column_type="file",
        primary_role="html",
        links_sheet_name="",
        output_names={"page": "page"},
        request={},
    )
    resolved = _resolve_web_capture_page_inputs(project, "web.capture_page", plan)
    if undone:
        assert not isinstance(resolved, ActionError)
        current = _web_capture_page_add_output_column(project, plan)
        assert current != old
        assert project.get_column(old)["active"] == 0
    else:
        assert isinstance(resolved, ActionError)
        assert resolved.code == "output_column_exists"
        with pytest.raises(ValueError, match="reserved by a hidden column"):
            _web_capture_page_add_output_column(project, plan)
        assert project.get_column(old)["hidden"] == 1
