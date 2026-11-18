from __future__ import annotations

import io

import sqlalchemy as sa

from frisket.cli import owner
from frisket.team.identity_service import IdentityAuthService
from frisket.team.local_auth import claim_first_owner
from frisket.team.schema import sessions
from frisket.team.team_bootstrap import initialize_team_schema_and_organization
from frisket.team.uow import TeamUnitOfWork


def _claimed_database(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'control.sqlite3'}"
    engine = sa.create_engine(database_url, future=True)
    org_id = initialize_team_schema_and_organization(
        engine, organization_name="Frisket"
    )
    claim_first_owner(
        engine,
        org_id=org_id,
        claim_token="setup-code",
        expected_claim_token="setup-code",
        workspace_name="Investigations",
        owner_name="Owner",
        email="owner@example.test",
        password="the original long password",
    )
    return database_url, engine


def _sign_in_password(engine, *, email: str, password: str) -> None:
    with engine.connect() as cx:
        org_id = cx.exec_driver_sql("SELECT id FROM orgs").scalar_one()
    IdentityAuthService(lambda: TeamUnitOfWork(engine, org_id=org_id)).sign_in_password(
        email=email, password=password
    )


def test_owner_reset_reads_stdin_revokes_sessions_and_changes_password(
    tmp_path, monkeypatch, capsys
):
    database_url, engine = _claimed_database(tmp_path)
    _sign_in_password(
        engine,
        email="owner@example.test",
        password="the original long password",
    )
    with engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(sessions)).scalar_one()
            == 2
        )

    monkeypatch.setenv("FRISKET_TEAM_DATABASE_URL", database_url)
    monkeypatch.setattr(
        owner.sys,
        "stdin",
        io.StringIO("the replacement password\nthe replacement password\n"),
    )
    assert owner.owner_admin(["reset-password"]) == 0
    assert "owner@example.test" in capsys.readouterr().out
    with engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(sessions)).scalar_one()
            == 0
        )
    _sign_in_password(
        engine,
        email="owner@example.test",
        password="the replacement password",
    )


def test_owner_reset_rejects_mismatch_without_password_in_output(
    tmp_path, monkeypatch, capsys
):
    database_url, engine = _claimed_database(tmp_path)
    engine.dispose()
    monkeypatch.setenv("FRISKET_TEAM_DATABASE_URL", database_url)
    monkeypatch.setattr(
        owner.sys,
        "stdin",
        io.StringIO("first secret password\nsecond secret password\n"),
    )
    assert owner.owner_admin(["reset-password"]) == 1
    output = capsys.readouterr().err
    assert "confirmation does not match" in output
    assert "first secret" not in output
    assert "second secret" not in output


def test_owner_reset_finds_standalone_database_from_data_dir(tmp_path, monkeypatch):
    database_url, engine = _claimed_database(tmp_path)
    engine.dispose()
    source = tmp_path / "control.sqlite3"
    standalone = tmp_path / "server.sqlite3"
    source.rename(standalone)
    assert database_url.endswith("control.sqlite3")

    monkeypatch.delenv("FRISKET_TEAM_DATABASE_URL", raising=False)
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        owner.sys,
        "stdin",
        io.StringIO("the standalone replacement\nthe standalone replacement\n"),
    )
    assert owner.owner_admin(["reset-password"]) == 0
