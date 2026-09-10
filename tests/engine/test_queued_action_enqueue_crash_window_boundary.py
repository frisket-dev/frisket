from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import copy
import importlib
import json
import multiprocessing
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.execution.provider import ExecutionCompositionContext
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


@pytest.fixture(autouse=True)
def _pin_zero_standing_preapproval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep positive-cost fixtures on their explicit-confirmation path."""

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


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


def _action_kind(action: dict[str, Any]) -> str:
    return action.get("action_id", action.get("kind"))


def _sheet_id(action: dict[str, Any]) -> int:
    return int(
        action["scope"]["sheet_id"]
        if "action_id" in action
        else action["params"]["sheet_id"]
    )


# The former durable seats named by packet 10. Boundaries 13 and 14 share the
# same test hook because edition context now commits in boundary 13's project
# transaction; both must therefore expose the same complete pending tuple.
PRECOMMIT_BOUNDARY_TARGETS = {
    1: "frisket.engine.store.receipts.ReceiptStore.insert_queued",
    2: "frisket.engine.store.output_claims.OutputColumnClaimStore.acquire",
    3: "frisket.engine.store.project.Project.add_column",
    4: "frisket.engine.store.project.Project.append_op",
    5: "frisket.engine.store.project.Project.set_undo_info",
    6: "frisket.engine.store.runs.RunResultStore.start_run",
    7: "frisket.engine.store.runs.RunResultStore.point_column_at_run",
    8: "frisket.engine.runner.preparation.retire_dropped_output_columns",
    9: "frisket.engine.runner.validation.persist_resolved_execution",
    10: (
        "frisket.engine.executor.action_reservations._mark_queued_action_run_prepared"
    ),
    11: "frisket.execution.attempt_authority.open_attempt",
    12: "frisket.execution.attempt_authority.admit_attempt",
}

OUTPUT_WRITER_TARGETS = {
    "add": "frisket.engine.store.project.Project.add_column",
    "revive": "frisket.engine.store.project.Project.add_column",
    "retype": "frisket.engine.store.project.Project.set_column_type",
    "semantic_retype": (
        "frisket.engine.store.project.Project.set_column_semantic_type"
    ),
    "format_retype": "frisket.engine.store.project.Project.set_column_format",
}

PROJECT_COUNT_QUERIES = {
    "receipts": "SELECT COUNT(*) FROM receipts",
    "output_column_claims": "SELECT COUNT(*) FROM output_column_claims",
    "ops": "SELECT COUNT(*) FROM ops",
    "runs": "SELECT COUNT(*) FROM runs",
    "run_scopes": "SELECT COUNT(*) FROM run_scopes",
    "run_rows": "SELECT COUNT(*) FROM run_rows",
    "consents": "SELECT COUNT(*) FROM consents",
    "promise_sets": "SELECT COUNT(*) FROM promise_sets",
    "routes": "SELECT COUNT(*) FROM routes",
    "execution_attempts": "SELECT COUNT(*) FROM execution_attempts",
    "attempt_row_authorizations": ("SELECT COUNT(*) FROM attempt_row_authorizations"),
}


def _seed_action(
    workspace: Workspace,
    action_kind: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    project = workspace.get("authority")
    sheet_id = project.add_sheet(action_kind)
    if action_kind == "media.transcribe":
        source_id = project.add_column(sheet_id, "media", type="audio")
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
            {"media": source_id},
        )
        params = {
            "source": "media",
            "engine": "faster_whisper",
        }
        outputs = {"text": "transcript"}
    elif action_kind == "media.ocr":
        source_id = project.add_column(sheet_id, "media", type="image")
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
            {"media": source_id},
        )
        params = {
            "source": "media",
            "engine": "rapidocr",
            "language": "en",
            "dpi": 180,
        }
        outputs = {"text": "ocr_text"}
    elif action_kind == "media.to_markdown":
        source_id = project.add_column(sheet_id, "doc", type="file")
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
            {"doc": source_id},
        )
        return {
            "action_id": action_kind,
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"source": "doc", "engine": "markitdown"},
            "output_names": {"markdown": "markdown"},
            "idempotency_key": idempotency_key,
        }
    elif action_kind == "enrich.geocode":
        source_id = project.add_column(sheet_id, "address", type="text")
        project.add_rows(
            sheet_id,
            [{"address": "1600 Pennsylvania Avenue NW, Washington, DC"}],
            {"address": source_id},
        )
        params = {
            "source": "address",
            "engine": "opencage",
            "include_lat_lon": False,
        }
    elif action_kind == "enrich.census_demographics":
        source_id = project.add_column(sheet_id, "point", type="geo_point")
        project.add_rows(
            sheet_id,
            [{"point": {"lat": 38.9, "lon": -77.03}}],
            {"point": source_id},
        )
        params = {
            "source": "point",
            "geography": "tract",
            "include_moe": False,
        }
    else:  # pragma: no cover - closed parameter roster
        raise AssertionError(action_kind)
    if action_kind in {"enrich.geocode", "enrich.census_demographics"}:
        return {
            "action_id": action_kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": params,
            "idempotency_key": idempotency_key,
            **(
                {"output_names": {"geo_point": "location"}}
                if action_kind == "enrich.geocode"
                else {}
            ),
        }
    return {
        "action_id": action_kind,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": params,
        "output_names": outputs,
        "idempotency_key": idempotency_key,
    }


def _confirmed_action(
    service: ActionRunService,
    action: dict[str, Any],
) -> dict[str, Any]:
    # This fixture prices geocode; free Census calls are preapproved.
    if _action_kind(action) != "enrich.geocode":
        return action
    challenge = service.run_action("authority", action)
    assert challenge.payload["status"] == "needs_confirmation"
    promise_set_hash = challenge.payload["errors"][0]["details"]["promise_set_hash"]
    confirmed = copy.deepcopy(action)
    confirmed["confirmation"] = promise_set_hash
    return confirmed


def _seed_case(
    root: Path,
    action_kind: str,
    *,
    idempotency_key: str,
    output_writer_variant: str | None = None,
    retirement: bool = False,
    confirm_action: bool = True,
) -> dict[str, Any]:
    workspace = Workspace(root, enable_local_model_pull=False)
    workspace.create("Execution authority", project_id="authority")
    action = _seed_action(
        workspace,
        action_kind,
        idempotency_key=idempotency_key,
    )
    project = workspace.get("authority")
    if output_writer_variant is not None:
        sheet_id = _sheet_id(action)
        if output_writer_variant == "format_retype":
            if action_kind != "media.to_markdown":
                raise AssertionError(
                    "format writer variant uses the routed to-markdown seam"
                )
            project.add_column(
                sheet_id,
                "markdown",
                type="text",
                ai_generated=True,
                format=None,
            )
        elif action_kind != "enrich.geocode":
            raise AssertionError("writer variants use the routed geocode seam")
        elif output_writer_variant == "revive":
            project.add_column(
                sheet_id,
                "location",
                type="text",
                ai_generated=True,
                hidden=True,
            )
        elif output_writer_variant == "retype":
            project.add_column(
                sheet_id,
                "location",
                type="text",
                ai_generated=True,
            )
        elif output_writer_variant == "semantic_retype":
            column_id = project.add_column(
                sheet_id,
                "location",
                type="geo_point",
                ai_generated=True,
            )
            project.set_column_semantic_type(column_id, "packet_10_prior_geocode")
        elif output_writer_variant != "add":
            raise AssertionError(output_writer_variant)
        if output_writer_variant in {
            "retype",
            "semantic_retype",
            "format_retype",
        }:
            if "action_id" in action:
                action["replace_existing"] = True
            else:
                action["output_intent"] = [{"kind": "overwrite_existing"}]
    if retirement:
        sheet_id = _sheet_id(action)
        project.add_column(
            sheet_id,
            "location_retired",
            type="category",
            ai_generated=True,
        )
    if confirm_action:
        action = _confirmed_action(ActionRunService(workspace), action)
    project.close()
    return action


def _located(target: str) -> Any:
    parts = target.split(".")
    for index in range(len(parts), 0, -1):
        try:
            value: Any = importlib.import_module(".".join(parts[:index]))
        except ModuleNotFoundError:
            continue
        for part in parts[index:]:
            value = getattr(value, part)
        return value
    raise AssertionError(f"cannot resolve crash target {target!r}")


def _pause_and_exit(
    boundary: int,
    reached: Any,
    release: Any,
) -> None:
    reached.set()
    if not release.wait(timeout=30):
        os._exit(180 + boundary)
    os._exit(80 + boundary)


def _exit_after(
    boundary: int,
    original: Any,
    reached: Any,
    release: Any,
) -> Any:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _pause_and_exit(boundary, reached, release)
        return result  # pragma: no cover - os._exit does not return

    return wrapped


def _exit_after_pointer_batch(
    boundary: int,
    original: Any,
    reached: Any,
    release: Any,
) -> Any:
    def wrapped(store: Any, op_id: int, *args: Any, **kwargs: Any) -> Any:
        result = original(store, op_id, *args, **kwargs)
        row = store.db.execute(
            "SELECT undo_info FROM ops WHERE id=?",
            (op_id,),
        ).fetchone()
        info = json.loads(row["undo_info"] or "{}") if row is not None else {}
        created = {int(value) for value in info.get("created_columns", [])}
        pointed = {int(value) for value in info.get("column_pointers_after", {})}
        if created and created <= pointed:
            _pause_and_exit(boundary, reached, release)
        return result

    return wrapped


def _exit_after_real_retirement(
    boundary: int,
    original: Any,
    reached: Any,
    release: Any,
) -> Any:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        retired = original(*args, **kwargs)
        if retired:
            _pause_and_exit(boundary, reached, release)
        return retired

    return wrapped


def _exit_before(
    boundary: int,
    reached: Any,
    release: Any,
) -> Any:
    def wrapped(*_args: Any, **_kwargs: Any) -> Any:
        _pause_and_exit(boundary, reached, release)

    return wrapped


def _crash_child(
    root_text: str,
    action: dict[str, Any],
    boundary: int,
    reached: Any,
    release: Any,
    output_writer_variant: str | None,
    retirement: bool,
) -> None:
    root = Path(root_text)
    workspace = Workspace(root, enable_local_model_pull=False)
    service = ActionRunService(workspace)
    with ExitStack() as stack:
        if retirement:
            # Exercise the surviving generic retirement transaction with a
            # declared test role. Built-in typed ASR intentionally leaves old
            # undeclared language columns alone; it is not this fixture's API.
            stack.enter_context(
                patch(
                    "frisket.engine.executor.map_rows_action."
                    "_TypedMapRowsProgram.retired_output_names",
                    return_value=["location_retired"],
                )
            )

        if boundary == 3 and output_writer_variant is not None:
            target = OUTPUT_WRITER_TARGETS[output_writer_variant]
            replacement = _exit_after(
                boundary,
                _located(target),
                reached,
                release,
            )
        elif boundary == 7:
            target = PRECOMMIT_BOUNDARY_TARGETS[boundary]
            replacement = _exit_after_pointer_batch(
                boundary,
                _located(target),
                reached,
                release,
            )
        elif boundary == 8:
            target = PRECOMMIT_BOUNDARY_TARGETS[boundary]
            replacement = _exit_after_real_retirement(
                boundary,
                _located(target),
                reached,
                release,
            )
        elif boundary in PRECOMMIT_BOUNDARY_TARGETS:
            target = PRECOMMIT_BOUNDARY_TARGETS[boundary]
            replacement = _exit_after(
                boundary,
                _located(target),
                reached,
                release,
            )
        elif boundary in {13, 14}:
            target = "frisket.engine.jobs.queue.SqliteJobQueue.enqueue"
            replacement = _exit_before(boundary, reached, release)
        elif boundary == 15:
            target = "frisket.engine.jobs.queue.SqliteJobQueue.enqueue"
            replacement = _exit_after(
                boundary,
                _located(target),
                reached,
                release,
            )
        elif boundary == 16:
            target = "frisket.engine.executor.action_reservations._mark_queued_action_enqueued"
            replacement = _exit_after(
                boundary,
                _located(target),
                reached,
                release,
            )
        else:  # pragma: no cover - closed parameter roster
            raise AssertionError(boundary)
        stack.enter_context(patch(target, replacement))
        service.run_action(
            "authority",
            action,
            request_context=_edition_request({"edition": "packet-10"}),
        )
    raise AssertionError(f"boundary {boundary} did not execute")


def _run_crash_child(
    root: Path,
    action: dict[str, Any],
    boundary: int,
    *,
    output_writer_variant: str | None = None,
    retirement: bool = False,
) -> None:
    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    release = context.Event()
    child = context.Process(
        target=_crash_child,
        args=(
            str(root),
            action,
            boundary,
            reached,
            release,
            output_writer_variant,
            retirement,
        ),
    )
    started = False
    try:
        child.start()
        started = True
        assert reached.wait(timeout=30), (
            f"boundary {boundary} child never reached its exact crash seat"
        )
        release.set()
        child.join(timeout=30)
        assert not child.is_alive(), f"boundary {boundary} child did not exit"
        assert child.exitcode == 80 + boundary
    finally:
        release.set()
        if started and child.is_alive():
            child.terminate()
            child.join(timeout=5)
        if started and child.is_alive():
            child.kill()
            child.join(timeout=5)
        if started and not child.is_alive():
            child.close()


def _project_snapshot(project: Any) -> dict[str, Any]:
    return {
        "counts": {
            name: int(project.db.execute(query).fetchone()[0])
            for name, query in PROJECT_COUNT_QUERIES.items()
        },
        "columns": [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, sheet_id, name, type, format, ai_generated, "
                "hidden, default_hidden, semantic_type, current_run_id "
                "FROM columns ORDER BY id"
            ).fetchall()
        ],
    }


def _prepared_tuple(
    project: Any,
    idempotency_key: str,
    action: dict[str, Any],
) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    assert row is not None
    assert type(row["run_id"]) is int
    receipt = json.loads(row["body"])
    prepared = [
        evidence["ref"]
        for evidence in receipt["evidence"]
        if evidence["ref"].get("kind") == "queued_action_run_prepared"
    ]
    assert len(prepared) == 1
    attempt_id = prepared[0].get("attempt_id")
    assert isinstance(attempt_id, str) and attempt_id
    run_id = int(row["run_id"])
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run is not None
    assert int(run["sheet_id"]) == _sheet_id(action)
    assert json.loads(run["edition_run_context"]) == {"edition": "packet-10"}
    route = project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    ).fetchall()
    assert len(route) == 1
    promise_set = project.db.execute(
        "SELECT * FROM promise_sets WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    ).fetchall()
    assert len(promise_set) == 1
    assert route[0]["promise_set_id"] == promise_set[0]["id"]
    attempt = project.db.execute(
        "SELECT * FROM execution_attempts WHERE id=? AND run_id=?",
        (attempt_id, run_id),
    ).fetchone()
    assert attempt is not None
    assert attempt["seq"] == 0
    assert attempt["state"] == "admitted"
    assert attempt["head_route_id"] == route[0]["id"]
    assert attempt["head_promise_set_id"] == promise_set[0]["id"]
    scope = project.db.execute(
        "SELECT row_count FROM run_scopes WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert scope is not None
    run_rows = project.db.execute(
        "SELECT row_id, position FROM run_rows WHERE run_id=? ORDER BY position",
        (run_id,),
    ).fetchall()
    assert len(run_rows) == int(scope["row_count"])
    assert json.loads(attempt["scope_json"]) == [
        int(value["row_id"]) for value in run_rows
    ]
    row_authorizations = project.db.execute(
        "SELECT row_id, quoted_quantity, terminal_outcome "
        "FROM attempt_row_authorizations WHERE attempt_id=? ORDER BY row_id",
        (attempt_id,),
    ).fetchall()
    cost_basis = json.loads(attempt["cost_basis_json"])
    row_quotes = cost_basis.get("row_quote_quantities", [])
    assert [
        (int(value["row_id"]), str(value["quoted_quantity"]))
        for value in row_authorizations
    ] == [(int(value["row_id"]), str(value["quantity"])) for value in row_quotes]
    assert all(value["terminal_outcome"] is None for value in row_authorizations)
    claims = project.db.execute(
        "SELECT c.*, col.name AS column_name, col.type AS column_type, "
        "col.semantic_type AS column_semantic_type, "
        "col.format AS column_format, "
        "col.current_run_id AS column_run_id "
        "FROM output_column_claims c "
        "JOIN columns col ON col.id=c.column_id "
        "WHERE c.run_id=? AND c.status='active' "
        "ORDER BY col.position, col.id",
        (run_id,),
    ).fetchall()
    assert claims
    bound = typed_action_for_request(action)
    expected_fields = build_typed_map_rows_plan(
        project, bound, _allow_existing_outputs=True
    ).output_fields
    expected = {
        str(field["name"]): (
            str(field["column_type"]),
            field.get("semantic_type"),
            field.get("format"),
        )
        for field in expected_fields
    }
    assert {
        str(claim["column_name"]): (
            str(claim["column_type"]),
            claim["column_semantic_type"],
            claim["column_format"],
        )
        for claim in claims
    } == expected
    claim_token = f"output-claim:{row['id']}"
    assert {str(claim["claim_token"]) for claim in claims} == {claim_token}
    assert all(int(claim["sheet_id"]) == int(run["sheet_id"]) for claim in claims)
    assert all(int(claim["run_id"]) == run_id for claim in claims)
    assert all(int(claim["op_id"]) == int(run["op_id"]) for claim in claims)
    assert all(int(claim["column_run_id"]) == run_id for claim in claims)
    assert receipt["run_id"] == run_id
    assert prepared[0]["run_id"] == run_id
    assert prepared[0]["attempt_id"] == attempt_id
    return {
        "run_id": run_id,
        "receipt_id": str(row["id"]),
        "attempt_id": attempt_id,
        "claim_token": claim_token,
        "route_id": str(route[0]["id"]),
        "promise_set_id": str(promise_set[0]["id"]),
        "output_column_ids": tuple(int(claim["column_id"]) for claim in claims),
    }


def _acknowledged_job_ids(project: Any, receipt_id: str) -> list[int]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    body = json.loads(row["body"])
    return [
        int(evidence["ref"]["job_id"])
        for evidence in body["evidence"]
        if isinstance(evidence["ref"].get("job_id"), int)
        and not isinstance(evidence["ref"].get("job_id"), bool)
    ]


@pytest.mark.parametrize(
    "boundary",
    range(1, 17),
    ids=tuple(f"boundary-{boundary:02d}" for boundary in range(1, 17)),
)
def test_routed_preparation_process_death_is_all_or_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    root = tmp_path / "workspace"
    key = f"packet-10:death:geocode:{boundary}"
    action = _seed_case(
        root,
        "enrich.geocode",
        idempotency_key=key,
        retirement=boundary == 8,
    )
    before_workspace = Workspace(root, enable_local_model_pull=False)
    before_project = before_workspace.get("authority")
    baseline = _project_snapshot(before_project)
    before_project.close()

    _run_crash_child(
        root,
        action,
        boundary,
        retirement=boundary == 8,
    )

    workspace = Workspace(root, enable_local_model_pull=False)
    project = workspace.get("authority")
    if boundary < 13:
        assert _project_snapshot(project) == baseline
        assert (
            workspace.queue.list_project_jobs("authority", kind="project.run", limit=20)
            == []
        )
        return

    prepared = _prepared_tuple(project, key, action)
    jobs_before_replay = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    prior_job_id = None
    if boundary in {13, 14}:
        assert jobs_before_replay == []
    else:
        assert len(jobs_before_replay) == 1
        prior_job_id = jobs_before_replay[0].id
    if boundary == 16:
        assert _acknowledged_job_ids(
            project,
            prepared["receipt_id"],
        ) == [prior_job_id]
    else:
        assert (
            _acknowledged_job_ids(
                project,
                prepared["receipt_id"],
            )
            == []
        )

    replay = ActionRunService(workspace).run_action(
        "authority",
        action,
        request_context=_edition_request({"edition": "packet-10"}),
    )
    assert replay.payload["status"] == "queued"
    assert replay.payload["run_id"] == prepared["run_id"]
    assert replay.payload["receipt_id"] == prepared["receipt_id"]
    jobs = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    assert len(jobs) == 1
    assert jobs[0].id == replay.payload["job_id"]
    if prior_job_id is not None:
        assert jobs[0].id == prior_job_id
    assert jobs[0].run_id == prepared["run_id"]
    assert jobs[0].receipt_id == prepared["receipt_id"]
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?",
            (prepared["receipt_id"],),
        ).fetchone()[0]
    )
    acknowledged = [
        evidence["ref"]
        for evidence in body["evidence"]
        if evidence["ref"].get("job_id") == jobs[0].id
    ]
    assert len(acknowledged) == 1


@pytest.mark.parametrize(
    "output_writer_variant",
    ("revive", "retype", "semantic_retype", "format_retype"),
    ids=(
        "revive",
        "retype",
        "semantic-retype",
        "format-retype",
    ),
)
def test_unowned_output_column_is_refused_before_writer_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_writer_variant: str,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    root = tmp_path / "workspace"
    key = f"packet-10:death:writer:{output_writer_variant}"
    action_kind = (
        "media.to_markdown"
        if output_writer_variant == "format_retype"
        else "enrich.geocode"
    )
    action = _seed_case(
        root,
        action_kind,
        idempotency_key=key,
        output_writer_variant=output_writer_variant,
        confirm_action=False,
    )
    workspace = Workspace(root, enable_local_model_pull=False)
    project = workspace.get("authority")
    baseline = _project_snapshot(project)
    result = ActionRunService(workspace).run_action("authority", action)

    assert result.payload["status"] == "failed"
    if "action_id" in action:
        assert result.payload["errors"][0]["code"] == "output_column_exists"
    assert _project_snapshot(project) == baseline
    assert (
        workspace.queue.list_project_jobs(
            "authority",
            kind="project.run",
            limit=20,
        )
        == []
    )


@pytest.mark.parametrize(
    ("action_kind", "boundary"),
    tuple(
        (action_kind, boundary)
        for action_kind in ROUTED_ACTIONS
        for boundary in (13, 15, 16)
    ),
    ids=tuple(
        f"{action_kind}-boundary-{boundary}"
        for action_kind in ROUTED_ACTIONS
        for boundary in (13, 15, 16)
    ),
)
def test_commit_projection_and_ack_death_boundaries_cover_all_five_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
    boundary: int,
) -> None:
    monkeypatch.setenv("OPENCAGE_API_KEY", "packet-10-test-key")
    monkeypatch.setenv("CENSUS_API_KEY", "packet-10-test-key")
    root = tmp_path / "workspace"
    key = f"packet-10:death:{action_kind}:{boundary}"
    action = _seed_case(root, action_kind, idempotency_key=key)

    _run_crash_child(root, action, boundary)

    workspace = Workspace(root, enable_local_model_pull=False)
    project = workspace.get("authority")
    prepared = _prepared_tuple(project, key, action)
    jobs_before_replay = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    prior_job_id = None
    if boundary == 13:
        assert jobs_before_replay == []
    else:
        assert len(jobs_before_replay) == 1
        prior_job_id = jobs_before_replay[0].id
    if boundary == 16:
        assert _acknowledged_job_ids(
            project,
            prepared["receipt_id"],
        ) == [prior_job_id]
    else:
        assert (
            _acknowledged_job_ids(
                project,
                prepared["receipt_id"],
            )
            == []
        )
    replay = ActionRunService(workspace).run_action(
        "authority",
        action,
        request_context=_edition_request({"edition": "packet-10"}),
    )
    assert replay.payload["status"] == "queued"
    assert replay.payload["run_id"] == prepared["run_id"]
    assert replay.payload["receipt_id"] == prepared["receipt_id"]
    jobs = workspace.queue.list_project_jobs(
        "authority",
        kind="project.run",
        limit=20,
    )
    assert len(jobs) == 1
    assert jobs[0].id == replay.payload["job_id"]
    if prior_job_id is not None:
        assert jobs[0].id == prior_job_id
    assert jobs[0].run_id == prepared["run_id"]
    assert jobs[0].receipt_id == prepared["receipt_id"]
