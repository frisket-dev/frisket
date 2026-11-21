"""Collection expansion owns the URL identity actually sent to enumeration."""

from frisket.engine.executor import collection_read
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.op_log import step_operation
from frisket.engine.store.staleness import sheet_freshness
from test_derive_collection_expand_executor import (
    _fake_channel_provider,
    _request_for,
    _seed_source_cell,
)
from test_table_capture_completeness import (
    _assert_captured,
    _assert_equal_contribution_roundtrip,
)


def test_collection_captures_selected_url_contribution(make_project, monkeypatch):
    project = make_project("collection")
    source = _seed_source_cell(project)
    monkeypatch.setattr(
        collection_read, "_ENUMERATOR_PROVIDER", _fake_channel_provider(1, 1)
    )
    result = run_action_spec(project, _request_for(source), project_id="capture")
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(
        project,
        output,
        source["sheet_id"],
        source["column_id"],
        source["row_id"],
    )


def test_collection_capture_precedes_enumerator_and_keeps_consumed_identity(
    make_project, monkeypatch
):
    project = make_project("collection")
    source = _seed_source_cell(project)
    provider = _fake_channel_provider(1, 1)
    edits = []

    def enumerate_after_edit(url, **kwargs):
        assert not project.db.in_transaction
        original = project.get_values(source["sheet_id"], source["column_id"])[
            source["row_id"]
        ]
        edits.append(
            project.apply_edits(
                [
                    {
                        "row_id": source["row_id"],
                        "column_id": source["column_id"],
                        "value": original,
                    }
                ]
            )
        )
        return provider(url, **kwargs)

    monkeypatch.setattr(collection_read, "_ENUMERATOR_PROVIDER", enumerate_after_edit)
    result = run_action_spec(project, _request_for(source), project_id="capture")
    assert result.status == "completed", result.errors
    output = next(item.sheet_id for item in result.outputs if item.kind == "sheet")
    assert len(edits) == 1
    # A later equal URL is a distinct contribution; publication must retain
    # the original input identity, rather than rereading it after enumeration.
    assert sheet_freshness(project, output) == "stale"
    with project.transaction_scope():
        step_operation(project, "revert", change_id=edits[0])
    assert sheet_freshness(project, output) == "synced"
