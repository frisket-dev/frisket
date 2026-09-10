"""Team-edition Google Sheets export wiring (bugfix, 2026-07-30).

`frisket.team.oauth_connections` gives the open team edition a full Google
OAuth flow -- routes, scopes, encrypted refresh-token storage
(TeamSecretBox), the `oauth_connections` table -- but `create_team_app`
never wired any of it into `ExecutorDeps.connected_account_resolver`
(engine/executor/action_inventory.py), the one thing
`_run_export_google_sheets` (engine/executor/action_families/exports.py)
actually reads. A user could walk through Google consent, see a stored
connection in Settings, launch the export action, and it would still fail
`connected_account_not_found` -- OAuth machinery with no working consumer.

This file proves the fix end-to-end (connection stored -> export runs ->
resolver used, no `connected_account_not_found`) and pins the two structural
facts that make the miss unrepeatable:

* `_team_executor_deps_factory` ALWAYS supplies a `connected_account_resolver`
  -- a plain callable that itself returns None for anything it can't resolve
  -- regardless of whether an operator has configured a Google OAuth app.
  Only `google_sheets_client` (the second, separate dependency) depends on
  `config.google_client_id`/`google_client_secret` being set. This is the
  "make the wiring non-optional where the work happens" half of the fix: the
  resolver is unconditional, so a future call site cannot forget to pass one.
* the bare local single-user tier (a real `create_app(workspace)` composition,
  no team wrapper at all) still has no resolver and reports so through the
  catalog (test_google_sheets_connected_account_hint.py covers this in
  depth; one assertion is repeated here for the "both halves in one place"
  record).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.engine.jobs.worker import Worker
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.oauth_connections import TeamOAuthConnectionService
from frisket.team.schema import users
from frisket.team.secret_box import TeamSecretBox
from tests.team_setup_helpers import claim_server


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
            "spreadsheet_id": "team-wired-1",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/team-wired-1",
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "sheet_id": tab["sheet_id"],
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                }
                for tab in tabs
            ],
        }


def _config(tmp_path: Path, **overrides: Any) -> TeamConfig:
    values: dict[str, Any] = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Sheets Desk",
        admin_emails={"owner@example.com"},
    )
    values.update(overrides)
    return TeamConfig(**values)


def _google_export_action(*, sheet_id: int, connection_id: str) -> dict[str, Any]:
    return {
        "action_id": "export.google_sheets",
        "scope": {"kind": "project"},
        "output_names": {},
        "params": {
            "connection_id": connection_id,
            "source": {"kind": "current_sheet", "sheet_id": sheet_id},
            "destination": {
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Team export",
            },
            "write_policy": "replace_managed_tabs",
        },
        "idempotency_key": "team-google-sheets@sha256:v1",
    }


def _confirm_export(browser: TestClient, project_id: str, action: dict[str, Any]):
    challenge = browser.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert challenge.status_code == 402, challenge.text
    action["confirmation"] = challenge.json()["errors"][0]["details"][
        "promise_set_hash"
    ]
    return browser.post(f"/api/projects/{project_id}/actions/v1/run", json=action)


def test_stored_connection_and_export_runs_end_to_end(tmp_path: Path) -> None:
    fake_client = FakeGoogleSheetsClient()
    config = _config(
        tmp_path,
        google_client_id="client-id",
        google_client_secret="client-secret",
    )
    app = create_team_app(config, google_sheets_client=fake_client)
    browser = claim_server(app)  # returns an authenticated owner session

    org_id = app.state.team_org_id
    with app.state.control_engine.connect() as cx:
        owner_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()

    # Seed a stored connection the SAME way the real OAuth callback would
    # (oauth_connections.py's production_google_connection_exchange ->
    # connect_google_account) -- a second TeamSecretBox(config) instance
    # derives the identical key (same config -> same key file/derivation),
    # so what it encrypts here is exactly what the app's own resolver (built
    # from ITS secret_box, inside create_team_app) can decrypt.
    secret_box = TeamSecretBox(config)
    service = TeamOAuthConnectionService(
        app.state.control_engine, secret_box=secret_box
    )
    connection_id = TeamOAuthConnectionService.google_connection_id("google-user-1")
    service.connect_google_account(
        org_id=org_id,
        user_id=owner_id,
        external_subject="google-user-1",
        refresh_token="refresh-secret-1",
        external_email="reporter@example.com",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    pid = browser.post("/api/projects", json={"name": "Team Sheets Export"}).json()[
        "id"
    ]
    imported = browser.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,status\nAda,ready\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])

    action = _google_export_action(sheet_id=sheet_id, connection_id=connection_id)
    response = _confirm_export(browser, pid, action)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"] is not None

    workspace = app.state.workspace
    assert (
        Worker(
            workspace.queue, workspace.registry, worker_id="team-sheets-test"
        ).run_once()
        is True
    )

    project = workspace.get(pid)
    stored = ReceiptStore(project).find_by_id(body["receipt_id"])
    assert stored is not None
    receipt = stored.parsed()
    # The whole point: no connected_account_not_found, and the RIGHT
    # decrypted refresh token reached the (fake) client.
    assert receipt.status == "completed", receipt
    assert receipt.outputs[0].ref["spreadsheet_id"] == "team-wired-1"
    assert fake_client.calls[0]["connection"]["refresh_token"] == "refresh-secret-1"
    assert (
        fake_client.calls[0]["connection"]["external_email"] == "reporter@example.com"
    )


def test_unknown_connection_id_still_fails_closed_not_silently(tmp_path: Path) -> None:
    """No stored connection at all -> the SAME connected_account_not_found
    the hosted edition returns (tests/engine/test_export_google_sheets_action.py),
    proving the wiring doesn't turn into a rubber-stamp resolver that
    accepts anything."""
    config = _config(
        tmp_path,
        google_client_id="client-id",
        google_client_secret="client-secret",
    )
    app = create_team_app(config, google_sheets_client=FakeGoogleSheetsClient())
    browser = claim_server(app)

    pid = browser.post("/api/projects", json={"name": "No Connection"}).json()["id"]
    imported = browser.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,status\nAda,ready\n", "text/csv")},
    )
    sheet_id = int(imported.json()["sheet_id"])
    action = _google_export_action(
        sheet_id=sheet_id, connection_id="google_doesnotexist"
    )
    response = browser.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert response.status_code == 400, response.text
    body = response.json()
    workspace = app.state.workspace
    project = workspace.get(pid)
    assert body["errors"][0]["code"] == "connected_account_not_found"
    assert body["receipt_id"] is None and body["job_id"] is None
    assert (
        ReceiptStore(project).find_by_idempotency_key(action["idempotency_key"]) is None
    )


def test_revoked_connection_fails_not_found_before_any_checkpoint(
    tmp_path: Path,
) -> None:
    """A revoked oauth_connections row must never reach the export effect
    seam (bugfix, 2026-07-31): `TeamOAuthConnectionService.connection()` now
    filters `revoked_at IS NULL` in the query itself (mirroring an external
    composition's `connection_row(include_revoked=False)`), so the
    resolver returns None for a revoked connection and the action refuses
    connected_account_not_found BEFORE `_run_export_google_sheets` reserves
    an external-effect checkpoint. Previously the query returned the row
    regardless of revocation (only `refresh_token` attachment was gated),
    so the action passed the not_found gate, reserved a checkpoint, and
    only then failed on the missing refresh token -- leaving a 'reserved'
    checkpoint row with zero egress that would have falsely demanded
    operator reconciliation."""
    fake_client = FakeGoogleSheetsClient()
    config = _config(
        tmp_path,
        google_client_id="client-id",
        google_client_secret="client-secret",
    )
    app = create_team_app(config, google_sheets_client=fake_client)
    browser = claim_server(app)

    org_id = app.state.team_org_id
    with app.state.control_engine.connect() as cx:
        owner_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()

    secret_box = TeamSecretBox(config)
    service = TeamOAuthConnectionService(
        app.state.control_engine, secret_box=secret_box
    )
    connection_id = TeamOAuthConnectionService.google_connection_id("google-user-2")
    service.connect_google_account(
        org_id=org_id,
        user_id=owner_id,
        external_subject="google-user-2",
        refresh_token="refresh-secret-2",
        external_email="revoked@example.com",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    revoked = service.revoke_connection(
        org_id, "google", connection_id, user_id=owner_id
    )
    assert revoked is True

    pid = browser.post("/api/projects", json={"name": "Revoked Connection"}).json()[
        "id"
    ]
    imported = browser.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,status\nAda,ready\n", "text/csv")},
    )
    sheet_id = int(imported.json()["sheet_id"])
    action = _google_export_action(sheet_id=sheet_id, connection_id=connection_id)
    response = browser.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert response.status_code == 400, response.text
    body = response.json()

    workspace = app.state.workspace
    project = workspace.get(pid)
    assert body["errors"][0]["code"] == "connected_account_not_found"
    assert body["receipt_id"] is None and body["job_id"] is None
    assert (
        ReceiptStore(project).find_by_idempotency_key(action["idempotency_key"]) is None
    )
    # The load-bearing assertion: no external-effect checkpoint was ever
    # reserved for this action -- a revoked connection must fail BEFORE the
    # effect seam, not leave a reserved row an operator has to reconcile.
    assert EffectCheckpointStore(project.db).list_all() == []
    # And the fake client -- standing in for the real Google API -- was
    # never called at all: zero egress, not a failed egress.
    assert fake_client.calls == []


class TestResolverWiringIsUnconditional:
    """The 'make the wiring non-optional where the work happens' half of the
    fix: a team composition ALWAYS supplies a connected_account_resolver
    callable, even with no Google OAuth app configured at all -- only
    google_sheets_client depends on that config. A resolver that only
    sometimes exists is exactly the class of miss this bug was (team had a
    full OAuth flow and STILL never wired one)."""

    def test_resolver_present_even_without_a_configured_oauth_app(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)  # no google_client_id/secret at all
        assert not config.google_client_id
        app = create_team_app(config)

        workspace = app.state.workspace
        assert workspace.executor_deps_factory is not None
        deps = workspace.executor_deps_factory("unused-project-id", None)
        assert deps.connected_account_resolver is not None
        assert deps.connected_account_resolver("google", "anything") is None
        # The OTHER dependency correctly reflects the missing OAuth app --
        # this is what actually gates the run-time
        # google_sheets_client_unavailable error, not the resolver.
        assert deps.google_sheets_client is None

    def test_resolver_present_and_client_wired_when_oauth_app_is_configured(
        self, tmp_path: Path
    ) -> None:
        config = _config(
            tmp_path, google_client_id="client-id", google_client_secret="client-secret"
        )
        fake_client = FakeGoogleSheetsClient()
        app = create_team_app(config, google_sheets_client=fake_client)

        workspace = app.state.workspace
        deps = workspace.executor_deps_factory("unused-project-id", None)
        assert deps.connected_account_resolver is not None
        assert deps.google_sheets_client is fake_client


def test_bare_local_tier_has_no_resolver_and_the_action_reports_unavailable(
    tmp_path: Path,
) -> None:
    """The other half, in the same file as the team-side fix for a single
    before/after record (test_google_sheets_connected_account_hint.py covers
    this local-tier case in full depth): a bare `create_app(workspace)` --
    what `frisket <workspace-dir>` actually runs, no team wrapper at all --
    has no resolver, and the catalog says so instead of the action just
    failing at run time."""
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Local"}).json()["id"]
    catalog = client.get(f"/api/projects/{pid}/actions/v1/catalog")
    assert catalog.status_code == 200, catalog.text
    entry = next(
        a for a in catalog.json()["actions"] if a["kind"] == "export.google_sheets"
    )
    reason = entry["ui_hints"].get("unavailable_reason")
    assert reason and "team" in reason.lower()
