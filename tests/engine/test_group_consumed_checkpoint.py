"""Returned group results retain the source capture from the paid request."""

import json

from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.staleness import sheet_freshness
from frisket.execution.attempt import abandon_stale_dispatching_attempts
from frisket.team.security.secrets import encrypt_secret, key_hint
from test_reduce_effect_checkpoint import (
    project,  # noqa: F401 - shared real project fixture
    _action,
    _CountingSummaryAdapter,
    _project_key_router,
    _run,
)
from frisket.engine.store.transactions import write_transaction


def test_group_replay_keeps_original_consumed_capture_after_equal_text_edit(
    project,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    provider = _CountingSummaryAdapter()
    with write_transaction(project.db):
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-ant-test"),
            hint=key_hint("sk-ant-test"),
            spend_cap_micro=50_000,
        )
    router = _project_key_router(provider)
    action = _action(project._sheet_id, key="group-capture-recovery")
    write = RunResultStore.publish_result_batch
    crashed_once = False

    calls = 0

    def crash_before_publication(*args, **kwargs):
        nonlocal calls
        calls += 1
        nonlocal crashed_once
        if not crashed_once and calls == 2:
            crashed_once = True
            raise RuntimeError("injected result publication crash")
        return write(*args, **kwargs)

    monkeypatch.setattr(
        RunResultStore, "publish_result_batch", crash_before_publication
    )
    crashed = _run(project, action, router)
    assert crashed.status == "failed"
    assert len(provider.requests) == 2, crashed.errors
    checkpoint = project.db.execute(
        "SELECT payload FROM effect_checkpoints WHERE state='returned' LIMIT 1"
    ).fetchone()
    saved_ref = next(iter(json.loads(checkpoint[0]).values()))["__consumed_sources"]
    fingerprint = saved_ref["shared_inputs"][0]
    saved = json.loads(
        project.db.execute(
            "SELECT capture_json FROM consumed_source_bundles WHERE input_fingerprint=?",
            (fingerprint,),
        ).fetchone()[0]
    )
    column = next(
        col for col in project.columns(project._sheet_id) if col["name"] == "story"
    )
    row = project.visible_row_ids(project._sheet_id)[0]
    original = project.get_values(project._sheet_id, column["id"])[row]
    project.apply_edits([{"row_id": row, "column_id": column["id"], "value": original}])
    run_id = int(project.db.execute("SELECT max(id) FROM runs").fetchone()[0])
    project.db.execute(
        "UPDATE execution_attempts SET created_at=datetime('now','-7 hours') WHERE run_id=?",
        (run_id,),
    )
    project.db.commit()
    with write_transaction(project.db):
        assert (
            abandon_stale_dispatching_attempts(project, run_id) == 0
        )  # Interrupted owner is already halted.
    resumed = _run(project, action, router)
    assert resumed.status == "completed", resumed.errors
    assert len(provider.requests) == 2
    bundle = project.db.execute(
        "SELECT output_sheet_id,capture_json FROM consumed_source_bundles WHERE run_id=? AND kind='aggregate'",
        (run_id,),
    ).fetchone()
    assert json.loads(bundle["capture_json"])["scopes"] == saved["scopes"]
    assert sheet_freshness(project, bundle["output_sheet_id"]) == "stale"
    project.undo()
    assert sheet_freshness(project, bundle["output_sheet_id"]) == "synced"
