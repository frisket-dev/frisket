"""Durable returned-file recovery and schema-bound occurrence contracts."""

import asyncio
from copy import deepcopy

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import ModelRouter
from frisket.engine.executor import actions
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.row_file_stage import verify_row_files
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from tests.engine.test_row_effect_checkpoint_boundary_fixes import _age_silent_attempt


class InterruptedAfterReturn(BaseException):
    pass


@pytest.fixture
def source(tmp_path, monkeypatch):
    calls = []

    def download(url, **kwargs):
        calls.append(url)
        return b"recoverable bytes", "text/plain", "evidence.txt", None

    monkeypatch.setattr("frisket.ops.enclosures.download_url", download)
    project = Project.create(tmp_path / "source.frisket")
    sheet = project.add_sheet("URLs")
    column = project.add_column(sheet, "url", "link")
    (row,) = project.add_rows(
        sheet, [{"url": "https://example.test/actual"}], {"url": column}
    )
    body = {
        "action_id": "media.fetch_url",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "url"},
        "output_names": {"media": 'Downloaded "evidence"'},
        "idempotency_key": "review-recovery",
    }
    router = ModelRouter()
    quote = actions.run_action_spec(project, body, project_id="review", router=router)
    assert quote.status == "needs_confirmation", quote.errors
    body["confirmation"] = quote.errors[0].details["promise_set_hash"]
    try:
        yield project, row, body, router, calls
    finally:
        project.close()


def _interrupt_returned(source, monkeypatch):
    project, row, body, router, calls = source

    def stop(*args, **kwargs):
        raise InterruptedAfterReturn()

    with monkeypatch.context() as patch:
        patch.setattr(RunResultStore, "consume_returned_row_effect_checkpoint", stop)
        with pytest.raises(InterruptedAfterReturn):
            actions.run_action_spec(project, body, project_id="review", router=router)
    checkpoint = project.db.execute(
        "SELECT * FROM effect_checkpoints WHERE family='row_effect'"
    ).fetchone()
    assert checkpoint["state"] == "returned"
    run_id = int(checkpoint["group_key"])
    returned = RunResultStore(project).row_effect_checkpoint(run_id, row)
    assert len(calls) == 1
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    return run_id, returned


def test_returned_bytes_survive_gc_bundle_restore_and_resume(
    source, monkeypatch, tmp_path
):
    project, row, body, router, calls = source
    run_id, checkpoint = _interrupt_returned(source, monkeypatch)
    descriptor = next(iter(checkpoint["response"].values()))["row_files"][0]
    digest = descriptor["primary"]["blob_hash"]
    assert digest in project._referenced_blob_hashes()
    project.gc_blobs()
    assert project.db.execute(
        "SELECT hash FROM blobs WHERE hash=?", (digest,)
    ).fetchone()
    archive = project.export(tmp_path / "returned.zip")
    restored = Project.import_bundle(archive, tmp_path / "restored.frisket")
    try:
        bound = typed_action_for_request(body)
        plan = build_typed_map_rows_plan(restored, bound, _allow_existing_outputs=True)
        verify_row_files(
            restored,
            checkpoint["response"],
            row_id=row,
            fields=plan.program._resolved_output_fields,
            output_names=plan.output_names,
        )
        with restored.materialize_blob(digest) as path:
            assert path.read_bytes() == b"recoverable bytes"
        attempt_a = checkpoint["authorized_attempt_id"]
        _age_silent_attempt(restored, attempt_a)
        claim = restored.db.execute(
            "SELECT claim_token FROM output_column_claims WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        runner = actions._default_map_runner_factory(restored, router)
        progress = asyncio.run(
            runner.run(
                plan.spec_dict(),
                program=plan.program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim,
            )
        )
        assert progress.halted_code is None
        assert progress.completed == 1
        assert len(calls) == 1
        assert RunResultStore(restored).row_effect_checkpoint(run_id, row) is None
        receipt = (
            ReceiptStore(restored)
            .find_by_idempotency_key(body["idempotency_key"])
            .parsed()
        )
        occurrences = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "row_file_output"
        ]
        assert len(occurrences) == 1
        assert occurrences[0]["output_key"] == "media"
        assert occurrences[0]["facts"]["url"] == "https://example.test/actual"
        assert (
            restored.db.execute(
                "SELECT COUNT(*) FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
    finally:
        restored.close()


@pytest.mark.parametrize("tamper", ["row", "key", "path", "missing", "duplicate"])
def test_returned_descriptor_requires_exact_declared_association(
    source, monkeypatch, tamper
):
    project, row, body, _router, calls = source
    _run_id, checkpoint = _interrupt_returned(source, monkeypatch)
    response = deepcopy(checkpoint["response"])
    payload = next(iter(response.values()))
    descriptor = payload["row_files"][0]
    if tamper == "row":
        descriptor["row_id"] += 1
    elif tamper == "key":
        descriptor["output_key"] = "other"
    elif tamper == "path":
        descriptor["item_path"] = ["ordinary"]
    elif tamper == "missing":
        payload["row_files"] = []
    else:
        payload["row_files"].append(deepcopy(descriptor))
    plan = build_typed_map_rows_plan(
        project, typed_action_for_request(body), _allow_existing_outputs=True
    )
    with pytest.raises(ValueError, match="returned file"):
        verify_row_files(
            project,
            response,
            row_id=row,
            fields=plan.program._resolved_output_fields,
            output_names=plan.output_names,
        )
    assert len(calls) == 1
