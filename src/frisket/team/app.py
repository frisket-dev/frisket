"""Open, single-organization team-server composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from frisket.contracts.http.api_tokens import (
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenList,
    ApiTokenRevokeResponse,
)
from frisket.contracts.http.instance_runtime import AuthMethods, OIDCAuthMethod
from frisket.contracts.http.endpoint_catalog import declare_endpoints
from frisket.contracts.http.project_collaboration import (
    ProjectMemberList,
    ProjectMemberRemove,
    ProjectMemberSet,
    SetProjectMemberRequest,
)
from frisket.contracts.http.project_config import (
    ProjectNetworkRequest,
    ProjectNetworkResponse,
)
from frisket.contracts.http.organization_operations import (
    OrganizationMediaProxyStatus,
)
from frisket.engine.executor import ExecutorDeps
from frisket.engine.jobs import WorkerPorts, open_queue
from frisket.server.app import create_app
from frisket.server.route_errors import http_error_responses
from frisket.server.static_serving import mount_spa_static, resolve_static_dir
from frisket.team.auth_runtime import (
    production_oidc_exchange,
    smtp_sender,
    validate_provider_credential,
)
from frisket.team.browser_auth_routes import register_browser_auth_routes
from frisket.team.admin_browser_routes import register_admin_browser_routes
from frisket.team.admin_browser_service import AdminMembershipService
from frisket.team.config import TeamConfig, team_config_from_env
from frisket.team.control_plane import TeamOrgKeyCredentialPort, org_provider_keys
from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.admin_routes import register_admin_api_routes
from frisket.team.enforcement import protect_core_app
from frisket.team.identity_service import IdentityAuthService
from frisket.team.invite_routes import register_invite_routes
from frisket.team.invite_service import InviteService
from frisket.team.operator_service import OperatorTokenService
from frisket.team.oauth_connections import (
    GoogleConnectionExchange,
    TeamOAuthConnectionService,
    register_oauth_connection_routes,
)
from frisket.team.observability_routes import register_observability_routes
from frisket.team.operational_routes import register_operational_routes
from frisket.team.policy import install_outer_policy
from frisket.team.schema import (
    api_tokens,
    audit_log,
    memberships,
    orgs,
    project_creation_intents,
    project_invites,
    project_roles,
    projects,
    users,
)
from frisket.team.uow import TeamUnitOfWork
from frisket.team.team_bootstrap import (
    initialize_team_schema_and_org,
    preflight_team_schema,
)
from frisket.team.secret_box import TeamSecretBox
from frisket.team.session_routes import register_session_routes
from frisket.team.session_service import (
    IdentitySessionService,
    SessionForbidden,
    SessionRequired,
)
from frisket.team.local_auth import (
    InvalidCredentials,
    ResetTokenInvalid,
    SetupError,
    SetupUnavailable,
    claim_first_owner,
    owner_count,
    password_reset_is_live,
    redeem_password_reset,
)
from frisket.team.readiness import deployment_readiness
from frisket.team.setup_gate import SetupGate

# The outer team app (this module) has no request-error logging middleware --
# that's only installed on the mounted core app (server/app.py's
# install_fastapi_logging). Handled exceptions on THIS app's own routes
# (/auth/*) must log themselves explicitly or the cause never reaches logs at
# all.
SESSION_COOKIE = "frisket_session"

_LOG = logging.getLogger("frisket.team.app")

# The team app's own control-plane locator (read by team_config_from_env into
# TeamConfig.database_url) versus the worker's audit-writer locator (read
# directly by frisket.jobs.model_pull, never through TeamConfig).
# FRISKET_TEAM_DATABASE_URL is the ONE authoritative control-plane locator:
# team_config_from_env reads ONLY it (no Python-side derivation), and Compose
# derives the worker's FRISKET_DATABASE_URL default FROM
# FRISKET_TEAM_DATABASE_URL (nested interpolation), closing the common case.
# Divergent values are UNSUPPORTED: the split is not merely "app control
# plane vs worker audit database" — the worker also resolves org provider
# keys through FRISKET_DATABASE_URL (jobs/ports.py), so splitting the two
# scatters one logical control plane across two databases with no supported
# reconciliation. The warning below (equality validation, never a crash)
# names both variables whenever both are set and differ; operators should
# converge on FRISKET_TEAM_DATABASE_URL (under Compose the worker var
# derives from it; for a NON-Compose launch set BOTH variables to that same
# URL, since the app reads only the team var).
_TEAM_DATABASE_URL_ENV = "FRISKET_TEAM_DATABASE_URL"
_WORKER_DATABASE_URL_ENV = "FRISKET_DATABASE_URL"


def _redact_dsn(value: str) -> str:
    """Best-effort credential-free rendering of a database locator for logs."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[unparseable]"
    if not parsed.scheme or not parsed.hostname:
        return "[unparseable]"
    netloc = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        # A nonnumeric port makes `parsed.port` raise; the locator is
        # already invalid, but "best-effort, never crash" means best-effort.
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _warn_if_control_plane_locator_diverges() -> None:
    team_url = os.environ.get(_TEAM_DATABASE_URL_ENV)
    worker_url = os.environ.get(_WORKER_DATABASE_URL_ENV)
    if team_url and worker_url and team_url != worker_url:
        _LOG.warning(
            "%s (%s) and %s (%s) are both set and point at DIFFERENT "
            "databases. Splitting these is UNSUPPORTED: the worker resolves "
            "org provider keys AND writes audits through the second "
            "variable, so a split scatters one logical control plane across "
            "two databases. Most likely one of them carries a stale "
            "credential/host from a half-done rotation -- %s is the "
            "authoritative control-plane locator: under Compose %s derives "
            "from it automatically, and for a non-Compose launch set BOTH to "
            "that same URL (see .env.example, docker-compose.yml).",
            _TEAM_DATABASE_URL_ENV,
            _redact_dsn(team_url),
            _WORKER_DATABASE_URL_ENV,
            _redact_dsn(worker_url),
            _TEAM_DATABASE_URL_ENV,
            _WORKER_DATABASE_URL_ENV,
            extra={
                "event": "control_plane_database_url_diverged",
                "team_database_url_env": _TEAM_DATABASE_URL_ENV,
                "worker_database_url_env": _WORKER_DATABASE_URL_ENV,
            },
        )


class _Body(BaseModel):
    email: str = ""
    name: str = ""
    role: str = ""
    sensitive: bool = False


class _DeleteBody(BaseModel):
    confirm_name: str


class _OrgNetworkBody(BaseModel):
    # "on" | "off" | None (clear back to the built-in "on").
    network_default: str | None = None


class _AttemptLimiter:
    """Small bounded process-local rolling window for authentication attempts."""

    def __init__(
        self,
        *,
        limit: int = 8,
        window_seconds: float = 60.0,
        max_keys: int = 4_096,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        with self._lock:
            cutoff = time.monotonic() - self.window_seconds
            attempts = self._attempts.get(key)
            if attempts is None:
                if len(self._attempts) >= self.max_keys:
                    # Dicts preserve insertion order. Evicting the oldest key
                    # keeps arbitrary submitted identities from growing this
                    # intentionally in-memory limiter without bound.
                    self._attempts.pop(next(iter(self._attempts)))
                attempts = deque()
                self._attempts[key] = attempts
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            return len(attempts) < self.limit

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, deque()).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _emit_setup_code(code: str) -> None:
    import sys

    # Must not pass structured logging: it intentionally redacts token values.
    print(f"FRISKET_SETUP_CODE {code} /setup", file=sys.stderr, flush=True)


_LOCAL_AUTH_PAGE_STYLES = """
:root {
  color-scheme: light;
  font-family: "IBM Plex Sans", Inter, ui-sans-serif, system-ui, -apple-system,
    BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #e9e5dd;
  color: #211f1b;
}
* { box-sizing: border-box; }
body {
  min-height: 100vh;
  min-height: 100svh;
  margin: 0;
  display: grid;
  place-items: center;
  padding: 32px 20px;
  background:
    radial-gradient(circle at 12% 12%, rgba(124, 92, 230, 0.12), transparent 34%),
    radial-gradient(circle at 86% 88%, rgba(14, 147, 132, 0.10), transparent 30%),
    #e9e5dd;
}
.shell {
  width: min(960px, 100%);
  display: grid;
  grid-template-columns: minmax(240px, 0.78fr) minmax(0, 1.22fr);
  overflow: hidden;
  border: 1px solid #d9d5cc;
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 34px 70px -30px rgba(30, 27, 22, 0.45);
}
.context {
  padding: 44px 38px;
  background: #faf8f4;
  border-right: 1px solid #eceae3;
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 52px;
  color: #3a41c4;
  font-weight: 750;
  letter-spacing: -0.02em;
}
.brand-mark {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: #4b53d9;
  color: #ffffff;
  font: 700 15px/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
}
.eyebrow {
  margin: 0 0 12px;
  color: #4b53d9;
  font-size: 12px;
  font-weight: 750;
  letter-spacing: 0.11em;
  text-transform: uppercase;
}
h1 {
  max-width: 13ch;
  margin: 0;
  font-size: clamp(30px, 4vw, 44px);
  line-height: 1.04;
  letter-spacing: -0.045em;
}
.lede {
  margin: 20px 0 0;
  color: #57534a;
  font-size: 15px;
  line-height: 1.65;
}
.expectation {
  margin-top: 30px;
  padding-top: 22px;
  border-top: 1px solid #e3dfd6;
  color: #57534a;
  font-size: 13px;
  line-height: 1.55;
}
.panel { padding: 44px 48px 42px; }
.panel-heading {
  margin: 0 0 24px;
  font-size: 21px;
  line-height: 1.25;
  letter-spacing: -0.025em;
}
.notice {
  margin: 0 0 28px;
  padding: 16px 17px;
  border: 1px solid #dddff8;
  border-radius: 10px;
  background: #f4f2fb;
  color: #4d4675;
  font-size: 13px;
  line-height: 1.55;
}
.notice strong { color: #332d59; }
.notice code {
  padding: 2px 5px;
  border-radius: 4px;
  background: #e7e4f5;
  color: #3a3370;
  font: 600 0.88em/1.4 ui-monospace, "SFMono-Regular", Consolas, monospace;
}
form { display: grid; gap: 18px; }
.field { display: grid; gap: 7px; }
.field > span {
  color: #38342e;
  font-size: 13px;
  font-weight: 650;
}
input {
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
  border: 1px solid #d9d5cc;
  border-radius: 7px;
  background: #ffffff;
  color: #211f1b;
  font: inherit;
}
input:hover { border-color: #b9b4a9; }
input:focus-visible {
  border-color: #4b53d9;
  outline: 3px solid #ecedfb;
  outline-offset: 1px;
}
.hint {
  margin: 0;
  color: #746f65;
  font-size: 12px;
  line-height: 1.45;
}
.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
button {
  min-height: 46px;
  margin-top: 4px;
  padding: 11px 18px;
  border: 1px solid #4b53d9;
  border-radius: 7px;
  background: #4b53d9;
  color: #ffffff;
  font: inherit;
  font-size: 14px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.14);
}
button:hover { background: #3a41c4; border-color: #3a41c4; }
button:focus-visible { outline: 3px solid #cfd2ff; outline-offset: 2px; }
.after-form {
  margin: 22px 0 0;
  color: #746f65;
  font-size: 12px;
  line-height: 1.55;
  text-align: center;
}
@media (max-width: 760px) {
  body { padding: 0; place-items: stretch; background: #ffffff; }
  .shell { grid-template-columns: 1fr; border: 0; border-radius: 0; box-shadow: none; }
  .context { padding: 30px 24px; border-right: 0; border-bottom: 1px solid #eceae3; }
  .brand { margin-bottom: 34px; }
  h1 { max-width: none; }
  .panel { padding: 32px 24px 40px; }
}
@media (max-width: 480px) {
  .field-grid { grid-template-columns: 1fr; }
}
"""


def _local_auth_page(
    *,
    title: str,
    eyebrow: str,
    heading: str,
    lede: str,
    expectation: str,
    panel_heading: str,
    trusted_panel_html: str,
) -> str:
    """Render the two static local-auth pages without scripts or remote assets."""
    title = escape(title)
    eyebrow = escape(eyebrow)
    heading = escape(heading)
    lede = escape(lede)
    expectation = escape(expectation)
    panel_heading = escape(panel_heading)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_LOCAL_AUTH_PAGE_STYLES}</style>
</head>
<body>
  <main class="shell">
    <section class="context" aria-labelledby="page-title">
      <div class="brand"><span class="brand-mark" aria-hidden="true">F</span>Frisket</div>
      <p class="eyebrow">{eyebrow}</p>
      <h1 id="page-title">{heading}</h1>
      <p class="lede">{lede}</p>
      <p class="expectation">{expectation}</p>
    </section>
    <section class="panel" aria-labelledby="form-title">
      <h2 class="panel-heading" id="form-title">{panel_heading}</h2>
      {trusted_panel_html}
    </section>
  </main>
</body>
</html>"""


def _project_intent_slug(name: str) -> str:
    base = (
        "".join(
            char if char.isalnum() or char in "-_" else "-"
            for char in name.lower().strip()
        )[:48].strip("-")
        or "project"
    )
    return f"{base}-{secrets.token_hex(6)}"


def _valid_project_bundle(bundle, *, slug: str) -> bool:
    try:
        manifest = json.loads((bundle / "manifest.json").read_text())
        if (
            manifest.get("format") != "frisket-bundle"
            or manifest.get("project_id") != slug
        ):
            return False
        with sqlite3.connect(bundle / "project.db") as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            meta_table = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if meta_table != (1,):
                return False
            return (
                db.execute(
                    "SELECT value FROM meta WHERE key='format_version'"
                ).fetchone()
                is not None
            )
    except (OSError, ValueError, sqlite3.DatabaseError):
        return False


def _sync_project_network_policy(
    cx: sa.Connection, workspace: Any, *, org_id: int, slug: str
) -> None:
    """Materialize the control-plane network settings into the bundle
    (following the `sensitive` sync precedent): the gate reads
    policy from the Project bundle at validate/dispatch/replay time, where no
    control-plane handle exists — so both the project's mode and the org
    default live there, reconciled on drift."""
    org_default = cx.execute(
        sa.select(orgs.c.network_default).where(orgs.c.id == org_id)
    ).scalar_one_or_none()
    mode = (
        cx.execute(
            sa.select(projects.c.network).where(
                projects.c.org_id == org_id, projects.c.slug == slug
            )
        ).scalar_one_or_none()
        or "inherit"
    )
    try:
        project = workspace.get(slug)
    except (HTTPException, ValueError):
        # Bundle not materialized yet (pending creation intent): intent
        # completion calls this sync again once the bundle exists.
        return
    policy = project.network_policy()
    if policy["mode"] != mode or policy["org_default"] != org_default:
        project.set_network_policy(
            mode=mode,
            org_default=org_default,
            clear_org_default=org_default is None,
        )


def _complete_project_intent(
    engine: sa.Engine, workspace: Any, *, slug: str
) -> dict[str, Any]:
    with locked_transaction(engine, lock_scope=("project-recovery", slug)) as cx:
        intent = cx.execute(
            sa.select(project_creation_intents).where(
                project_creation_intents.c.slug == slug
            )
        ).first()
        if intent is None:
            existing = cx.execute(
                sa.select(projects.c.name, projects.c.sensitive).where(
                    projects.c.slug == slug
                )
            ).first()
            if existing is None:
                raise LookupError(f"project creation intent disappeared: {slug}")
            return {
                "id": slug,
                "name": existing.name,
                "description": "",
                "sensitive": bool(existing.sensitive),
            }
        requested_sensitive = bool(intent.sensitive)
        bundle = workspace.root / f"{slug}.frisket"
        if bundle.exists() and not _valid_project_bundle(bundle, slug=slug):
            quarantine = workspace.root / ".quarantine"
            quarantine.mkdir(exist_ok=True)
            bundle.rename(quarantine / f"{slug}-{secrets.token_hex(6)}.frisket")
        if not _valid_project_bundle(bundle, slug=slug):
            made = workspace.create(
                str(intent.name),
                project_id=slug,
                sensitive=requested_sensitive,
            )
        else:
            project = workspace.get(slug)
            if project.project_metadata()["sensitive"] != requested_sensitive:
                project.set_project_sensitivity(requested_sensitive)
            made = project.project_metadata()
        exists = cx.execute(
            sa.select(projects.c.id).where(
                projects.c.org_id == intent.org_id, projects.c.slug == slug
            )
        ).scalar_one_or_none()
        if exists is None:
            cx.execute(
                projects.insert().values(
                    org_id=intent.org_id,
                    storage_org_id=intent.org_id,
                    slug=slug,
                    name=intent.name,
                    sensitive=requested_sensitive,
                )
            )
        else:
            cx.execute(
                projects.update()
                .where(projects.c.id == exists)
                .values(sensitive=requested_sensitive)
            )
        _sync_project_network_policy(
            cx, workspace, org_id=int(intent.org_id), slug=slug
        )
        atomic_upsert(
            cx,
            table=project_roles,
            values={
                "org_id": intent.org_id,
                "slug": slug,
                "user_id": intent.creator_user_id,
                "role": "owner",
            },
            conflict_columns=("org_id", "slug", "user_id"),
            update_values={"role": "owner"},
        )
        audit_detail = f"{slug}:sensitive=true" if requested_sensitive else str(slug)
        audited = cx.execute(
            sa.select(audit_log.c.id).where(
                audit_log.c.action == "project_created",
                audit_log.c.detail == audit_detail,
            )
        ).scalar_one_or_none()
        if audited is None:
            cx.execute(
                audit_log.insert().values(
                    user_id=intent.creator_user_id,
                    org_id=intent.org_id,
                    action="project_created",
                    detail=audit_detail,
                )
            )
        cx.execute(
            project_creation_intents.delete().where(
                project_creation_intents.c.slug == slug
            )
        )
        return made


class TeamProjectAccess:
    ROLES = ("viewer", "reviewer", "editor", "owner")

    def __init__(self, engine: sa.Engine, org_id: int):
        self.engine, self.org_id = engine, org_id

    def resolve_project_for_user_id(
        self,
        user_id: int,
        slug: str,
        *,
        preferred_org_id: int | None = None,
        scope_org_id: int | None = None,
    ) -> dict[str, Any] | None:
        if scope_org_id not in (None, self.org_id):
            return None
        with self.engine.connect() as cx:
            exists = cx.execute(
                sa.select(projects.c.id).where(
                    projects.c.org_id == self.org_id, projects.c.slug == slug
                )
            ).scalar_one_or_none()
        return (
            None
            if exists is None
            else {"org_id": self.org_id, "storage_org_id": self.org_id, "slug": slug}
        )

    def _role(self, cx: sa.Connection, user_id: int, slug: str) -> str | None:
        explicit = cx.execute(
            sa.select(project_roles.c.role).where(
                project_roles.c.org_id == self.org_id,
                project_roles.c.slug == slug,
                project_roles.c.user_id == user_id,
            )
        ).scalar_one_or_none()
        if explicit:
            return str(explicit)
        membership = cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.org_id == self.org_id, memberships.c.user_id == user_id
            )
        ).scalar_one_or_none()
        return "owner" if membership == "owner" else None

    def can_on_project(self, org_id: int, slug: str, user_id: int, need: str) -> bool:
        if org_id != self.org_id or need not in self.ROLES:
            return False
        with self.engine.connect() as cx:
            role = self._role(cx, user_id, slug)
        return role in self.ROLES and self.ROLES.index(role) >= self.ROLES.index(need)


def _set_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=30 * 86400,
    )


def create_team_app(
    config: TeamConfig,
    *,
    send_magic_email: Callable[[str, str], Awaitable[bool]] | None = None,
    oidc_exchange: Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
    | None = None,
    google_connection_exchange: GoogleConnectionExchange | None = None,
    validate_provider_key: Callable[[str, str], Awaitable[bool]] | None = None,
    product_telemetry_destination: Any | None = None,
    # Test seam only (mirrors google_connection_exchange/validate_provider_key
    # above): production always builds a real GoogleSheetsClient from
    # config.google_client_id/secret below. A test injects a fake here to
    # exercise the export.google_sheets wiring without a live Google token
    # endpoint (see tests/team/test_google_sheets_executor_wiring.py).
    google_sheets_client: Any | None = None,
) -> FastAPI:
    from frisket.server import provider_config

    # Every team composition enqueues every queued job kind (action runs
    # included, not just model pulls) into this queue (see the
    # `queue_database_url` open_queue() call below), and a bare `frisket
    # worker` never falls back to the control-plane database -- it falls
    # back to a workspace-local `.queue.db` sqlite file instead. An
    # unconfigured run-queue locator is therefore a broken topology for
    # EVERY queued job kind, pull flag or not: the app would enqueue
    # somewhere no worker is guaranteed to drain, so the constructor
    # requires the locator unconditionally, same as
    # `create_team_app_from_env` (the production ASGI entrypoint).
    if config.run_queue_database_url is None:
        raise ValueError(
            "FRISKET_RUN_QUEUE_DATABASE_URL must be set for a team "
            "deployment: the app enqueues every queued job kind (action "
            "runs included) into this queue, and without an explicit shared "
            "locator the worker fleet falls back to a different database "
            "and never drains them. Set it to the same value on the app "
            "and every worker (docker-compose.yml wires this by default)."
        )
    engine = sa.create_engine(config.database_url, future=True)
    preflight_team_schema(engine)
    config.validate(
        magic_transport_injected=send_magic_email is not None,
        oidc_exchange_injected=oidc_exchange is not None,
    )
    _warn_if_control_plane_locator_diverges()
    # Artifact pulling is a structural worker capability. Local model
    # downloads remain gated by the selected endpoint's pull_enabled fact.
    model_pull_enabled = True
    if (
        send_magic_email is None
        and config.magic_link_enabled
        and config.smtp_host
        and config.smtp_from
    ):
        send_magic_email = smtp_sender(config)
    if oidc_exchange is None and config.oidc_providers:
        oidc_exchange = production_oidc_exchange(config)
    if validate_provider_key is None:
        validate_provider_key = validate_provider_credential
    org_id = initialize_team_schema_and_org(
        engine, organization_name=config.organization_name
    )
    secret_box = TeamSecretBox(config)
    provider_config.configure_validation_token_secret(
        secret_box.validation_signing_secret
    )
    # Built here (before `core = create_app(...)` below) so the resolver
    # closure can be handed to it via `executor_deps_factory=` — the SAME
    # service instance `register_oauth_connection_routes` uses further down
    # for the browser-facing connect/list/revoke routes; both are stateless
    # wrappers over `engine`/`secret_box`, so building it once here and
    # reusing it there is just avoiding a second identical instance, not a
    # correctness requirement.
    oauth_connection_service = TeamOAuthConnectionService(engine, secret_box=secret_box)
    # Construct a real GoogleSheetsClient only when an operator has configured
    # a Google OAuth app; leave it absent otherwise. `export.google_sheets`
    # then fails closed with `google_sheets_client_unavailable` at run time
    # instead of a resolver that can never do anything, AND the catalog
    # pre-flight hint (server/action_catalog_hints.py
    # `_apply_connected_account_hints`) can tell a genuinely misconfigured
    # team deployment apart from a fresh one with no OAuth app yet.
    if (
        google_sheets_client is None
        and config.google_client_id
        and config.google_client_secret
    ):
        from frisket.ops.integrations.google_sheets import GoogleSheetsClient

        google_sheets_client = GoogleSheetsClient(
            client_id=config.google_client_id,
            client_secret=config.google_client_secret,
        )

    def _connected_account_resolver(
        provider: str, connection_id: str
    ) -> dict[str, Any] | None:
        if provider.strip().lower() != "google":
            return None
        return oauth_connection_service.connection(
            org_id, "google", connection_id, include_refresh_token=True
        )

    def _team_executor_deps_factory(
        _project_id: str, _request: Request
    ) -> ExecutorDeps:
        # This composition is single-org (org_id fixed above at server
        # startup, not per-request — see the run-queue/org_id comments
        # around `core = create_app(...)` below), so the resolver closes
        # over it directly; project_id/request are unused but required by
        # the `executor_deps_factory` contract (server/app.py).
        from decimal import Decimal
        from frisket.execution.consent_coverage import ConsentCoverage
        from frisket.engine.store.execution_routes import instance_principal

        coverage = None
        if _request is not None:
            user = require_user(_request)
            project = core.state.workspace.get(_project_id)
            with engine.connect() as connection:
                threshold = connection.execute(
                    sa.select(users.c.cost_preapproval_usd).where(
                        users.c.id == user["id"]
                    )
                ).scalar_one()
            coverage = ConsentCoverage(
                f"{instance_principal(project)}:user:{user['id']}",
                Decimal("0") if threshold is None else Decimal(str(threshold)),
            )
        return ExecutorDeps(
            consent_coverage=coverage,
            connected_account_resolver=_connected_account_resolver,
            google_sheets_client=google_sheets_client,
        )

    def uow_factory() -> TeamUnitOfWork:
        return TeamUnitOfWork(engine, org_id=org_id)

    auth = IdentityAuthService(uow_factory)
    access = TeamProjectAccess(engine, org_id)

    def resolve_token(token: str) -> dict[str, Any] | None:
        if not token.startswith("frisket_pat_"):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with engine.begin() as cx:
            row = cx.execute(
                sa.select(
                    api_tokens.c.id,
                    api_tokens.c.user_id,
                    api_tokens.c.org_id,
                    users.c.email,
                )
                .join(users, users.c.id == api_tokens.c.user_id)
                .join(
                    memberships,
                    sa.and_(
                        memberships.c.user_id == api_tokens.c.user_id,
                        memberships.c.org_id == api_tokens.c.org_id,
                    ),
                )
                .where(
                    api_tokens.c.token_hash == digest,
                    api_tokens.c.org_id == org_id,
                    api_tokens.c.revoked_at.is_(None),
                )
            ).first()
        return (
            None
            if row is None
            else {
                "id": row.user_id,
                "email": row.email,
                "org_id": row.org_id,
                "auth": "pat",
                "pat_id": row.id,
            }
        )

    class _TokenService:
        @staticmethod
        def resolve_user(token: str) -> dict[str, Any] | None:
            return resolve_token(token)

    identity_session = IdentitySessionService(
        uow_factory,
        token_service=_TokenService(),
        is_org_owner=lambda user_id, oid: (
            _membership_role(engine, user_id, oid) == "owner"
        ),
    )
    operator_service = OperatorTokenService(engine, org_id=org_id)

    def resolve_request(request: Request) -> dict[str, Any] | None:
        return identity_session.resolve_user(
            authorization=request.headers.get("authorization", ""),
            session_token=request.cookies.get(SESSION_COOKIE, ""),
        )

    def require_user(
        request: Request, *, browser: bool = False, admin: bool = False
    ) -> dict[str, Any]:
        authorization = request.headers.get("authorization", "")
        session_token = request.cookies.get(SESSION_COOKIE, "")
        if admin:
            # The operator bearer channel (frisket.team.operator_service) is
            # admin-only authority: it never satisfies plain session/member
            # requirements, and session auth never satisfies it in reverse.
            operator = operator_service.authenticate(authorization)
            if operator is not None:
                return operator
        try:
            if admin:
                return identity_session.require_admin(
                    authorization=authorization,
                    session_token=session_token,
                )
            if browser:
                return identity_session.require_browser_user(
                    authorization=authorization,
                    session_token=session_token,
                )
            return identity_session.require_user(
                authorization=authorization,
                session_token=session_token,
            )
        except SessionRequired as exc:
            raise HTTPException(401, str(exc)) from exc
        except SessionForbidden as exc:
            raise HTTPException(403, str(exc)) from exc

    def require_org_member(
        request: Request, *, browser: bool = False
    ) -> dict[str, Any]:
        user = require_user(request, browser=browser)
        if _membership_role(engine, int(user["id"]), org_id) is None:
            raise HTTPException(403, "organization membership required")
        return user

    # A configured run-queue locator must be the SAME queue the standalone
    # self-host worker polls (`frisket worker --database-url ...` /
    # FRISKET_RUN_QUEUE_DATABASE_URL), or every enqueued job (OCR,
    # transcribe, recipes, digests, watches, model pulls) sits forever
    # unclaimed. `hosted=False` always: the open team server -- single org,
    # workspace root IS the storage directory, no claimed ProjectStorageKey
    # routing -- is never the multi-tenant hosted posture an external
    # composition provides, even when its run-queue happens to be Postgres.
    # `config.run_queue_database_url`
    # is guaranteed non-None here (the unconditional check at the top of
    # this function raises otherwise), so an implicit workspace-local
    # `.queue.db` fallback is never reached -- an implicit queue location is
    # the exact silent-topology bug class this guards against.
    core = create_app(
        config.data_dir,
        queue=open_queue(database_url=config.run_queue_database_url, hosted=False),
        control_database_url=config.database_url,
        queue_payload_extra={"org_id": org_id},
        executor_deps_factory=_team_executor_deps_factory,
        provider_keys_resolver=lambda: org_provider_keys(
            engine, org_id=org_id, decryptor=secret_box.decrypt
        ),
        worker_ports=WorkerPorts(
            credential_port=TeamOrgKeyCredentialPort(secret_box.decrypt)
        ),
        require_explicit_provider_keys=True,
        serve_spa=False,
        enable_provider_config=False,
        product_telemetry_destination=product_telemetry_destination,
        edition="team",
        # Team's own capability fact is env-only (never the local tier's
        # workspace-file toggle, which `enable_provider_config=False` above
        # already excludes from this composition entirely) -- explicit here
        # rather than relying on create_app's own
        # enable_provider_config-gated default. Reuses the SAME predicate
        # value already evaluated above (the env can't change mid-startup).
        model_pull_enabled=model_pull_enabled,
        auth_methods=AuthMethods(
            password=True,
            magic_link=config.magic_link_enabled and send_magic_email is not None,
            oidc=[
                OIDCAuthMethod(id=provider, label=provider.replace("-", " ").title())
                for provider in config.oidc_providers
            ],
        ),
    )
    with engine.connect() as cx:
        pending_slugs = list(
            cx.execute(
                sa.select(project_creation_intents.c.slug).where(
                    project_creation_intents.c.org_id == org_id
                )
            ).scalars()
        )
    for pending_slug in pending_slugs:
        _complete_project_intent(engine, core.state.workspace, slug=pending_slug)

    def current_owner_emails() -> set[str]:
        with engine.connect() as cx:
            return {
                str(row.email)
                for row in cx.execute(
                    sa.select(users.c.email)
                    .join(memberships, memberships.c.user_id == users.c.id)
                    .where(
                        memberships.c.org_id == org_id,
                        memberships.c.role == "owner",
                    )
                )
            }

    protect_core_app(
        app=core,
        resolve_user=resolve_request,
        admin_emails=current_owner_emails,
        project_access=access,
        org_role=lambda user_id, oid: _membership_role(engine, user_id, oid),
    )
    app = FastAPI(title="frisket team", version="0.1.0")
    app.state.control_engine = engine
    app.state.team_org_id = org_id
    app.state.workspace = core.state.workspace
    app.state.product_telemetry = core.state.product_telemetry
    # Mounted ASGI applications do not receive the parent application's
    # lifespan events. Mirror the core preview registry onto the outer app and
    # join it here; shutdown is idempotent, so this stays safe if a future ASGI
    # stack also elects to run the mounted core's handler.
    action_preview_job_registry = core.state.action_preview_job_registry
    app.state.action_preview_job_registry = action_preview_job_registry

    async def _shutdown_action_preview_jobs() -> None:
        await asyncio.to_thread(action_preview_job_registry.shutdown)

    app.router.add_event_handler("shutdown", _shutdown_action_preview_jobs)
    app.state.identity_session_service = identity_session
    app.state.setup_claim_token = None
    if owner_count(engine, org_id=org_id) == 0:
        app.state.setup_claim_token = secrets.token_urlsafe(32)
        _emit_setup_code(app.state.setup_claim_token)

    @app.get("/api/ready")
    def server_readiness() -> Response:
        report = deployment_readiness(
            engine=engine,
            queue=core.state.workspace.queue,
            runtime_state=getattr(app.state, "standalone_runtime", None),
        )
        return JSONResponse(report, status_code=200 if report["ok"] else 503)

    @app.middleware("http")
    async def no_store_setup_and_local_auth(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/setup" or request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    setup_attempts = _AttemptLimiter()

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page() -> Response:
        if (
            app.state.setup_claim_token is None
            or owner_count(engine, org_id=org_id) > 0
        ):
            app.state.setup_claim_token = None
            raise HTTPException(404, "setup is unavailable")
        return _no_store(
            HTMLResponse(
                _local_auth_page(
                    title="Set up Frisket",
                    eyebrow="First-run setup",
                    heading="Make this Frisket yours.",
                    lede=(
                        "Create the first owner account and give this workspace "
                        "a name. This is the only time the setup code is used."
                    ),
                    expectation=(
                        "After setup, you will be signed in automatically and "
                        "this setup page will close. Future visits use the "
                        "regular sign-in page."
                    ),
                    panel_heading="Create the first owner",
                    trusted_panel_html="""
      <div class="notice" id="setup-code-help">
        <strong>Get the code from the Frisket app's current startup logs.</strong>
        Find the line beginning <code>FRISKET_SETUP_CODE</code> and copy only the
        code that follows it. Frisket never displays the secret code in this page.
        If the app restarted, an older pre-claim code is stale; use the newest
        startup-log entry. If no current code is visible and setup is still open,
        restart the Frisket app service—not the database—to emit a new one.
      </div>
      <form method="post" action="/setup">
        <label class="field">
          <span>Setup code</span>
          <input name="claim_token" type="password" autocomplete="one-time-code"
            maxlength="512" aria-describedby="setup-code-help" required>
        </label>
        <label class="field">
          <span>Workspace name</span>
          <input name="workspace_name" autocomplete="organization" maxlength="200"
            placeholder="workspace">
        </label>
        <div class="field-grid">
          <label class="field">
            <span>Your name</span>
            <input name="owner_name" autocomplete="name" maxlength="200" required>
          </label>
          <label class="field">
            <span>Email</span>
            <input name="email" type="email" autocomplete="username" maxlength="320"
              inputmode="email" required>
          </label>
        </div>
        <label class="field">
          <span>Password</span>
          <input name="password" type="password" autocomplete="new-password"
            minlength="12" maxlength="1024" aria-describedby="password-help" required>
          <span class="hint" id="password-help">Use 12–1,024 characters. This becomes your regular Frisket password.</span>
        </label>
        <label class="field">
          <span>Confirm password</span>
          <input name="password_confirmation" type="password" autocomplete="new-password"
            minlength="12" maxlength="1024" required>
        </label>
        <button type="submit">Create workspace</button>
      </form>
      <p class="after-form">Keep the setup code private. Frisket support will never need it.</p>""",
                )
            )
        )

    @app.post("/setup")
    def setup(
        request: Request,
        claim_token: str = Form(""),
        workspace_name: str = Form(""),
        owner_name: str = Form(""),
        email: str = Form(""),
        password: str = Form(""),
        password_confirmation: str = Form(""),
    ) -> Response:
        # The workspace-name field has no `required` attribute (setup_page's
        # "workspace" placeholder is a hint, not a value) — an empty/
        # whitespace submission defaults here instead of failing
        # claim_first_owner's required-field check below.
        workspace_name = workspace_name.strip() or "workspace"
        if (
            app.state.setup_claim_token is None
            or owner_count(engine, org_id=org_id) > 0
        ):
            app.state.setup_claim_token = None
            raise HTTPException(
                404,
                "setup is unavailable",
                headers={"Cache-Control": "no-store"},
            )
        # The claim code carries 256 bits of entropy. Key its bounded limiter
        # by the submitted candidate so unrelated visitors behind one proxy
        # cannot lock out the operator without knowing the real code.
        attempt_key = (
            hashlib.sha256(claim_token.encode()).hexdigest()
            if len(claim_token) <= 512
            else "invalid-setup-code"
        )
        if not setup_attempts.allowed(attempt_key):
            raise HTTPException(
                429,
                "too many setup attempts",
                headers={"Cache-Control": "no-store"},
            )
        origin = request.headers.get("origin")
        if origin != config.base_url:
            raise HTTPException(
                403,
                "invalid setup origin",
                headers={"Cache-Control": "no-store"},
            )
        if password != password_confirmation:
            setup_attempts.record_failure(attempt_key)
            raise HTTPException(
                400,
                "password confirmation does not match",
                headers={"Cache-Control": "no-store"},
            )
        try:
            result = claim_first_owner(
                engine,
                org_id=org_id,
                claim_token=claim_token,
                expected_claim_token=app.state.setup_claim_token,
                workspace_name=workspace_name,
                owner_name=owner_name,
                email=email,
                password=password,
            )
        except InvalidCredentials as exc:
            setup_attempts.record_failure(attempt_key)
            raise HTTPException(
                403,
                "invalid setup code",
                headers={"Cache-Control": "no-store"},
            ) from exc
        except SetupUnavailable as exc:
            app.state.setup_claim_token = None
            raise HTTPException(
                409,
                "setup is unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        except SetupError as exc:
            setup_attempts.record_failure(attempt_key)
            raise HTTPException(
                400,
                str(exc),
                headers={"Cache-Control": "no-store"},
            ) from exc
        app.state.setup_claim_token = None
        setup_attempts.reset(attempt_key)
        response = RedirectResponse("/", status_code=303)
        _set_cookie(response, result["session"], secure=config.secure_cookies)
        return _no_store(response)

    register_browser_auth_routes(
        app,
        engine=engine,
        auth=auth,
        base_url=config.base_url,
        session_cookie=SESSION_COOKIE,
        secure_cookies=config.secure_cookies,
        magic_link_enabled=config.magic_link_enabled,
        oidc_providers=config.oidc_providers,
        send_magic_email=send_magic_email,
        oidc_exchange=oidc_exchange,
        resolve_request=resolve_request,
        trusted_proxy_cidrs=config.trusted_proxy_cidrs,
    )
    register_session_routes(
        app,
        session=identity_session,
        session_cookie=SESSION_COOKIE,
        require_user=lambda request: require_user(request),
        require_browser_user=lambda request: require_user(request, browser=True),
    )

    @app.post("/api/projects")
    def create_project(request: Request, body: _Body) -> dict[str, Any]:
        user = require_org_member(request)
        if not body.name.strip():
            raise HTTPException(400, "project name is required")
        slug = _project_intent_slug(body.name)
        with engine.begin() as cx:
            cx.execute(
                project_creation_intents.insert().values(
                    slug=slug,
                    org_id=org_id,
                    creator_user_id=user["id"],
                    name=body.name.strip(),
                    sensitive=body.sensitive,
                )
            )
        return _complete_project_intent(engine, core.state.workspace, slug=slug)

    @app.patch("/api/projects/{pid}/sensitivity", name="update_project_sensitivity")
    def update_project_sensitivity(
        pid: str, request: Request, body: _Body
    ) -> dict[str, Any]:
        """Team twin of the core /sensitivity route: writes the control-plane
        projects.sensitive AND the bundle in one step, exactly like the
        /network twin below. Without the control-plane half, a project marked
        sensitive after creation carried `projects.sensitive = false` forever
        — intent completion and the hosted admin surfaces read that column, so
        the record of which projects are protected disagreed with the bundle
        the egress gate actually reads."""
        actor = require_user(request)
        if not access.can_on_project(org_id, pid, int(actor["id"]), "owner"):
            raise HTTPException(403, "project owner required")
        with engine.begin() as cx:
            written = cx.execute(
                projects.update()
                .where(projects.c.org_id == org_id, projects.c.slug == pid)
                .values(sensitive=body.sensitive)
            )
            if written.rowcount == 0:
                raise HTTPException(404, f"no project '{pid}'")
            updated = core.state.workspace.get(pid).set_project_sensitivity(
                body.sensitive
            )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor["id"],
                    org_id=org_id,
                    action="project_sensitivity_updated",
                    detail=f"{pid}:{str(body.sensitive).lower()}",
                )
            )
        return updated

    @app.patch(
        "/api/projects/{pid}/network",
        name="update_project_network",
        response_model=ProjectNetworkResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def update_project_network(
        pid: str, request: Request, body: ProjectNetworkRequest
    ) -> ProjectNetworkResponse:
        """Team twin of the core /network route: writes the control-plane
        projects.network AND the bundle in one step (the sensitivity-route
        precedent), so intent recovery re-materializes the same value."""
        actor = require_user(request)
        if not access.can_on_project(org_id, pid, int(actor["id"]), "owner"):
            raise HTTPException(403, "project owner required")
        if body.mode not in ("inherit", "on", "off"):
            raise HTTPException(400, f"unsupported network mode: {body.mode}")
        with engine.begin() as cx:
            updated = cx.execute(
                projects.update()
                .where(projects.c.org_id == org_id, projects.c.slug == pid)
                .values(network=body.mode)
            )
            if updated.rowcount == 0:
                raise HTTPException(404, f"no project '{pid}'")
            _sync_project_network_policy(
                cx, core.state.workspace, org_id=org_id, slug=pid
            )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor["id"],
                    org_id=org_id,
                    action="project_network_updated",
                    detail=f"{pid}:{body.mode}",
                )
            )
        project = core.state.workspace.get(pid)
        return ProjectNetworkResponse.model_validate(
            {
                "schemaVersion": "frisket.project_network_policy.v1",
                **project.network_policy(),
                "effective": project.effective_network_policy(),
            }
        )

    @app.delete("/api/projects/{pid}", name="delete_project")
    def delete_project(pid: str, request: Request, body: _DeleteBody) -> dict[str, Any]:
        """Team twin of the core delete route: applies the same danger-zone
        gates (typed-name confirmation + runs-in-flight refusal) and the real
        bundle deletion via the core service, then removes the control-plane
        rows for this project and writes an audit entry in one transaction so
        the deletion is attributable and leaves no ghost project behind."""
        from frisket.server.services.projects import (
            ProjectDeletionBlocked,
            ProjectDeletionRequiresConfirmation,
            ProjectLifecycleService,
            ProjectNotFound,
        )

        actor = require_user(request)
        if not access.can_on_project(org_id, pid, int(actor["id"]), "owner"):
            raise HTTPException(403, "project owner required")
        service = ProjectLifecycleService(core.state.workspace)
        try:
            result = service.delete_project(pid, confirm_name=body.confirm_name)
        except ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ProjectDeletionRequiresConfirmation as exc:
            raise HTTPException(422, str(exc)) from exc
        except ProjectDeletionBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        with engine.begin() as cx:
            for table in (project_roles, project_invites, project_creation_intents):
                cx.execute(
                    table.delete().where(table.c.org_id == org_id, table.c.slug == pid)
                )
            cx.execute(
                projects.delete().where(
                    projects.c.org_id == org_id, projects.c.slug == pid
                )
            )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor["id"],
                    org_id=org_id,
                    action="project_deleted",
                    detail=pid,
                )
            )
        return result

    @app.get("/api/org/network")
    def get_org_network(request: Request) -> dict[str, Any]:
        require_org_member(request, browser=True)
        with engine.connect() as cx:
            network_default = cx.execute(
                sa.select(orgs.c.network_default).where(orgs.c.id == org_id)
            ).scalar_one_or_none()
        return {
            "network_default": network_default,
            "effective_default": (network_default or "on"),
        }

    @app.patch("/api/org/network")
    def update_org_network(request: Request, body: _OrgNetworkBody) -> dict[str, Any]:
        """Org-wide default for projects whose mode is "inherit" (egress-gate
        same synchronization rule). Owner-only; fans the new default out to every org
        project bundle because the gate reads bundle-side."""
        actor = require_user(request, browser=True, admin=True)
        if body.network_default not in (None, "on", "off"):
            raise HTTPException(
                400, f"unsupported network default: {body.network_default}"
            )
        with engine.begin() as cx:
            cx.execute(
                orgs.update()
                .where(orgs.c.id == org_id)
                .values(network_default=body.network_default)
            )
            slugs = list(
                cx.execute(
                    sa.select(projects.c.slug).where(projects.c.org_id == org_id)
                ).scalars()
            )
            for slug in slugs:
                _sync_project_network_policy(
                    cx, core.state.workspace, org_id=org_id, slug=slug
                )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor["id"],
                    org_id=org_id,
                    action="org_network_default_updated",
                    detail=str(body.network_default),
                )
            )
        return {
            "network_default": body.network_default,
            "effective_default": body.network_default or "on",
        }

    @app.get(
        "/api/org/media-proxy/status",
        response_model=OrganizationMediaProxyStatus,
        responses=http_error_responses(401, 403, 500),
    )
    def org_media_proxy_status(request: Request) -> dict[str, Any]:
        """Member-readable media egress proxy status for run-failure
        remediation UI. Deliberately boolean-only: members learn whether a
        proxy is configured and currently carrying traffic, never its URL
        (that stays on the operator admin surface). ``can_configure`` tells
        the UI whether to show the setup command or ask-your-admin copy."""
        user = require_org_member(request)
        from frisket.ops import media_proxy as media_proxy_config

        resolved = media_proxy_config.read_media_proxy(config.data_dir)
        connected: bool | None = None
        if resolved.url is not None:
            connected = media_proxy_config.probe_media_proxy_cached(resolved.url).ok
        return {
            "configured": resolved.url is not None,
            "connected": connected,
            "can_configure": (
                _membership_role(engine, int(user["id"]), org_id) == "owner"
            ),
        }

    @app.get("/api/projects")
    def list_projects(request: Request) -> list[dict[str, Any]]:
        user = require_user(request)
        workspace_rows = {row["id"]: row for row in core.state.workspace.list()}
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(projects.c.slug, projects.c.name).where(
                    projects.c.org_id == org_id
                )
            ).all()
            allowed = [
                (row, role)
                for row in rows
                if (role := access._role(cx, int(user["id"]), str(row.slug)))
                is not None
            ]
        return [
            {
                **workspace_rows.get(
                    str(row.slug),
                    {
                        "id": str(row.slug),
                        "name": row.name,
                        "description": "",
                        "sensitive": False,
                        "updated_at": None,
                        "pending_review_count": 0,
                        "starred": False,
                        "archived": False,
                    },
                ),
                "role": role,
            }
            for row, role in allowed
        ]

    @app.post(
        "/api/projects/{pid}/members",
        response_model=ProjectMemberSet,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def set_member(
        pid: str, request: Request, body: SetProjectMemberRequest
    ) -> dict[str, Any]:
        actor = require_user(request)
        if (
            not access.can_on_project(org_id, pid, int(actor["id"]), "owner")
            or body.role not in access.ROLES
        ):
            raise HTTPException(403, "project owner required")
        with locked_transaction(
            engine, lock_scope=("project-membership", org_id, pid)
        ) as cx:
            if access._role(cx, int(actor["id"]), pid) != "owner":
                raise HTTPException(403, "project owner required")
            user_id = cx.execute(
                sa.select(users.c.id).where(users.c.email == body.email.lower().strip())
            ).scalar_one_or_none()
            if user_id is None:
                raise HTTPException(404, "user not found")
            existing = cx.execute(
                sa.select(project_roles.c.role).where(
                    project_roles.c.org_id == org_id,
                    project_roles.c.slug == pid,
                    project_roles.c.user_id == user_id,
                )
            ).scalar_one_or_none()
            if existing == "owner" and body.role != "owner":
                owner_count = cx.execute(
                    sa.select(sa.func.count())
                    .select_from(project_roles)
                    .where(
                        project_roles.c.org_id == org_id,
                        project_roles.c.slug == pid,
                        project_roles.c.role == "owner",
                    )
                ).scalar_one()
                if owner_count <= 1:
                    raise HTTPException(409, "cannot demote the last project owner")
            atomic_upsert(
                cx,
                table=project_roles,
                values={
                    "org_id": org_id,
                    "slug": pid,
                    "user_id": user_id,
                    "role": body.role,
                },
                conflict_columns=("org_id", "slug", "user_id"),
                update_values={"role": body.role},
            )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor["id"],
                    org_id=org_id,
                    action="project_member_set",
                    detail=f"{pid}:{body.email.lower().strip()}:{body.role}",
                )
            )
        return {"ok": True, "email": body.email, "role": body.role}

    @app.get(
        "/api/projects/{pid}/members",
        response_model=ProjectMemberList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def list_members(pid: str, request: Request) -> list[dict[str, Any]]:
        actor = require_user(request)
        if not access.can_on_project(org_id, pid, int(actor["id"]), "owner"):
            raise HTTPException(403, "project owner required")
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(users.c.id, users.c.email, project_roles.c.role)
                .join(project_roles, project_roles.c.user_id == users.c.id)
                .where(project_roles.c.org_id == org_id, project_roles.c.slug == pid)
            ).all()
        return [
            {
                "user_id": int(row.id),
                "email": str(row.email),
                "role": str(row.role),
            }
            for row in rows
        ]

    @app.delete(
        "/api/projects/{pid}/members/{email}",
        response_model=ProjectMemberRemove,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def remove_member(pid: str, email: str, request: Request) -> dict[str, bool]:
        actor = require_user(request)
        if not access.can_on_project(org_id, pid, int(actor["id"]), "owner"):
            raise HTTPException(403, "project owner required")
        with locked_transaction(
            engine, lock_scope=("project-membership", org_id, pid)
        ) as cx:
            if access._role(cx, int(actor["id"]), pid) != "owner":
                raise HTTPException(403, "project owner required")
            user_id = cx.execute(
                sa.select(users.c.id).where(users.c.email == email.lower().strip())
            ).scalar_one_or_none()
            if user_id is None:
                raise HTTPException(404, "user not found")
            role = cx.execute(
                sa.select(project_roles.c.role).where(
                    project_roles.c.org_id == org_id,
                    project_roles.c.slug == pid,
                    project_roles.c.user_id == user_id,
                )
            ).scalar_one_or_none()
            if role == "owner":
                owner_count = cx.execute(
                    sa.select(sa.func.count())
                    .select_from(project_roles)
                    .where(
                        project_roles.c.org_id == org_id,
                        project_roles.c.slug == pid,
                        project_roles.c.role == "owner",
                    )
                ).scalar_one()
                if owner_count <= 1:
                    raise HTTPException(409, "cannot remove the last project owner")
            result = cx.execute(
                project_roles.delete().where(
                    project_roles.c.org_id == org_id,
                    project_roles.c.slug == pid,
                    project_roles.c.user_id == user_id,
                )
            )
            if result.rowcount:
                cx.execute(
                    audit_log.insert().values(
                        user_id=actor["id"],
                        org_id=org_id,
                        action="project_member_removed",
                        detail=f"{pid}:{email.lower().strip()}",
                    )
                )
        return {"ok": True, "removed": bool(result.rowcount)}

    @app.post(
        "/api/org/tokens",
        response_model=ApiTokenCreateResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 422, 500),
    )
    def create_token(
        request: Request, body: ApiTokenCreateRequest
    ) -> ApiTokenCreateResponse:
        user = require_org_member(request, browser=True)
        token = "frisket_pat_" + secrets.token_urlsafe(32)
        token_name = body.name.strip() or "token"
        token_prefix = token[:20]
        with engine.begin() as cx:
            token_id = cx.execute(
                api_tokens.insert().values(
                    org_id=org_id,
                    user_id=user["id"],
                    name=token_name,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    prefix=token_prefix,
                )
            ).inserted_primary_key[0]
            cx.execute(
                audit_log.insert().values(
                    user_id=user["id"],
                    org_id=org_id,
                    action="pat_created",
                    detail=str(token_id),
                )
            )
        return ApiTokenCreateResponse.model_validate(
            {
                "id": int(token_id),
                "name": token_name,
                "token": token,
                "prefix": token_prefix,
            }
        )

    @app.get(
        "/api/org/tokens",
        response_model=ApiTokenList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def list_tokens(request: Request) -> ApiTokenList:
        user = require_org_member(request, browser=True)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(
                    api_tokens.c.id,
                    api_tokens.c.user_id,
                    api_tokens.c.name,
                    api_tokens.c.prefix,
                    api_tokens.c.created_at,
                    api_tokens.c.last_used_at,
                ).where(
                    api_tokens.c.org_id == org_id,
                    api_tokens.c.user_id == user["id"],
                    api_tokens.c.revoked_at.is_(None),
                )
            ).all()
        return ApiTokenList.model_validate(
            [
                {
                    "id": int(row.id),
                    "user_id": int(row.user_id),
                    "created_by": str(user["email"]),
                    "name": str(row.name),
                    "prefix": str(row.prefix),
                    "created_at": (
                        row.created_at.isoformat()
                        if row.created_at is not None
                        else None
                    ),
                    "last_used_at": (
                        row.last_used_at.isoformat()
                        if row.last_used_at is not None
                        else None
                    ),
                    "revoked": False,
                }
                for row in rows
            ]
        )

    @app.delete(
        "/api/org/tokens/{token_id}",
        response_model=ApiTokenRevokeResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 422, 500),
    )
    def revoke_token(token_id: int, request: Request) -> ApiTokenRevokeResponse:
        user = require_org_member(request, browser=True)

        with engine.begin() as cx:
            result = cx.execute(
                api_tokens.update()
                .where(
                    api_tokens.c.id == token_id,
                    api_tokens.c.org_id == org_id,
                    api_tokens.c.user_id == user["id"],
                )
                .values(revoked_at=datetime.now(UTC))
            )
            if result.rowcount:
                cx.execute(
                    audit_log.insert().values(
                        user_id=user["id"],
                        org_id=org_id,
                        action="pat_revoked",
                        detail=str(token_id),
                    )
                )
        revoked = bool(result.rowcount)
        return ApiTokenRevokeResponse.model_validate(
            {"ok": revoked, "revoked": revoked}
        )

    async def unavailable_invite_mail(_email: str, _link: str) -> bool:
        raise HTTPException(503, "invite email transport is not configured")

    invite_service = InviteService(engine, org_id=org_id, access=access)
    admin_membership = AdminMembershipService(engine, org_id=org_id)
    register_admin_api_routes(
        app,
        engine=engine,
        org_id=org_id,
        invites=invite_service,
        auth=auth,
        operator_service=operator_service,
        secret_box=secret_box,
        require_user=require_user,
        base_url=config.base_url,
        data_dir=config.data_dir,
    )

    @app.get("/auth/reset/{token}", response_class=HTMLResponse)
    def password_reset_page(token: str) -> Response:
        if not password_reset_is_live(engine, token):
            raise HTTPException(404, "reset link is invalid, expired, or already used")
        return _no_store(
            HTMLResponse(
                _local_auth_page(
                    title="Reset your Frisket password",
                    eyebrow="Password reset",
                    heading="Choose a new password.",
                    lede=(
                        "This reset link was issued by your workspace "
                        "administrator and can be used exactly once."
                    ),
                    expectation=(
                        "After the reset you are signed out everywhere and "
                        "return to the regular sign-in page with the new "
                        "password."
                    ),
                    panel_heading="Set a new password",
                    trusted_panel_html=f"""
      <form method="post" action="/auth/reset/{escape(token, quote=True)}">
        <label class="field">
          <span>New password</span>
          <input name="password" type="password" autocomplete="new-password"
            minlength="12" maxlength="1024" aria-describedby="reset-password-help" required>
          <span class="hint" id="reset-password-help">Use 12&ndash;1,024 characters.</span>
        </label>
        <label class="field">
          <span>Confirm new password</span>
          <input name="password_confirmation" type="password" autocomplete="new-password"
            minlength="12" maxlength="1024" required>
        </label>
        <button type="submit">Reset password</button>
      </form>""",
                )
            )
        )

    @app.post("/auth/reset/{token}")
    def password_reset_submit(
        token: str,
        password: str = Form(""),
        password_confirmation: str = Form(""),
    ) -> Response:
        if password != password_confirmation:
            raise HTTPException(
                400,
                "password confirmation does not match",
                headers={"Cache-Control": "no-store"},
            )
        try:
            redeem_password_reset(engine, org_id=org_id, token=token, password=password)
        except ResetTokenInvalid as exc:
            raise HTTPException(
                404,
                str(exc),
                headers={"Cache-Control": "no-store"},
            ) from exc
        except SetupError as exc:
            raise HTTPException(
                400,
                str(exc),
                headers={"Cache-Control": "no-store"},
            ) from exc
        return _no_store(RedirectResponse("/", status_code=303))

    register_invite_routes(
        app,
        invites=invite_service,
        auth=auth,
        require_user=lambda request: require_user(request),
        require_admin=lambda request: require_user(request, browser=True, admin=True),
        base_url=config.base_url,
        session_cookie=SESSION_COOKIE,
        secure_cookie=config.secure_cookies,
        send_mail=send_magic_email or unavailable_invite_mail,
    )
    app.state.invite_service = invite_service

    register_admin_browser_routes(
        app,
        engine=engine,
        org_id=org_id,
        invites=invite_service,
        auth=auth,
        membership=admin_membership,
        require_user=require_user,
        base_url=config.base_url,
        send_mail=send_magic_email or unavailable_invite_mail,
        run_queue=core.state.workspace.queue,
        workspace_root=core.state.workspace.root,
    )
    app.state.admin_membership_service = admin_membership

    register_oauth_connection_routes(
        app,
        config=config,
        engine=engine,
        secret_box=secret_box,
        require_browser_user=lambda request: require_org_member(request, browser=True),
        google_connection_exchange=google_connection_exchange,
        # The SAME instance already wired into `_connected_account_resolver`
        # above (before `create_app()`), not a second equivalent one.
        service=oauth_connection_service,
    )
    app.state.oauth_connection_service = oauth_connection_service

    register_observability_routes(
        app,
        engine=engine,
        org_id=org_id,
        require_user=require_user,
        run_queue=core.state.workspace.queue,
        workspace=core.state.workspace,
    )

    register_operational_routes(
        app,
        engine=engine,
        org_id=org_id,
        require_user=require_user,
        resolve_user=resolve_request,
        membership_role=lambda user_id, oid: _membership_role(engine, user_id, oid),
        run_queue=core.state.workspace.queue,
        validate_provider_key=validate_provider_key,
        secret_box=secret_box,
        workspace=core.state.workspace,
        admin_membership=admin_membership,
    )
    _install_team_outer_policy(app, require_user=require_user)
    # Mount the built SPA onto the core sub-app, AFTER all of core's own API
    # routes and the outer app's auth/session/org routes above.
    #
    # `core` keeps serve_spa=False from its own create_app() call above (it
    # must not grow an inner catch-all just because the process-wide
    # FRISKET_STATIC_DIR is set — same reasoning as the local tier's
    # embedder opt-out); mount_spa_static() instead appends the static mount
    # directly to core's router here, after every route core already
    # registered, so within core the SPA fallback is tried last. The outer
    # `app` is only ever reached via `app.mount("/", core)` below for paths
    # its own routes above did not already claim, so this ordering also
    # keeps every outer auth/session/org route ahead of the SPA.
    #
    # resolve_static_dir() is the same resolution the local tier uses, EXCEPT
    # allow_packaged_fallback=False: explicit FRISKET_STATIC_DIR (the shipped
    # team image always sets this, to /app/static/team, see Dockerfile) ->
    # None. The packaged-wheel tier is deliberately skipped here --
    # scripts/release/build_frontend.py only ever stages the LOCAL edition's build
    # into frisket/web_static/, so falling back to it would silently serve
    # the wrong SPA (local capabilities, not team's) for the unsupported
    # case of running this factory from an installed wheel with no
    # FRISKET_STATIC_DIR set. A dev checkout with no built bundle (and the
    # supported install path, the Docker image, always sets the env var)
    # resolves to None and mounts nothing, so `frisket.team.app` stays
    # headless exactly like today (the dev-mode signal vite relies on is
    # preserved).
    resolved_static = resolve_static_dir(
        config.static_dir or "", allow_packaged_fallback=False
    )
    if resolved_static is not None:
        map_config = {
            key: value
            for key, value in (
                ("tileUrlTemplate", config.map_tile_url_template),
                ("apiKey", config.map_api_key),
                ("attribution", config.map_attribution),
            )
            if value
        }
        mount_spa_static(
            core,
            resolved_static,
            client_error_capture=config.client_error_capture,
            map_config=map_config,
        )
    app.mount("/", core)
    app.add_middleware(
        SetupGate,
        claimed=lambda: owner_count(engine, org_id=org_id) > 0,
        operator_probe=lambda authorization: (
            operator_service.authenticate(authorization) is not None
        ),
    )
    return app


def _membership_role(engine: sa.Engine, user_id: int, org_id: int) -> str | None:
    with engine.connect() as cx:
        return cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.user_id == user_id, memberships.c.org_id == org_id
            )
        ).scalar_one_or_none()


# The team-only local-model routes: an EDITION-SCOPED catalog contribution,
# declared here at team composition time via `install_outer_policy`'s
# `local_declarations` parameter -- deliberately NOT added to
# `frisket.contracts.http.endpoint_catalog`'s shared `BASE_ENDPOINT_CATALOG`
# (also consumed by an external managed edition's own route-policy compiler),
# because that shared catalog fails closed on a declaration for a route that
# edition never registered. An external edition does not have these routes,
# so putting them there would break that edition's own startup the moment it
# composed against BASE_ENDPOINT_CATALOG. `frisket.team.policy`
# (this module's own compiler, imported by nothing outside the open team
# package) is the mechanism that "cannot break another edition's startup":
# an external composition never imports
# `frisket.team.policy.compile_team_policies`
# or its `local_declarations` parameter, so this contribution is invisible
# to it by construction, not by convention. That composition's CI should
# still verify, once, that its own route-policy compiler run never imports
# `frisket.team.policy` (an accidental import would be the only way this
# contribution could ever reach it).
_TEAM_LOCAL_MODEL_DECLARATIONS = tuple(
    replace(declaration, browser_client=True)
    for declaration in declare_endpoints(
        "outer",
        "session_or_pat",
        (
            ("list_org_local_endpoints", "GET"),
            ("post_org_models_pull", "POST"),
            ("list_org_model_pulls", "GET"),
            ("get_org_model_pull", "GET"),
            ("cancel_org_model_pull", "POST"),
        ),
    )
)

# Browser-session auth is team-composition-only. Keep it out of the shared
# base catalog and use this as the one source for auth and browser membership.
_TEAM_BROWSER_AUTH_DECLARATIONS = tuple(
    replace(declaration, browser_client=True)
    for declaration in declare_endpoints(
        "outer",
        "public",
        (
            ("complete_magic_link", "POST"),
            ("accept_project_invite", "POST"),
            ("password_login", "POST"),
            ("request_link", "POST"),
            ("logout", "POST"),
        ),
    )
)

_TEAM_READINESS_DECLARATIONS = declare_endpoints(
    "outer",
    "public",
    (("server_readiness", "GET"),),
)

# Operator-only administration remains a team-local policy contribution. The
# browser siblings below now live in the shared catalog because they are a
# generated public HTTP surface; keeping these two inventories separate avoids
# duplicating those browser operation IDs.
_TEAM_OPERATOR_ADMIN_DECLARATIONS = declare_endpoints(
    "outer",
    "admin",
    (
        ("admin_ping", "GET"),
        ("admin_rotate_operator_token", "POST"),
        ("admin_create_user", "POST"),
        ("admin_set_user_role_by_email", "PUT"),
        ("admin_reset_user_password", "POST"),
        ("admin_list_secrets", "GET"),
        ("admin_set_secret", "PUT"),
        ("admin_delete_secret", "DELETE"),
        ("admin_get_media_proxy", "GET"),
        ("admin_set_media_proxy", "PUT"),
        ("admin_clear_media_proxy", "DELETE"),
        ("admin_check_media_proxy", "POST"),
    ),
)


def _install_team_outer_policy(
    app: FastAPI, *, require_user: Callable[..., dict[str, Any]]
) -> None:
    install_outer_policy(
        app,
        local_declarations=(
            *_TEAM_LOCAL_MODEL_DECLARATIONS,
            *_TEAM_BROWSER_AUTH_DECLARATIONS,
            *_TEAM_READINESS_DECLARATIONS,
            *_TEAM_OPERATOR_ADMIN_DECLARATIONS,
        ),
        auth_declarations={
            ("GET", "/auth/callback", "callback"): "public",
            ("GET", "/auth/oidc/{provider}", "oidc_start"): "public",
            (
                "GET",
                "/auth/oidc/{provider}/callback",
                "oidc_callback",
            ): "public",
            (
                "GET",
                "/auth/project-invites/{token}/accept",
                "accept_project_invite_link",
            ): "public",
            ("GET", "/auth/reset/{token}", "password_reset_page"): "public",
            ("POST", "/auth/reset/{token}", "password_reset_submit"): "public",
        },
        require_user=require_user,
    )


def create_team_app_from_env() -> FastAPI:
    """ASGI factory for Uvicorn with server proxy-header parsing disabled.

    Run ``uvicorn --factory frisket.team.app:create_team_app_from_env
    --no-proxy-headers`` so browser-auth source resolution retains the socket
    peer and applies only Frisket's explicit trusted-proxy configuration.

    THE production entrypoint — a team deployment without an explicit
    run-queue locator is a broken topology for EVERY queued job kind, not
    just model pulls: the app would enqueue somewhere a bare ``frisket
    worker`` (which falls back to a workspace-local queue file) never
    drains. Every team
    composition, this production entrypoint included, now fails fast on a
    missing ``FRISKET_RUN_QUEUE_DATABASE_URL`` -- :func:`create_team_app`
    itself raises unconditionally, so no separate pre-check is needed here.
    Compose already sets the variable unconditionally, so shipped
    deployments never see this.
    """
    from frisket.ai.llm.pricing_refresh import start_pricing_refresh

    start_pricing_refresh()
    return create_team_app(team_config_from_env())


__all__ = [
    "TeamConfig",
    "create_team_app",
    "create_team_app_from_env",
    "team_config_from_env",
]
