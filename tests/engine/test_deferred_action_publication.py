"""Only admitted coupled programs may defer publication across their finalizer."""

import json
from copy import deepcopy

import pytest

from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlan,
    _TypedMapRowsProgram,
)
from frisket.engine.jobs.runs import _deferred_publication
from frisket.engine.executor.queued_actions import queued_v1_payload_envelope
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from http_test_helpers import drain_queue
from test_typed_action_lifecycle import (
    REGISTRY,
    _request,
    _runner,
    run_typed_map_rows_action,
)
from test_typed_project_run_queue import _client, _seed_regex, _regex_request


@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("replacing", [False, True])
def test_direct_preparation_derives_defer_and_visibility_from_program(
    tmp_path, monkeypatch, admitted, replacing
):
    project = Project.create(tmp_path / "direct.frisket")
    try:
        sheet = project.add_sheet("Source")
        source = project.add_column(sheet, "source", type="text")
        columns = {"source": source}
        rows = project.add_rows(
            sheet,
            [{"source": "value"}],
            columns,
        )
        project.db.commit()
        if replacing:
            baseline_request = _request(sheet, rows, key="baseline")
            baseline = run_typed_map_rows_action(
                project,
                "test",
                REGISTRY.get(baseline_request.action_id),
                baseline_request,
                None,
                _runner,
            )
            assert baseline.status == "completed", baseline.errors
        request = _request(sheet, rows).model_copy(
            update={"replace_existing": replacing}
        )
        original_spec = TypedMapRowsPlan.spec_dict
        monkeypatch.setattr(
            TypedMapRowsPlan,
            "spec_dict",
            lambda self: {**original_spec(self), "deferred_publication": not admitted},
        )
        monkeypatch.setattr(
            _TypedMapRowsProgram, "defer_generation_seal", admitted, raising=False
        )
        observed = []

        async def stop_before_rows(self, spec, **kwargs):
            run_id = kwargs["prepared_run"].run_id
            assert kwargs["defer_generation_seal"] is admitted
            assert kwargs["defer_attempt_close"] is admitted
            assert _deferred_publication(project, run_id) is admitted
            assert (spec.get("deferred_publication") is True) is admitted
            column = project.db.execute(
                "SELECT id,hidden FROM columns WHERE sheet_id=? AND name='upper'",
                (sheet,),
            ).fetchone()
            assert bool(column["hidden"]) is (admitted and not replacing)
            if replacing:
                assert project.get_values(sheet, column["id"])[rows[0]] == "VALUE"
            observed.append(run_id)
            raise RuntimeError("stop before domain finalizer")

        monkeypatch.setattr(MapRunner, "run", stop_before_rows)
        result = run_typed_map_rows_action(
            project, "test", REGISTRY.get(request.action_id), request, None, _runner
        )
        assert result.status == "failed"
        assert len(observed) == 1, result.errors
    finally:
        project.close()


@pytest.mark.parametrize("admitted", [False, True])
def test_queue_persists_admission_and_worker_defers_from_durable_operation(
    tmp_path, monkeypatch, admitted
):
    monkeypatch.setattr(
        _TypedMapRowsProgram, "defer_generation_seal", admitted, raising=False
    )
    client = _client(tmp_path)
    project_id, sheet, _ = _seed_regex(client)
    result = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=_regex_request(sheet)
    ).json()
    assert result["status"] == "queued", result
    project = client.app.state.workspace.get(project_id)
    assert _deferred_publication(project, result["run_id"]) is admitted
    job = client.app.state.workspace.queue.get(result["job_id"])
    assert queued_v1_payload_envelope(job.payload) is not None
    forged = deepcopy(job.payload)
    forged["spec"]["deferred_publication"] = not admitted
    assert queued_v1_payload_envelope(forged) is None
    observed = []

    async def stop_before_rows(self, spec, **kwargs):
        assert kwargs["defer_generation_seal"] is admitted
        assert kwargs["defer_attempt_close"] is admitted
        assert _deferred_publication(self.project, result["run_id"]) is admitted
        observed.append(kwargs["resume_run_id"])
        raise RuntimeError("stop before domain finalizer")

    monkeypatch.setattr(MapRunner, "run", stop_before_rows)
    drain_queue(client)
    assert observed == [result["run_id"]], json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result["receipt_id"],)
        ).fetchone()[0]
    )["errors"]


def test_worker_defer_lookup_ignores_mutable_run_params(tmp_path):
    project = Project.create(tmp_path / "durable.frisket")
    try:
        sheet = project.add_sheet("Source")
        op = project.append_op("test", {"deferred_publication": True})
        project.db.execute(
            "INSERT INTO runs (op_id,sheet_id,action_kind,params) VALUES (?,?,'test',?)",
            (op, sheet, json.dumps({"deferred_publication": False})),
        )
        run_id = project.db.execute("SELECT id FROM runs").fetchone()[0]
        assert _deferred_publication(project, run_id)
        project.db.execute("UPDATE ops SET spec='{}' WHERE id=?", (op,))
        project.db.execute(
            "UPDATE runs SET params=? WHERE id=?",
            (json.dumps({"deferred_publication": True}), run_id),
        )
        assert not _deferred_publication(project, run_id)
    finally:
        project.close()
