"""Disk staging preserves complete group requests while bounding input payloads."""

import gc
import hashlib
import json
import tracemalloc

import pytest

from frisket.engine.executor.group_input_stage import GroupInputStage
from frisket.engine.executor.group_summary_plan import GroupSummaryPlan
from frisket.engine.executor.group_summary_program import GroupSummaryProgram
from frisket.engine.executor import group_summary_runtime as runtime
from frisket.engine.store import Project, cells
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.ops.group_summary import (
    group_summary_rows,
    group_summary_messages,
    estimate_group_summary,
)


def plan(sheet, *, rows=None):
    return GroupSummaryPlan(
        action_kind="reduce.group_summary",
        request={},
        sheet_id=sheet,
        input_columns=["story"],
        group_by="beat",
        model="anthropic/claude-haiku-4-5",
        instruction="Keep important outliers.",
        target_sheet_name="Summaries",
        group_column_name="group",
        row_count_column_name="rows",
        summary_column_name="summary",
        row_ids=rows,
    )


@pytest.mark.parametrize("group_type", [None, "number"])
def test_stage_preserves_typed_groups_original_order_prompt_bytes_and_hashes(
    group_type,
):
    rows = {
        9: {"story": 'first café\nquoted "source"', "beat": 1.0},
        3: {"story": "second", "beat": None},
        8: {"story": "third", "beat": 1},
        1: {"story": "fourth", "beat": "null"},
        7: {"story": "fifth", "beat": "1"},
    }
    expected = group_summary_rows(
        rows, source=["story"], group_by="beat", group_type=group_type
    )
    stage = GroupInputStage(
        list(rows), source=["story"], group_by="beat", group_type=group_type
    )
    directory = stage.path.parent
    try:
        # Source fingerprint coordinates are sorted, independently of execution order.
        for row in sorted(rows):
            stage.add(row, rows[row])
        stage.seal()
        actual = [{**group, "rows": list(group["rows"])} for group in stage]
        assert actual == expected
        messages = [
            group_summary_messages("Keep important outliers.", group)
            for group in expected
        ]
        assert [
            group_summary_messages("Keep important outliers.", group) for group in stage
        ] == messages
        estimate = estimate_group_summary("anthropic/claude-haiku-4-5", iter(messages))
        assert (
            estimate["resolved_prompt_hash"]
            == "sha256:"
            + hashlib.sha256(
                json.dumps(
                    messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
        )
        params = plan(1)
        program = GroupSummaryProgram(params, publication=None)
        for group, message in zip(stage, messages, strict=True):
            rendered = program.render(
                {"messages": group_summary_messages(params.instruction, group)}, {}
            )
            assert rendered.messages == message
            assert rendered.schema is None
    finally:
        stage.close()
    assert not directory.exists()


def test_preparation_pages_and_spools_a_large_corpus_without_retaining_payloads(
    make_project, monkeypatch
):
    project = make_project("large-group")
    sheet = project.add_sheet("Inputs")
    cols = {name: project.add_column(sheet, name) for name in ("story", "beat")}
    count, width = 10_000, 4096
    ids = project.add_rows(
        sheet,
        [
            {"story": str(i) + "x" * width, "beat": f"beat-{i % 100}"}
            for i in range(count)
        ],
        cols,
    )
    original_values, original_refs = cells.get_values, cells.get_values_with_refs
    value_pages, ref_pages = [], []

    def bounded_values(project, sheet, column, row_ids=None, **kwargs):
        assert row_ids is not None and len(row_ids) <= 256
        value_pages.append(len(row_ids))
        return original_values(project, sheet, column, row_ids, **kwargs)

    def bounded_refs(project, sheet, column, row_ids=None, **kwargs):
        assert row_ids is not None and len(row_ids) <= 256
        ref_pages.append(len(row_ids))
        return original_refs(project, sheet, column, row_ids, **kwargs)

    for store in (Project, ProjectReadSnapshot):
        monkeypatch.setattr(store, "get_values", bounded_values)
        monkeypatch.setattr(store, "get_values_with_refs", bounded_refs)
    gc.collect()
    measurements = {}
    for capture in (False, True):
        tracemalloc.start()
        resolved = runtime._resolve_reduce_group_summary_inputs(
            project, plan(sheet), capture_inputs=capture, cohort=ids
        )
        assert len(resolved["groups"]) == 100
        assert (
            resolved["consumed_capture"] is not None
            if capture
            else resolved["consumed_capture"] is None
        )
        assert isinstance(resolved["groups"], GroupInputStage)
        estimate = runtime._estimate_reduce_group_summary_cost(plan(sheet), resolved)
        assert estimate["groups"] == 100
        params = plan(sheet)
        program = GroupSummaryProgram(params, publication=None)
        lazy = (
            program.render(
                {"messages": group_summary_messages(params.instruction, group)}, {}
            )
            for group in resolved["groups"]
        )
        first = next(lazy)
        assert first.messages[1]["content"].count('"row_id"') == 100
        assert first.messages[1]["content"].count("x") == 100 * width
        assert resolved["groups"].staged == count
        lazy.close()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        measurements["captured" if capture else "advisory"] = peak
        # Input text alone exceeds39MiB. Membership IDs and a bounded page /
        # full individual prompt may live in Python, not the whole payload.
        assert peak < 16 * 1024 * 1024, measurements
        resolved["groups"].close()
    assert value_pages and ref_pages
    print(
        json.dumps(
            {
                "source_payload_bytes": count * width,
                "python_peak_bytes": measurements,
            }
        )
    )


def test_later_groups_use_the_staged_read_and_capture_survives_source_edit(
    tmp_path, make_project
):
    from executor_harness import run_action_with_confirmation
    from test_reduce_group_summary_executor import (
        _seed,
        _reduce_action,
        _summary_router,
    )
    from frisket.engine.store.staleness import sheet_freshness
    from frisket.engine.store.op_log import step_operation

    project = make_project("staged-actual")
    source = _seed(project, tmp_path)
    sheet = source["sheet_id"]
    row = source["source_row_ids"][-1]
    story = next(c["id"] for c in project.columns(sheet) if c["name"] == "story")
    before = project.get_values(sheet, story)[row]
    router, adapter = _summary_router()
    complete = adapter.complete
    edits = []

    async def edit_after_stage(request, client):
        assert not project.db.in_transaction
        if not adapter.requests:
            edits.append(
                project.apply_edits(
                    [
                        {
                            "row_id": row,
                            "column_id": story,
                            "value": "New text after staging",
                        }
                    ]
                )
            )
        return await complete(request, client)

    adapter.complete = edit_after_stage
    result = run_action_with_confirmation(
        project, _reduce_action(sheet), project_id="staged-group", router=router
    )
    assert result.status == "completed", result.errors
    assert len(adapter.requests) == 2
    second = adapter.requests[1].messages[-1]["content"]
    assert before in second and "New text after staging" not in second
    output = result.outputs[0].sheet_id
    assert sheet_freshness(project, output) == "stale"
    with project.transaction_scope():
        step_operation(project, "revert", change_id=edits[0])
    assert sheet_freshness(project, output) == "synced"


def test_advisory_and_refused_preparation_release_their_disposable_inputs(
    tmp_path, make_project, monkeypatch
):
    from frisket.engine.executor.group_summary_action import (
        prepare_group_summary_action,
        GroupSummaryRefused,
    )
    from test_reduce_group_summary_executor import _seed, _reduce_action, _bound_request

    directories = []
    initialize = GroupInputStage.__init__

    def observe(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        directories.append(self.path.parent)

    monkeypatch.setattr(GroupInputStage, "__init__", observe)
    project = make_project("advisory-lifetime")
    source = _seed(project, tmp_path)
    bound = _bound_request(_reduce_action(source["sheet_id"]))
    prepared = prepare_group_summary_action(project, bound)
    assert directories[-1].exists()
    assert prepared.resolved["consumed_capture"] is None
    assert (
        project.db.execute("SELECT COUNT(*) FROM consumed_source_bundles").fetchone()[0]
        == 0
    )
    del prepared
    gc.collect()
    assert all(not path.exists() for path in directories)
    project.add_sheet("Beat Summaries")
    with pytest.raises(GroupSummaryRefused) as caught:
        prepare_group_summary_action(project, bound)
    assert caught.value.error.code == "duplicate_sheet_name"
    # Even a retained error/traceback must not retain the staged corpus.
    assert all(not path.exists() for path in directories)
