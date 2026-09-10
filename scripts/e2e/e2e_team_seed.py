"""Seed a real single-org team admin session for Playwright storageState.

Two of the eight non-core team specs (admin-users, audit-log-viewer) navigate
straight to `/admin` without mocking `/api/me` themselves, so they need a REAL
authenticated session against the real single-org team backend
(`frisket.team.asgi:app`) already established before the browser loads the
page — the same schema/org `create_team_app` provisions on boot
(`frisket.team.team_bootstrap.initialize_team_schema_and_org`, always org id
1 for the single-org team edition).

This claims the fresh workspace's first owner and writes a session directly
into the sqlite DB `frisket.team.asgi:app` is already serving from
(`FRISKET_TEAM_DATABASE_URL`), then emits a Playwright storageState JSON
carrying the resulting `frisket_session` cookie. Specs that mock `/api/me`
themselves (page.route interception happens before the request reaches the
network) simply ignore this cookie.

Usage:
    uv run python scripts/e2e/e2e_team_seed.py --storage-state <path> --base-url <url>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import sqlalchemy as sa

from frisket.team.config import team_config_from_env
from frisket.team.identity_store import IdentityStore
from frisket.team.local_auth import claim_first_owner, owner_count

SESSION_COOKIE = "frisket_session"
SESSION_LIFETIME = timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-state", required=True, help="output storageState path"
    )
    parser.add_argument("--base-url", required=True, help="the team backend's base URL")
    args = parser.parse_args(argv)

    config = team_config_from_env()
    if not config.admin_emails:
        print(
            "FRISKET_ADMIN_EMAILS must name at least one admin for e2e seeding",
            file=sys.stderr,
        )
        return 1
    admin_email = sorted(config.admin_emails)[0]

    engine = sa.create_engine(config.database_url, future=True)
    if owner_count(engine, org_id=1) == 0:
        claim_first_owner(
            engine,
            org_id=1,
            claim_token="playwright-team-owner",
            expected_claim_token="playwright-team-owner",
            workspace_name=config.organization_name,
            owner_name="Playwright Owner",
            email=admin_email,
            password="playwright-team-owner-password",
        )
    now = datetime.now(UTC)
    expires_at = now + SESSION_LIFETIME
    with engine.begin() as cx:
        store = IdentityStore(cx, provision_org_id=1)
        user = store.ensure_user_for_login(admin_email)
        token = store.create_session(
            user_id=int(user["id"]), created_at=now, expires_at=expires_at
        )

    host = urlsplit(args.base_url).hostname or "127.0.0.1"
    storage_state = {
        "cookies": [
            {
                "name": SESSION_COOKIE,
                "value": token,
                "domain": host,
                "path": "/",
                "expires": expires_at.timestamp(),
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    with open(args.storage_state, "w", encoding="utf-8") as fh:
        json.dump(storage_state, fh)
    print(f"seeded team admin session for {admin_email} -> {args.storage_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
