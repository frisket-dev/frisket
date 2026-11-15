"""A crashed Stop must repair its cancelled owner, never erase or restart it."""

import os
import signal
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.team.security.secrets import encrypt_secret, key_hint
from tests.engine.test_reduce_effect_checkpoint import (
    _CountingSummaryAdapter,
    _action,
    _project_key_router,
    _run,
    project as _project_fixture,
)


project = _project_fixture


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process kill")
def test_stopped_group_crash_repairs_cancelled_receipt_without_new_purchase(
    project, tmp_path
):
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-test"),
        hint=key_hint("sk-test"),
        spend_cap_micro=100_000,
    )
    original = _run(
        project,
        _action(project._sheet_id, key="create"),
        _project_key_router(_CountingSummaryAdapter()),
    )
    sheet = original.outputs[0].sheet_id
    path = project.path
    project.close()
    child = tmp_path / "stop-and-kill.py"
    child.write_text("""
import os,signal,sys
from pathlib import Path
sys.path[:0]=[sys.argv[3],str(Path(sys.argv[3]).parent)]
from test_reduce_effect_checkpoint import _CountingSummaryAdapter,_project_key_router,_run
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.executor.group_summary_publication import GroupPublication
p=Project(Path(sys.argv[1]))
publish=GroupPublication.publish
count=0
def stop(self,value):
 global count
 count+=1
 if count == 2:
  assert RunResultStore(p).request_cancel(self.envelope['run_id'])
  os.kill(os.getpid(),signal.SIGKILL)
 return publish(self,value)
GroupPublication.publish=stop
_run(p,{'action_id':'sheet.refresh','scope':{'kind':'project'},'params':{'sheet_id':int(sys.argv[2])},'idempotency_key':'stopped-refresh'},_project_key_router(_CountingSummaryAdapter()))
raise RuntimeError('death hook not reached')
""")
    killed = subprocess.run(
        [
            sys.executable,  # subprocess-boundary: SIGKILL after Stop must preserve committed accounting
            str(child),
            str(path),
            str(sheet),
            str(Path(__file__).resolve().parent),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    with closing(Project(path)) as reopened:
        prior = reopened.db.execute(
            "SELECT * FROM receipts WHERE idempotency_key='stopped-refresh'"
        ).fetchone()
        run_id, receipt_id = prior["run_id"], prior["id"]
        calls = [
            tuple(row)
            for row in reopened.db.execute(
                "SELECT * FROM model_calls WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]
        assert len(calls) == 2
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        reopened.db.execute(
            "UPDATE execution_attempts SET created_at='2000-01-01' WHERE run_id=?",
            (run_id,),
        )
        reopened.db.execute(
            "UPDATE output_column_claims SET lease_expires_at='2000-01-01' WHERE run_id=?",
            (run_id,),
        )
        reopened.db.execute(
            "UPDATE sheets SET refresh_lease_expires_at='2000-01-01' WHERE id=?",
            (sheet,),
        )
        reopened.db.execute(
            "UPDATE receipts SET created_at='2000-01-01' WHERE id=?", (receipt_id,)
        )
        reopened.db.commit()
        provider = _CountingSummaryAdapter()
        recovered = _run(
            reopened,
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet},
                "idempotency_key": "stopped-refresh",
            },
            _project_key_router(provider),
        )
        assert recovered.status == "cancelled", recovered.model_dump()
        assert recovered.run_id == run_id
        assert recovered.receipt_id == receipt_id
        assert provider.requests == []
        assert [
            tuple(row)
            for row in reopened.db.execute(
                "SELECT * FROM model_calls WHERE run_id=? ORDER BY id", (run_id,)
            )
        ] == calls
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened.db.execute(
                "SELECT refresh_claim_token FROM sheets WHERE id=?", (sheet,)
            ).fetchone()[0]
            is None
        )


def test_stale_cleanup_keeps_stopped_owner_with_ambiguous_effect(tmp_path):
    from frisket.engine.executor.action_reservations import (
        _running_receipt_stale_result,
    )
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.contracts.action import ActionSpec
    from tests.engine.test_project_run_terminalization_recovery import (
        _seed_orphaned_run,
        ACTION_KIND,
        PROJECT_ID,
    )

    with closing(Project.create(tmp_path / "ambiguous-stop.frisket")) as project:
        owner = _seed_orphaned_run(project, reserved_checkpoint=True)
        receipts = ReceiptStore(project)
        prior = receipts.parsed_by_id(owner.receipt_id)
        running = prior.model_copy(update={"status": "running"})
        assert receipts.update_body_status(running, require_status="queued")
        project.db.execute(
            "UPDATE receipts SET created_at='2000-01-01' WHERE id=?",
            (owner.receipt_id,),
        )
        project.db.commit()
        checkpoint = tuple(
            project.db.execute(
                "SELECT * FROM effect_checkpoints WHERE id=?", (owner.checkpoint_id,)
            ).fetchone()
        )
        result = _running_receipt_stale_result(
            project,
            receipts.find_by_id(owner.receipt_id),
            params_hash=running.params_hash,
            project_id=PROJECT_ID,
            action=ActionSpec(
                kind=ACTION_KIND, params={}, idempotency_key=running.idempotency_key
            ),
        )
        assert result is None
        assert receipts.parsed_by_id(owner.receipt_id).status == "running"
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (owner.run_id,)
            ).fetchone()[0]
            == "running"
        )
        assert (
            tuple(
                project.db.execute(
                    "SELECT * FROM effect_checkpoints WHERE id=?",
                    (owner.checkpoint_id,),
                ).fetchone()
            )
            == checkpoint
        )


def test_stop_before_group_metadata_batch_closes_cancelled_owner(project, monkeypatch):
    from frisket.engine.store.derived_refresh_stage import DerivedRefreshStage
    from frisket.engine.store.runs import RunResultStore

    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-test"),
        hint=key_hint("sk-test"),
        spend_cap_micro=100_000,
    )
    batches = DerivedRefreshStage.batches
    stopped = []

    def stop_before_batch(stage, *args, **kwargs):
        for batch in batches(stage, *args, **kwargs):
            if not stopped:
                run = project.db.execute(
                    "SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                assert run is not None
                assert RunResultStore(project).request_cancel(run["id"])
                stopped.append(run["id"])
            yield batch

    monkeypatch.setattr(DerivedRefreshStage, "batches", stop_before_batch)
    provider = _CountingSummaryAdapter()
    result = _run(
        project,
        _action(project._sheet_id, key="stop-metadata"),
        _project_key_router(provider),
    )
    assert result.status == "cancelled", result.model_dump()
    assert result.run_id == stopped[0]
    assert provider.requests == []
    assert RunResultStore(project).get_run(result.run_id)["status"] == "cancelled"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE run_id=? AND status='active'",
            (result.run_id,),
        ).fetchone()[0]
        == 0
    )
