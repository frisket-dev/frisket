from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.google_sheets_action import _identity
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_families.exports import (
    _google_sheets_confirmation_hash,
)
from frisket.engine.executor.action_inventory import (
    ExecutorDeps,
)
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from helpers import replace_test_source_cell


ROOT = Path(__file__).resolve().parents[2]


def _seed_project(path: Path) -> int:
    project = Project.create(path, name="Google Sheets Export")
    try:
        sheet_id = project.add_sheet("Stories")
        column_ids = {
            "name": project.add_column(sheet_id, "name", "text"),
            "status": project.add_column(sheet_id, "status", "text"),
        }
        project.add_rows(
            sheet_id,
            [
                {"name": "Ada", "status": "ready"},
                {"name": "Grace", "status": "blocked"},
            ],
            column_ids,
        )
        return sheet_id
    finally:
        project.close()


def _google_export_action(
    *,
    sheet_id: int,
    connection_id: str = "conn-google-1",
    source: dict[str, Any] | None = None,
    destination: dict[str, Any] | None = None,
    key: str = "google_sheets_export@sha256:v1",
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": "export.google_sheets",
        "scope": {"kind": "project"},
        "params": {
            "connection_id": connection_id,
            "source": source or {"kind": "current_sheet", "sheet_id": sheet_id},
            "destination": destination
            or {
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Frisket export",
            },
            "write_policy": "replace_managed_tabs",
        },
        "idempotency_key": key,
    }
    return _confirm_google_action(action)


def _confirm_google_action(action: dict[str, Any]) -> dict[str, Any]:
    """Refresh confirmation after a test mutates any consent-bound field."""

    action.pop("confirmation", None)
    bound = typed_action_for_request(action)
    action["confirmation"] = _google_sheets_confirmation_hash(
        _identity(bound),
        GoogleSheetsExportRequest.model_validate(action["params"]),
        params_hash=typed_request_hash(bound),
    )
    return action


class FakeGoogleSheetsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def export_tabs(
        self,
        *,
        connection: dict[str, Any],
        destination: dict[str, Any],
        tabs: list[dict[str, Any]],
        write_policy: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "connection": connection,
                "destination": destination,
                "tabs": tabs,
                "write_policy": write_policy,
            }
        )
        return {
            "spreadsheet_id": destination.get("spreadsheet_id") or "sheet-created-1",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet-created-1",
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                    "sheet_id": tab["sheet_id"],
                }
                for tab in tabs
            ],
        }


class FailingGoogleSheetsClient:
    def __init__(self) -> None:
        self.calls = 0

    def export_tabs(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("provider unavailable")


class SequencedGoogleSheetsClient:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def export_tabs(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        sequence = len(self.calls)
        return {
            "spreadsheet_id": f"sheet-created-{sequence}",
            "spreadsheet_url": (
                f"https://docs.google.com/spreadsheets/d/sheet-created-{sequence}"
            ),
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                    "sheet_id": tab["sheet_id"],
                }
                for tab in request["tabs"]
            ],
        }


class RawResultGoogleSheetsClient:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def export_tabs(self, **_: Any) -> Any:
        self.calls += 1
        return self.result


def _resolver(provider: str, connection_id: str) -> dict[str, Any] | None:
    if provider == "google" and connection_id == "conn-google-1":
        return {
            "id": connection_id,
            "provider": "google",
            "external_subject": "google-user-123",
            "external_email": "reporter@example.com",
            "refresh_token": "refresh-secret-1",
            "scopes": [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.file",
            ],
        }
    return None


def test_export_google_sheets_uses_fake_client_receipts_and_idempotency(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = FakeGoogleSheetsClient()
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )

    project = Project(project_path)
    try:
        action = _google_export_action(
            sheet_id=sheet_id,
            source={
                "kind": "current_view",
                "sheet_id": sheet_id,
                "query": {
                    "schema_version": "frisket.query.v1",
                    "kind": "sheet.filter",
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filter": {"status": {"eq": "ready"}},
                    "sort": [{"column": "name", "dir": "asc"}],
                },
            },
        )
        result = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert result.status == "completed", result.errors
        assert result.receipt_id is not None
        assert len(fake_client.calls) == 1
        call = fake_client.calls[0]
        assert call["connection"]["id"] == "conn-google-1"
        assert call["write_policy"] == "replace_managed_tabs"
        assert call["tabs"][0]["columns"] == ["name", "status"]
        assert call["tabs"][0]["rows"] == [["Ada", "ready"]]
        assert call["tabs"][0]["query_hash"].startswith("sha256:")

        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = json.loads(receipt_row["body"])
        assert "refresh-secret-1" not in json.dumps(receipt)
        assert "refresh_token" not in json.dumps(receipt)
        assert receipt["action_kind"] == "export.google_sheets"
        assert receipt["provider_use"] == [
            {
                "provider": "google_sheets",
                "connection_id": "conn-google-1",
                "external_subject": "google-user-123",
                "external_email": "reporter@example.com",
            }
        ]
        assert receipt["exports"][0]["spreadsheet_id"] == "sheet-created-1"
        assert any(e["ref"]["kind"] == "exported_query" for e in receipt["evidence"])

        replay = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert len(fake_client.calls) == 1

        conflict_action = _google_export_action(
            sheet_id=sheet_id,
            destination={
                "kind": "google_sheets",
                "mode": "update_existing",
                "spreadsheet_id": "different",
            },
        )
        conflict = run_action_spec(
            project,
            conflict_action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert len(fake_client.calls) == 1
    finally:
        project.close()


def test_export_google_sheets_fails_without_connection_or_client(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets-missing.frisket"
    sheet_id = _seed_project(project_path)
    action = _google_export_action(sheet_id=sheet_id)
    project = Project(project_path)
    try:
        no_client = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=ExecutorDeps(connected_account_resolver=_resolver),
        )
        assert no_client.status == "failed"
        assert no_client.errors[0].code == "google_sheets_client_unavailable"

        missing_connection = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                connection_id="missing",
                key="google_sheets_export@sha256:missing-connection",
            ),
            project_id="project-google-sheets",
            deps=ExecutorDeps(
                connected_account_resolver=_resolver,
                google_sheets_client=FakeGoogleSheetsClient(),
            ),
        )
        assert missing_connection.status == "failed"
        assert missing_connection.errors[0].code == "connected_account_not_found"

        failing_client = FailingGoogleSheetsClient()
        provider_failure = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                key="google_sheets_export@sha256:provider-failure",
            ),
            project_id="project-google-sheets",
            deps=ExecutorDeps(
                connected_account_resolver=_resolver,
                google_sheets_client=failing_client,
            ),
        )
        assert provider_failure.status == "failed"
        assert (
            provider_failure.errors[0].code == "external_effect_reconciliation_required"
        )
        assert provider_failure.errors[0].details["reconciliation_required"] is True
        assert provider_failure.receipt_id is not None
        assert failing_client.calls == 1
        receipt_row = project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?",
            (provider_failure.receipt_id,),
        ).fetchone()
        assert receipt_row["status"] == "failed"
        assert "refresh-secret-1" not in receipt_row["body"]
        assert "refresh_token" not in receipt_row["body"]

        replay_failure = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                key="google_sheets_export@sha256:provider-failure",
            ),
            project_id="project-google-sheets",
            deps=ExecutorDeps(
                connected_account_resolver=_resolver,
                google_sheets_client=failing_client,
            ),
        )
        assert replay_failure.status == "failed"
        assert replay_failure.receipt_id == provider_failure.receipt_id
        assert failing_client.calls == 1
    finally:
        project.close()


def test_export_google_sheets_stale_clear_retry_replays_instead_of_rewriting(
    tmp_path: Path,
) -> None:
    """Defect 2 regression: client.export_tabs is an external WRITE between
    receipt reserve and finalize with no checkpoint machinery. A crash right
    after the write (the write lands, the checkpoint commits its own
    'returned' transaction, but this action's own receipt never reaches
    finalize) leaves the receipt stuck 'running' forever. Once the 1-hour
    stale-clear window passes, a retry under the SAME idempotency_key must
    replay the durable 'returned' checkpoint's stored provider_result -- not
    call ``export_tabs`` again. destination.mode="new_spreadsheet" makes the
    stakes concrete: a second call creates a SECOND spreadsheet, not an
    idempotent overwrite of the first.
    """

    from frisket.engine.executor.action_reservations import (
        RUNNING_RECEIPT_STALE_AFTER_SECONDS,
    )

    project_path = tmp_path / "google-sheets-stale-clear.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = FakeGoogleSheetsClient()
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )
    idempotency_key = "google_sheets_export@sha256:stale-clear-replay"
    action = _google_export_action(sheet_id=sheet_id, key=idempotency_key)

    project = Project(project_path)
    try:
        first = run_action_spec(
            project, action, project_id="project-google-sheets", deps=deps
        )
        assert first.status == "completed", first.errors
        assert len(fake_client.calls) == 1
        first_spreadsheet_id = first.outputs[0].ref["spreadsheet_id"]
        assert first_spreadsheet_id == "sheet-created-1"

        # The effect checkpoint is already 'returned' (its own transaction
        # committed the instant client.export_tabs returned) -- exactly what
        # a crash right after the write, before THIS action's own finalize,
        # leaves behind. Simulate that shape by reverting only the ACTION
        # receipt to the never-finalized 'running' state a hard crash would
        # leave, aged past the stale-clear window; the checkpoint is left
        # untouched.
        checkpoint_before = project.db.execute(
            "SELECT state FROM effect_checkpoints WHERE family=?",
            ("export_google_sheets_egress",),
        ).fetchone()
        assert checkpoint_before is not None
        assert checkpoint_before["state"] == "returned"

        project.db.execute(
            "UPDATE receipts SET status='running', "
            "created_at=datetime('now', ?) WHERE id=?",
            (f"-{RUNNING_RECEIPT_STALE_AFTER_SECONDS + 60} seconds", first.receipt_id),
        )
        project.db.commit()

        # Attempt 1: finds the stale 'running' receipt, clears it, asks for
        # a retry (the same two-step stale-clear contract every reserved
        # action shares).
        cleared = run_action_spec(
            project, action, project_id="project-google-sheets", deps=deps
        )
        assert cleared.status == "failed"
        assert [error.code for error in cleared.errors] == ["idempotency_stale_running"]
        assert (
            project.db.execute(
                "SELECT 1 FROM receipts WHERE id=?", (first.receipt_id,)
            ).fetchone()
            is None
        )

        # Attempt 2 (the actual retry): must replay the returned checkpoint,
        # not call export_tabs again -- a second new_spreadsheet call would
        # create a second, distinct spreadsheet.
        resumed = run_action_spec(
            project, action, project_id="project-google-sheets", deps=deps
        )
        assert resumed.status == "completed", resumed.errors
        assert len(fake_client.calls) == 1  # never called again
        assert resumed.outputs[0].ref["spreadsheet_id"] == first_spreadsheet_id
    finally:
        project.close()


def test_ambiguous_export_refuses_equivalent_fresh_key_without_second_call(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets-ambiguous-cross-key.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = SequencedGoogleSheetsClient(error=TimeoutError("provider timeout"))
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )

    project = Project(project_path)
    try:
        omitted_default = _google_export_action(
            sheet_id=sheet_id,
            key="ambiguous@sha256:first",
        )
        omitted_default["params"].pop("write_policy")
        omitted_default["params"]["destination"].pop("spreadsheet_title")
        _confirm_google_action(omitted_default)
        first = run_action_spec(
            project,
            omitted_default,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert first.status == "failed"
        assert first.errors[0].code == "external_effect_reconciliation_required"
        checkpoint_id = first.errors[0].details["checkpoint_id"]

        fresh_key = run_action_spec(
            project,
            _google_export_action(sheet_id=sheet_id, key="ambiguous@sha256:second"),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert fresh_key.status == "failed"
        assert fresh_key.errors[0].code == "external_effect_reconciliation_required"
        assert fresh_key.errors[0].details == {
            "checkpoint_code": "ambiguous_reserved",
            "checkpoint_id": checkpoint_id,
            "possible_external_effect": True,
            "reconciliation_required": True,
            "retryable": False,
        }
        assert len(fake_client.calls) == 1
        rows = project.db.execute(
            "SELECT id, state FROM effect_checkpoints WHERE family=?",
            ("export_google_sheets_egress",),
        ).fetchall()
        assert [(row["id"], row["state"]) for row in rows] == [
            (checkpoint_id, "reserved")
        ]
    finally:
        project.close()


def test_export_refuses_legacy_envelope_extras_without_provider_calls(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "strict-export.frisket"
    sheet_id = _seed_project(project_path)
    fake = FakeGoogleSheetsClient()
    project = Project(project_path)
    try:
        for field, value in (
            ("capabilities", ["project:read", "external:google_sheets"]),
            ("input_refs", [{"kind": "unrelated"}]),
            ("output_intent", [{"kind": "unrelated"}]),
        ):
            body = _google_export_action(sheet_id=sheet_id, key=field)
            body[field] = value
            result = run_action_spec(
                project,
                body,
                project_id="strict",
                deps=ExecutorDeps(
                    connected_account_resolver=_resolver, google_sheets_client=fake
                ),
            )
            assert result.status == "failed"
            assert result.errors[0].code == "invalid_action_request"
        assert fake.calls == []
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()


def test_ambiguous_export_identity_ignores_planned_confirmation_plumbing(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets-confirmation-invariance.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = SequencedGoogleSheetsClient(error=TimeoutError("provider timeout"))
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )

    first_body = _google_export_action(
        sheet_id=sheet_id,
        key="confirmation@sha256:first",
    )
    confirmed_body = _google_export_action(
        sheet_id=sheet_id,
        key="confirmation@sha256:confirmed",
    )

    project = Project(project_path)
    try:
        first = run_action_spec(
            project, first_body, project_id="project-google-sheets", deps=deps
        )
        assert first.status == "failed"
        checkpoint_id = first.errors[0].details["checkpoint_id"]

        refused = run_action_spec(
            project, confirmed_body, project_id="project-google-sheets", deps=deps
        )
        assert refused.status == "failed"
        assert refused.errors[0].details["checkpoint_id"] == checkpoint_id
        assert len(fake_client.calls) == 1
    finally:
        project.close()


def test_operator_accept_refuses_equivalent_effect_until_proven_discard(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets-operator-resolution.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = SequencedGoogleSheetsClient(error=TimeoutError("provider timeout"))
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )

    project = Project(project_path)
    try:
        first = run_action_spec(
            project,
            _google_export_action(sheet_id=sheet_id, key="operator@sha256:first"),
            project_id="project-google-sheets",
            deps=deps,
        )
        checkpoint_id = first.errors[0].details["checkpoint_id"]
        store = EffectCheckpointStore(project.db)
        assert store.operator_accept_charged(checkpoint_id)["state"] == "consumed"

        accepted = run_action_spec(
            project,
            _google_export_action(sheet_id=sheet_id, key="operator@sha256:accepted"),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert accepted.status == "failed"
        assert accepted.errors[0].details["checkpoint_code"] == (
            "invalid_checkpoint_state"
        )
        assert accepted.errors[0].details["checkpoint_id"] == checkpoint_id
        assert len(fake_client.calls) == 1

        discarded = store.operator_discard(checkpoint_id, expected_state="consumed")
        assert discarded["id"] == checkpoint_id
        fake_client.error = None
        released = run_action_spec(
            project,
            _google_export_action(sheet_id=sheet_id, key="operator@sha256:discarded"),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert released.status == "completed", released.errors
        assert len(fake_client.calls) == 2
    finally:
        project.close()


def test_returned_effect_replays_cross_key_but_changed_query_or_value_is_new(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "google-sheets-cross-key-returned.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = SequencedGoogleSheetsClient()
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )
    ready_query = {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"status": {"eq": "ready"}},
        "sort": [{"column": "name", "dir": "asc"}],
    }
    blocked_query = {
        **ready_query,
        "filter": {"status": {"eq": "blocked"}},
    }

    project = Project(project_path)
    try:
        first = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                source={
                    "kind": "current_view",
                    "sheet_id": sheet_id,
                    "query": ready_query,
                },
                key="returned@sha256:first",
            ),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert first.status == "completed", first.errors
        assert first.outputs[0].ref["spreadsheet_id"] == "sheet-created-1"

        equivalent_fresh_key = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                source={
                    "kind": "current_view",
                    "sheet_id": sheet_id,
                    "query": ready_query,
                },
                key="returned@sha256:equivalent-fresh-key",
            ),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert equivalent_fresh_key.status == "completed", equivalent_fresh_key.errors
        assert (
            equivalent_fresh_key.outputs[0].ref["spreadsheet_id"] == "sheet-created-1"
        )
        assert len(fake_client.calls) == 1

        changed_query = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                source={
                    "kind": "current_view",
                    "sheet_id": sheet_id,
                    "query": blocked_query,
                },
                key="returned@sha256:changed-query",
            ),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert changed_query.status == "completed", changed_query.errors
        assert changed_query.outputs[0].ref["spreadsheet_id"] == "sheet-created-2"
        assert len(fake_client.calls) == 2

        name_column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='name'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        ada_row_id = int(
            project.db.execute(
                "SELECT row_id FROM cells WHERE column_id=? AND value=?",
                (name_column_id, json.dumps("Ada")),
            ).fetchone()["row_id"]
        )
        replace_test_source_cell(
            project,
            row_id=ada_row_id,
            column_id=name_column_id,
            value="Ada Lovelace",
        )

        changed_live_value = run_action_spec(
            project,
            _google_export_action(
                sheet_id=sheet_id,
                source={
                    "kind": "current_view",
                    "sheet_id": sheet_id,
                    "query": ready_query,
                },
                key="returned@sha256:changed-live-value",
            ),
            project_id="project-google-sheets",
            deps=deps,
        )
        assert changed_live_value.status == "completed", changed_live_value.errors
        assert changed_live_value.outputs[0].ref["spreadsheet_id"] == "sheet-created-3"
        assert len(fake_client.calls) == 3

        checkpoints = project.db.execute(
            "SELECT group_key, identity, payload FROM effect_checkpoints WHERE family=? "
            "ORDER BY id",
            ("export_google_sheets_egress",),
        ).fetchall()
        assert len(checkpoints) == 3
        assert len({row["identity"] for row in checkpoints}) == 3
        assert all(row["group_key"] == row["identity"] for row in checkpoints)
        assert all(row["identity"].startswith("sha256:") for row in checkpoints)
        assert "Ada Lovelace" not in "".join(row["payload"] for row in checkpoints)
    finally:
        project.close()


def test_provider_failure_is_bounded_redacted_and_exact_replay_is_stable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_path = tmp_path / "google-sheets-provider-redaction.frisket"
    sheet_id = _seed_project(project_path)
    refresh_secret = "refresh-super-secret-value"
    access_secret = "access-super-secret-value"
    client_secret = "client-super-secret-value"
    oversized = "x" * 10_000
    fake_client = SequencedGoogleSheetsClient(
        error=RuntimeError(
            f"access_token={access_secret} refresh_token={refresh_secret} "
            f"client_secret={client_secret} response={oversized}"
        )
    )

    def secret_resolver(provider: str, connection_id: str) -> dict[str, Any] | None:
        if provider != "google" or connection_id != "conn-google-1":
            return None
        return {
            "id": connection_id,
            "provider": "google",
            "external_subject": "subject-1",
            "refresh_token": refresh_secret,
            "access_token": access_secret,
            "client_secret": client_secret,
        }

    deps = ExecutorDeps(
        connected_account_resolver=secret_resolver,
        google_sheets_client=fake_client,
    )
    action = _google_export_action(
        sheet_id=sheet_id,
        key="redaction@sha256:stable-replay",
    )
    caplog.set_level(logging.DEBUG, logger="frisket.executor")

    project = Project(project_path)
    try:
        failed = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert failed.status == "failed"
        assert failed.errors[0].code == "external_effect_reconciliation_required"
        assert failed.errors[0].details["reconciliation_required"] is True
        assert len(json.dumps(failed.model_dump(mode="json"))) < 4_000

        replay = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert replay.model_dump(mode="json") == failed.model_dump(mode="json")
        assert len(fake_client.calls) == 1

        persisted = "\n".join(
            [
                caplog.text,
                *(
                    row["body"]
                    for row in project.db.execute("SELECT body FROM receipts")
                ),
                *(
                    str(row["payload"])
                    for row in project.db.execute(
                        "SELECT payload FROM effect_checkpoints"
                    )
                ),
            ]
        )
        for secret in (refresh_secret, access_secret, client_secret):
            assert secret not in persisted
        assert oversized not in persisted
        assert "Traceback (most recent call last)" not in caplog.text
    finally:
        project.close()


@pytest.mark.parametrize(
    ("provider_result", "destination"),
    [
        (None, None),
        ({"spreadsheet_id": ""}, None),
        (
            {"spreadsheet_id": "provider-returned-the-wrong-sheet"},
            {
                "kind": "google_sheets",
                "mode": "update_existing",
                "spreadsheet_id": "requested-sheet",
            },
        ),
    ],
)
def test_malformed_provider_result_stays_ambiguous_and_never_completes(
    tmp_path: Path,
    provider_result: Any,
    destination: dict[str, Any] | None,
) -> None:
    project_path = tmp_path / "google-sheets-malformed-result.frisket"
    sheet_id = _seed_project(project_path)
    fake_client = RawResultGoogleSheetsClient(provider_result)
    deps = ExecutorDeps(
        connected_account_resolver=_resolver,
        google_sheets_client=fake_client,
    )
    action = _google_export_action(
        sheet_id=sheet_id,
        destination=destination,
        key="malformed-result@sha256:stable-replay",
    )

    project = Project(project_path)
    try:
        failed = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert failed.status == "failed"
        assert failed.outputs == []
        assert failed.errors[0].code == "external_effect_reconciliation_required"
        assert failed.errors[0].details["checkpoint_code"] == (
            "provider_result_invalid"
        )
        checkpoint = EffectCheckpointStore(project.db).get(
            failed.errors[0].details["checkpoint_id"]
        )
        assert checkpoint is not None
        assert checkpoint["state"] == "reserved"
        assert "provider_result" not in checkpoint["payload"]
        assert fake_client.calls == 1

        replay = run_action_spec(
            project,
            action,
            project_id="project-google-sheets",
            deps=deps,
        )
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert fake_client.calls == 1
    finally:
        project.close()
