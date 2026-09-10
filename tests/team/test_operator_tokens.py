"""Operator-token mechanism: mint/hash/verify/rotate, the on-box
`frisket-control token mint` console, and the pre-claim setup-gate window."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.operability.control.cli import main as control_main
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.operator_service import (
    OPERATOR_ACTOR_EMAIL,
    OperatorTokenError,
    OperatorTokenService,
    mint_operator_token,
    resolve_control_database_url,
)
from frisket.team.schema import (
    OPERATOR_TOKEN_PREFIX,
    audit_log,
    memberships,
    operator_tokens,
    users,
)
from frisket.team.team_bootstrap import initialize_team_schema_and_org


def _app(tmp_path: Path, **overrides: Any):
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Ops Desk",
        magic_link_enabled=False,
    )
    values.update(overrides)
    return create_team_app(TeamConfig(**values))


def _engine(tmp_path: Path) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}", future=True)
    initialize_team_schema_and_org(engine, organization_name="Ops Desk")
    return engine


def test_mint_stores_only_the_hash_and_authenticate_round_trips(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    raw, token_id = mint_operator_token(engine, org_id=1, label="laptop")
    assert raw.startswith(OPERATOR_TOKEN_PREFIX)
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(operator_tokens).where(operator_tokens.c.id == token_id)
        ).one()
    assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in (row.token_hash, row.label)
    assert row.label == "laptop"
    assert row.revoked_at is None

    service = OperatorTokenService(engine, org_id=1)
    actor = service.authenticate(f"Bearer {raw}")
    assert actor is not None
    assert actor["auth"] == "operator"
    assert actor["email"] == OPERATOR_ACTOR_EMAIL
    assert actor["operator_token_id"] == token_id
    assert actor["operator_label"] == "laptop"
    assert service.authenticate(f"Bearer {OPERATOR_TOKEN_PREFIX}nope") is None
    assert service.authenticate("Bearer frisket_pat_something") is None
    assert service.authenticate("") is None
    with engine.connect() as cx:
        minted_audit = cx.execute(
            sa.select(sa.func.count())
            .select_from(audit_log)
            .where(audit_log.c.action == "operator_token_minted")
        ).scalar_one()
    assert minted_audit == 1


def test_operator_actor_never_gains_a_membership(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    raw, _ = mint_operator_token(engine, org_id=1)
    service = OperatorTokenService(engine, org_id=1)
    actor = service.authenticate(f"Bearer {raw}")
    assert actor is not None
    with engine.connect() as cx:
        membership_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(memberships)
            .where(memberships.c.user_id == int(actor["id"]))
        ).scalar_one()
        email = cx.execute(
            sa.select(users.c.email).where(users.c.id == int(actor["id"]))
        ).scalar_one()
    assert membership_count == 0
    assert email == OPERATOR_ACTOR_EMAIL


def test_rotate_invalidates_the_old_token_and_carries_the_label(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    raw, token_id = mint_operator_token(engine, org_id=1, label="laptop")
    service = OperatorTokenService(engine, org_id=1)
    new_raw, label = service.rotate(token_id=token_id)
    assert label == "laptop"
    assert new_raw != raw
    assert service.authenticate(f"Bearer {raw}") is None
    replacement = service.authenticate(f"Bearer {new_raw}")
    assert replacement is not None and replacement["operator_label"] == "laptop"
    with pytest.raises(OperatorTokenError):
        service.rotate(token_id=token_id)
    with engine.connect() as cx:
        rotated_audit = cx.execute(
            sa.select(audit_log.c.detail).where(
                audit_log.c.action == "operator_token_rotated"
            )
        ).scalar_one()
    assert rotated_audit.startswith(f"{token_id}->")


def test_control_cli_mints_a_usable_token_via_env_resolution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    engine = _engine(tmp_path)
    monkeypatch.setenv(
        "FRISKET_TEAM_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
    )
    assert control_main(["token", "mint", "--label", "initial"]) == 0
    raw = capsys.readouterr().out.strip()
    assert raw.startswith(OPERATOR_TOKEN_PREFIX)
    actor = OperatorTokenService(engine, org_id=1).authenticate(f"Bearer {raw}")
    assert actor is not None and actor["operator_label"] == "initial"


def test_control_cli_standalone_data_dir_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    engine = sa.create_engine(f"sqlite:///{data_dir / 'server.sqlite3'}", future=True)
    initialize_team_schema_and_org(engine, organization_name="Ops Desk")
    monkeypatch.delenv("FRISKET_TEAM_DATABASE_URL", raising=False)
    monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))
    assert resolve_control_database_url() == f"sqlite:///{data_dir / 'server.sqlite3'}"
    assert control_main(["token", "mint"]) == 0
    raw = capsys.readouterr().out.strip()
    assert OperatorTokenService(engine, org_id=1).authenticate(f"Bearer {raw}")


def test_control_cli_refuses_without_a_database(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("FRISKET_TEAM_DATABASE_URL", raising=False)
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "nowhere"))
    assert control_main(["token", "mint"]) == 2
    err = capsys.readouterr().err
    assert "no server control database" in err


def test_unclaimed_server_admits_only_the_operator_admin_api(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    raw, _ = mint_operator_token(app.state.control_engine, org_id=1, label="install")
    client = TestClient(app)
    bearer = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/admin/ping").status_code == 503
    ping = client.get("/api/admin/ping", headers=bearer)
    assert ping.status_code == 200, ping.text
    assert ping.json()["org"] == "Ops Desk"
    assert ping.json()["token_label"] == "install"
    # Only /api/admin/* passes pre-claim; org-owner power does not open the
    # rest of the unclaimed server.
    assert client.get("/api/me", headers=bearer).status_code == 503
    assert (
        client.get(
            "/api/admin/ping",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN_PREFIX}guess"},
        ).status_code
        == 503
    )
    added = client.post(
        "/api/admin/users", headers=bearer, json={"email": "first@example.com"}
    )
    assert added.status_code == 200, added.text
    assert added.json()["invite_link"].startswith("http://testserver/auth/callback")
