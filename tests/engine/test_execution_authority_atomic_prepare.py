from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.core import MapRows, MapBatch, routed_capability
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    typed_request_hash,
)
from frisket.engine.executor.action_reservations import (
    QUEUED_ACTION_RUN_MARKER_PARAM,
    QUEUED_ACTION_RUN_MARKER_SCHEMA,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.provider import ExecutionCompositionContext
from frisket.ops.builtin import get_recipe
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


ROUTED_ACTIONS = (
    "media.transcribe",
    "media.ocr",
    "media.to_markdown",
    "enrich.geocode",
    "enrich.census_demographics",
)


def _edition_request(snapshot: dict[str, Any]) -> SimpleNamespace:
    direct = ExecutionCompositionContext.direct()
    context = ExecutionCompositionContext(
        storage_key=None,
        run_id=None,
        trusted_job_org_id=direct.trusted_job_org_id,
        edition_snapshot=snapshot,
    )
    return SimpleNamespace(state=SimpleNamespace(execution_composition_context=context))


ACTION_RECIPE = {
    "media.transcribe": "media.transcribe",
    "media.ocr": "media.ocr",
    "media.to_markdown": "media.to_markdown",
    "enrich.geocode": "enrich.geocode",
    "enrich.census_demographics": "enrich.census_demographics",
}


def _seed_action(
    workspace: Workspace,
    action_kind: str,
    *,
    idempotency_key: str,
    output_name: str | None = None,
) -> dict[str, Any]:
    project = workspace.get("authority")
    sheet_id = project.add_sheet(action_kind)

    if action_kind == "media.transcribe":
        column_id = project.add_column(sheet_id, "media", type="audio")
        blob_id = project.add_blob(
            b"RIFF0000WAVEfmt ",
            filename="sample.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "media": media_cell(
                        blob_id,
                        mime="audio/wav",
                        filename="sample.wav",
                    )
                }
            ],
            {"media": column_id},
        )
        params = {
            "source": "media",
            "engine": "faster_whisper",
        }
        outputs = {"text": output_name or "transcript"}
    elif action_kind == "media.ocr":
        column_id = project.add_column(sheet_id, "media", type="image")
        blob_id = project.add_blob(
            b"\x89PNG\r\n\x1a\n",
            filename="scan.png",
            mime="image/png",
            metadata=owned_media_metadata_document(
                probe={"width": 1, "height": 1, "kind": "image"}
            ),
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "media": media_cell(
                        blob_id,
                        mime="image/png",
                        filename="scan.png",
                    )
                }
            ],
            {"media": column_id},
        )
        params = {
            "source": "media",
            "engine": "rapidocr",
            "language": "en",
            "dpi": 180,
        }
        outputs = {"text": output_name or "ocr_text"}
    elif action_kind == "media.to_markdown":
        column_id = project.add_column(sheet_id, "doc", type="file")
        blob_id = project.add_blob(
            b"<h1>Packet 10</h1>",
            filename="document.html",
            mime="text/html",
            metadata=owned_media_metadata_document(probe={"kind": "document"}),
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "doc": media_cell(
                        blob_id,
                        mime="text/html",
                        filename="document.html",
                    )
                }
            ],
            {"doc": column_id},
        )
        return {
            "action_id": action_kind,
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"source": "doc", "engine": "markitdown"},
            "output_names": {"markdown": output_name or "markdown"},
            "idempotency_key": idempotency_key,
        }
    elif action_kind == "enrich.geocode":
        column_id = project.add_column(sheet_id, "address", type="text")
        project.add_rows(
            sheet_id,
            [{"address": "1600 Pennsylvania Avenue NW, Washington, DC"}],
            {"address": column_id},
        )
        return {
            "action_id": action_kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": "address",
                "engine": "opencage",
                "include_lat_lon": False,
            },
            "output_names": {"geo_point": output_name or "location"},
            "idempotency_key": idempotency_key,
        }
    elif action_kind == "enrich.census_demographics":
        column_id = project.add_column(sheet_id, "point", type="geo_point")
        project.add_rows(
            sheet_id,
            [{"point": {"lat": 38.9, "lon": -77.03}}],
            {"point": column_id},
        )
        return {
            "action_id": action_kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": "point",
                "geography": "tract",
                "include_moe": False,
            },
            "idempotency_key": idempotency_key,
        }
    else:  # pragma: no cover - the parameter roster above is closed
        raise AssertionError(f"unsupported routed test kind {action_kind!r}")

    return {
        "action_id": action_kind,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": params,
        "output_names": outputs,
        "idempotency_key": idempotency_key,
    }


def _workspace(tmp_path: Path, *, require_confirmation: bool = False) -> Workspace:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Execution authority", project_id="authority")
    if require_confirmation:
        # These replay cases exercise the exact-confirmation path, not preapproval.
        workspace.executor_deps_factory = lambda project_id, _request: ExecutorDeps(
            consent_coverage=ConsentCoverage(
                instance_principal(workspace.get(project_id)), Decimal("0")
            )
        )
    return workspace


def _echo_confirmation(
    action: dict[str, Any],
    response: Any,
) -> dict[str, Any]:
    assert response.payload["status"] == "needs_confirmation"
    errors = response.payload.get("errors") or []
    assert errors
    promise_set_hash = errors[0].get("details", {}).get("promise_set_hash")
    assert isinstance(promise_set_hash, str) and promise_set_hash
    confirmed = copy.deepcopy(action)
    if "action_id" in action:
        confirmed["confirmation"] = promise_set_hash
    else:
        confirmed["params"]["consented_promise_set_hash"] = promise_set_hash
    return confirmed


def _prepared_tuple(project: Any, *, idempotency_key: str) -> dict[str, Any]:
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    assert receipt_row is not None
    receipt = json.loads(receipt_row["body"])
    run_id = receipt_row["run_id"]
    assert type(run_id) is int

    prepared_refs = [
        item["ref"]
        for item in receipt["evidence"]
        if item["ref"].get("kind") == "queued_action_run_prepared"
    ]
    assert len(prepared_refs) == 1
    attempt_id = prepared_refs[0].get("attempt_id")
    assert isinstance(attempt_id, str) and attempt_id

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    route = project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    ).fetchall()
    attempt = project.db.execute(
        "SELECT * FROM execution_attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    claims = project.db.execute(
        "SELECT c.*, col.name AS column_name, col.type AS column_type, "
        "col.semantic_type AS column_semantic_type, col.format AS column_format "
        "FROM output_column_claims c "
        "JOIN columns col ON col.id=c.column_id "
        "WHERE c.run_id=? AND c.status='active' ORDER BY col.position",
        (run_id,),
    ).fetchall()
    return {
        "receipt_row": receipt_row,
        "receipt": receipt,
        "run_id": run_id,
        "run": run,
        "route": route,
        "attempt": attempt,
        "attempt_id": attempt_id,
        "claims": claims,
    }


@pytest.mark.parametrize("action_kind", ROUTED_ACTIONS)
def test_all_five_routed_preparations_publish_one_exact_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    monkeypatch.setenv("CENSUS_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path)
    key = f"packet-10:{action_kind}"
    action = _seed_action(workspace, action_kind, idempotency_key=key)

    recipe = (
        _typed_map_rows_plan(typed_action_for_request(action)).program
        if "action_id" in action
        else get_recipe(ACTION_RECIPE[action_kind])
    )
    assert recipe.consumes_resolution
    original_output_fields = type(recipe).output_fields
    minted: list[list[dict[str, Any]]] = []

    def count_output_fields(self: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
        fields = [dict(field) for field in original_output_fields(self, spec)]
        minted.append(fields)
        return fields

    monkeypatch.setattr(type(recipe), "output_fields", count_output_fields)
    service = ActionRunService(workspace)
    response = service.run_action(
        "authority",
        action,
        request_context=_edition_request({"edition": "packet-10"}),
    )
    if response.payload["status"] == "needs_confirmation":
        action = _echo_confirmation(action, response)
        minted.clear()
        response = service.run_action(
            "authority",
            action,
            request_context=_edition_request({"edition": "packet-10"}),
        )

    assert response.status_code == 200
    assert response.payload["status"] == "queued"
    assert len(minted) == 1, "the dynamic output plan must be minted once"

    project = workspace.get("authority")
    prepared = _prepared_tuple(project, idempotency_key=key)
    assert response.payload["run_id"] == prepared["run_id"]
    assert response.payload["receipt_id"] == prepared["receipt_row"]["id"]
    assert prepared["run"] is not None
    assert json.loads(prepared["run"]["edition_run_context"]) == {
        "edition": "packet-10"
    }
    assert len(prepared["route"]) == 1
    assert prepared["attempt"] is not None
    assert prepared["attempt"]["run_id"] == prepared["run_id"]
    assert prepared["attempt"]["seq"] == 0
    assert prepared["attempt"]["state"] == "admitted"
    assert prepared["receipt"]["run_id"] == prepared["run_id"]
    assert prepared["claims"]
    assert {
        str(row["column_name"]): (
            str(row["column_type"]),
            row["column_semantic_type"],
            row["column_format"],
        )
        for row in prepared["claims"]
    } == {
        str(field["name"]): (
            str(field["column_type"]),
            field.get("semantic_type"),
            field.get("format"),
        )
        for field in minted[0]
    }
    assert {row["claim_token"] for row in prepared["claims"]} == {
        f"output-claim:{prepared['receipt_row']['id']}"
    }
    assert all(row["run_id"] == prepared["run_id"] for row in prepared["claims"])
    assert all(row["column_id"] is not None for row in prepared["claims"])

    jobs = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    assert len(jobs) == 1
    assert jobs[0].run_id == prepared["run_id"]
    assert jobs[0].receipt_id == prepared["receipt_row"]["id"]
    assert jobs[0].payload["dedupe_key"] == f"run:{prepared['run_id']}"

    routed_from_registry = {
        registered.action_id
        for registered in ACTION_REGISTRY.actions
        if isinstance(registered.definition.run, (MapRows, MapBatch))
        and routed_capability(registered.definition.run) is not None
    }
    assert routed_from_registry == set(ROUTED_ACTIONS)


def test_extract_metadata_atomic_output_family_semantics_remain_non_routed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed admission atomically prepares the family without routing a provider."""

    from frisket.actions.media_metadata import MetadataOutput
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    workspace = _workspace(tmp_path)
    project = workspace.get("authority")
    sheet_id = project.add_sheet("extract metadata")
    input_column_id = project.add_column(sheet_id, "media", type="image")
    blob_id = project.add_blob(
        b"\x89PNG\r\n\x1a\n",
        filename="metadata.png",
        mime="image/png",
        metadata=owned_media_metadata_document(
            probe={"width": 1, "height": 1, "kind": "image"}
        ),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    blob_id,
                    mime="image/png",
                    filename="metadata.png",
                )
            }
        ],
        {"media": input_column_id},
    )
    action = {
        "action_id": "media.extract_metadata",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {
            "source": "media",
            "output_mode": "columns",
            "refresh": False,
        },
        "output_names": {key: f"meta_{key}" for key in MetadataOutput.model_fields},
        "idempotency_key": "packet-10:extract-metadata-family",
    }
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    assert plan.program.consumes_resolution is False
    fields = plan.output_fields
    baseline = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "columns",
            "ops",
            "runs",
            "run_scopes",
            "receipts",
            "output_column_claims",
            "routes",
            "execution_attempts",
        )
    }
    original_start_run = RunResultStore.start_run

    def fail_after_family_and_op(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("injected extract-metadata start_run failure")

    monkeypatch.setattr(RunResultStore, "start_run", fail_after_family_and_op)
    service = ActionRunService(workspace)
    failed = service.run_action("authority", action)
    assert failed.payload["status"] == "failed"
    assert [error["code"] for error in failed.payload["errors"]] == ["map_rows_failed"]
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in baseline
    } == baseline
    assert (
        workspace.queue.list_project_jobs(
            "authority",
            kind="project.run",
            limit=20,
        )
        == []
    )

    monkeypatch.setattr(RunResultStore, "start_run", original_start_run)
    queued = service.run_action("authority", action)
    assert queued.status_code == 200
    assert queued.payload["status"] == "queued"
    run_id = int(queued.payload["run_id"])
    receipt_id = str(queued.payload["receipt_id"])
    output_rows = project.db.execute(
        "SELECT c.id,c.name,c.type,c.format,c.ai_generated,c.current_run_id,"
        "oc.claim_token,oc.run_id,oc.status "
        "FROM columns c JOIN output_column_claims oc ON oc.column_id=c.id "
        "WHERE c.sheet_id=? AND oc.receipt_id=? ORDER BY c.position",
        (sheet_id, receipt_id),
    ).fetchall()
    assert [(row["name"], row["type"], row["format"]) for row in output_rows] == [
        (field["name"], field["column_type"], field.get("format")) for field in fields
    ]
    assert all(bool(row["ai_generated"]) for row in output_rows)
    assert all(int(row["current_run_id"]) == run_id for row in output_rows)
    assert {row["claim_token"] for row in output_rows} == {f"output-claim:{receipt_id}"}
    assert {int(row["run_id"]) for row in output_rows} == {run_id}
    assert {row["status"] for row in output_rows} == {"active"}
    run = project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run is not None
    undo = json.loads(
        project.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (run["op_id"],)
        ).fetchone()[0]
    )
    assert set(undo["created_columns"]) == {int(row["id"]) for row in output_rows}
    assert project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0
    )
    jobs = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    assert len(jobs) == 1
    assert jobs[0].run_id == run_id


def test_concurrent_project_run_projection_uses_enforced_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path)
    action = _seed_action(
        workspace,
        "enrich.geocode",
        idempotency_key="packet-10:concurrent-idempotency",
    )
    service = ActionRunService(workspace)
    challenge = service.run_action("authority", action)
    if challenge.payload["status"] == "needs_confirmation":
        action = _echo_confirmation(action, challenge)
    barrier = Barrier(2)

    def invoke() -> dict[str, Any]:
        barrier.wait()
        return service.run_action(
            "authority",
            action,
            request_context=_edition_request({"edition": "packet-10"}),
        ).payload

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: invoke(), range(2)))

    assert {result["status"] for result in results} == {"queued"}
    assert len({result["run_id"] for result in results}) == 1
    assert len({result["job_id"] for result in results}) == 1
    project = workspace.get("authority")
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
            (action["idempotency_key"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        len(
            workspace.queue.list_project_jobs(
                "authority",
                kind="project.run",
                limit=20,
            )
        )
        == 1
    )


def test_competing_output_preparation_has_one_coherent_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path)
    first = _seed_action(
        workspace,
        "enrich.geocode",
        idempotency_key="packet-10:competing-output:first",
        output_name="shared_location",
    )
    seeded_op_count = (
        workspace.get("authority").db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    )
    service = ActionRunService(workspace)
    challenge = service.run_action("authority", first)
    if challenge.payload["status"] == "needs_confirmation":
        first = _echo_confirmation(first, challenge)
    second = copy.deepcopy(first)
    second["idempotency_key"] = "packet-10:competing-output:second"
    barrier = Barrier(2)

    def invoke(action: dict[str, Any]) -> dict[str, Any]:
        barrier.wait()
        return service.run_action("authority", action).payload

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (first, second)))

    winner = [result for result in results if result["status"] == "queued"]
    loser = [result for result in results if result["status"] == "failed"]
    assert len(winner) == 1
    assert len(loser) == 1
    # Typed actions require new output names, so the second preparation refuses
    # the already-created family before it can acquire any competing claims.
    assert [error["code"] for error in loser[0]["errors"]] == ["output_column_exists"]

    project = workspace.get("authority")
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        == seeded_op_count + 1
    )
    assert project.db.execute("SELECT COUNT(*) FROM run_scopes").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 1
    )
    active_claims = project.db.execute(
        "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
    ).fetchone()[0]
    assert active_claims == len(
        _typed_map_rows_plan(typed_action_for_request(first)).output_fields
    )
    retained_keys = {
        str(row["idempotency_key"])
        for row in project.db.execute("SELECT idempotency_key FROM receipts").fetchall()
    }
    assert retained_keys <= {
        first["idempotency_key"],
        second["idempotency_key"],
    }
    assert len(retained_keys) == 1
    assert (
        len(
            workspace.queue.list_project_jobs(
                "authority",
                kind="project.run",
                limit=20,
            )
        )
        == 1
    )


def test_stale_prepared_attempt_resume_reuses_tuple_and_replaces_only_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path, require_confirmation=True)
    key = "packet-10:stale-prepared-attempt-resume"
    action = _seed_action(
        workspace,
        "enrich.geocode",
        idempotency_key=key,
    )
    service = ActionRunService(workspace)
    challenge = service.run_action("authority", action)
    action = _echo_confirmation(action, challenge)
    first = service.run_action("authority", action)
    assert first.payload["status"] == "queued"

    project = workspace.get("authority")
    before = _prepared_tuple(project, idempotency_key=key)
    before_job = workspace.queue.get(first.payload["job_id"])
    assert before_job is not None
    before_claims = {
        (
            int(row["column_id"]),
            str(row["column_name"]),
            str(row["claim_token"]),
        )
        for row in before["claims"]
    }
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching', "
        "created_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (before["attempt_id"],),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (before["attempt_id"], before["run_id"]),
    )
    project.db.execute(
        "UPDATE output_column_claims SET renewed_at=?, lease_expires_at=? "
        "WHERE claim_token=?",
        (
            "2000-01-01T00:00:00+00:00",
            "2000-01-01T00:00:01+00:00",
            f"output-claim:{before['receipt_row']['id']}",
        ),
    )
    # Model the only state reservation recovery owns: preparation committed,
    # but enqueue publication did not.  An already-published job is observable
    # and an idempotent request must replay it without replacing its authority.
    receipt_body = json.loads(before["receipt_row"]["body"])
    receipt_body["evidence"] = [
        evidence
        for evidence in receipt_body["evidence"]
        if evidence["ref"].get("job_id") is None
    ]
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (
            json.dumps(receipt_body, sort_keys=True),
            before["receipt_row"]["id"],
        ),
    )
    project.db.execute(
        "UPDATE output_column_claims SET job_id=NULL WHERE claim_token=?",
        (f"output-claim:{before['receipt_row']['id']}",),
    )
    project.db.commit()

    resumed = service.run_action("authority", action)
    assert resumed.payload["status"] == "queued"
    assert resumed.payload["receipt_id"] == before["receipt_row"]["id"]
    assert resumed.payload["run_id"] == before["run_id"]
    assert resumed.payload["job_id"] == before_job.id

    after = _prepared_tuple(project, idempotency_key=key)
    assert after["receipt_row"]["id"] == before["receipt_row"]["id"]
    assert after["run_id"] == before["run_id"]
    assert [row["id"] for row in after["route"]] == [
        row["id"] for row in before["route"]
    ]
    assert {
        (
            int(row["column_id"]),
            str(row["column_name"]),
            str(row["claim_token"]),
        )
        for row in after["claims"]
    } == before_claims
    assert after["attempt_id"] != before["attempt_id"]
    old_attempt = project.db.execute(
        "SELECT * FROM execution_attempts WHERE id=?",
        (before["attempt_id"],),
    ).fetchone()
    assert old_attempt["seq"] == 0
    assert old_attempt["state"] == "abandoned"
    assert after["attempt"]["seq"] == 1
    assert after["attempt"]["state"] == "admitted"
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 2
    )
    jobs = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    assert [job.id for job in jobs] == [before_job.id]


def test_unacknowledged_job_replay_accepts_the_live_dispatching_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A death after enqueue may race the worker's admitted→dispatching CAS."""

    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path, require_confirmation=True)
    key = "packet-10:dispatching-ack-replay"
    action = _seed_action(
        workspace,
        "enrich.geocode",
        idempotency_key=key,
    )
    service = ActionRunService(workspace)
    action = _echo_confirmation(action, service.run_action("authority", action))
    first = service.run_action("authority", action)
    assert first.payload["status"] == "queued"

    project = workspace.get("authority")
    prepared = _prepared_tuple(project, idempotency_key=key)
    job = workspace.queue.get(first.payload["job_id"])
    assert job is not None
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (prepared["attempt_id"],),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (prepared["attempt_id"], prepared["run_id"]),
    )
    body = json.loads(prepared["receipt_row"]["body"])
    body["evidence"] = [
        evidence
        for evidence in body["evidence"]
        if evidence["ref"].get("job_id") != job.id
    ]
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (
            json.dumps(body, sort_keys=True),
            prepared["receipt_row"]["id"],
        ),
    )
    project.db.execute(
        "UPDATE output_column_claims SET job_id=NULL WHERE claim_token=?",
        (f"output-claim:{prepared['receipt_row']['id']}",),
    )
    project.db.commit()

    replay = service.run_action("authority", action)
    assert replay.payload["status"] == "queued"
    assert replay.payload["receipt_id"] == prepared["receipt_row"]["id"]
    assert replay.payload["run_id"] == prepared["run_id"]
    assert replay.payload["job_id"] == job.id
    after = _prepared_tuple(project, idempotency_key=key)
    assert after["attempt_id"] == prepared["attempt_id"]
    assert after["attempt"]["state"] == "dispatching"
    assert [
        evidence["ref"]["job_id"]
        for evidence in after["receipt"]["evidence"]
        if evidence["ref"].get("job_id") is not None
    ] == [job.id]
    assert [
        queued.id
        for queued in workspace.queue.list_project_jobs(
            "authority",
            kind="project.run",
            limit=20,
        )
    ] == [job.id]


def test_half_receipt_never_adopts_an_unlinked_run_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    workspace = _workspace(tmp_path, require_confirmation=True)
    key = "packet-10:half-receipt-no-marker-adoption"
    action = _seed_action(
        workspace,
        "enrich.geocode",
        idempotency_key=key,
    )
    service = ActionRunService(workspace)
    action = _echo_confirmation(action, service.run_action("authority", action))
    bound = typed_action_for_request(action)
    params_hash = typed_request_hash(bound)

    project = workspace.get("authority")
    sheet_id = int(action["scope"]["sheet_id"])
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY id",
            (sheet_id,),
        ).fetchall()
    ]
    receipt_id = "receipt_pre_atomic_half"
    action_id = "action_pre_atomic_half"
    op_id = project.append_op(
        "map",
        {
            QUEUED_ACTION_RUN_MARKER_PARAM: {
                "schema_version": QUEUED_ACTION_RUN_MARKER_SCHEMA,
                "action_kind": action["action_id"],
                "receipt_id": receipt_id,
                "action_id": action_id,
                "params_hash": params_hash,
            }
        },
    )
    orphan_run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        action["action_id"],
        params={
            **_typed_map_rows_plan(bound).spec_dict(),
            QUEUED_ACTION_RUN_MARKER_PARAM: {
                "schema_version": QUEUED_ACTION_RUN_MARKER_SCHEMA,
                "action_kind": action["action_id"],
                "receipt_id": receipt_id,
                "action_id": action_id,
                "params_hash": params_hash,
            },
        },
        row_ids=row_ids,
    )
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=receipt_id,
            project_id="authority",
            action_id=action_id,
            action_kind=action["action_id"],
            idempotency_key=key,
            params_hash=params_hash,
            status="queued",
        )
    )
    before = {
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "routes": project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "attempts": project.db.execute(
            "SELECT COUNT(*) FROM execution_attempts"
        ).fetchone()[0],
        "claims": project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims"
        ).fetchone()[0],
    }

    replay = service.run_action("authority", action)

    assert replay.payload["status"] == "failed"
    assert [error["code"] for error in replay.payload["errors"]] == [
        "idempotency_in_progress"
    ]
    receipt_row = project.db.execute(
        "SELECT run_id, body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] is None
    assert json.loads(receipt_row["body"])["evidence"] == []
    assert (
        project.db.execute("SELECT id FROM runs ORDER BY id").fetchall()[0]["id"]
        == orphan_run_id
    )
    assert {
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "routes": project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "attempts": project.db.execute(
            "SELECT COUNT(*) FROM execution_attempts"
        ).fetchone()[0],
        "claims": project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims"
        ).fetchone()[0],
    } == before
    assert (
        workspace.queue.list_project_jobs(
            "authority",
            kind="project.run",
            limit=20,
        )
        == []
    )
