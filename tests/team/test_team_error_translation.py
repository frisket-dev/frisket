"""Error-translation coverage for the team edition.

Three things under test:
1. ``frisket.team.diagnostics.BOOT_FAILURE_CATALOG`` -- every seeded failure
   has a non-empty ``{what, todo}`` and the catalog covers the named
   self-hosting failures.
2. ``classify_team_boot_error`` -- the SMTP-transport translation seam,
   reusing ``RemediatedError`` (the SAME shape ``classify_llm_error`` uses),
   NARROWLY matched so unrelated OSError subclasses
   and non-connection smtplib rejections are left unclassified.
3. An unreachable/misconfigured SMTP host at magic-link request time used to
   surface as a raw 500 with a bare traceback
   message -- ``/auth/request-link`` must now translate it instead, and log
   the real cause (this app has no request-error logging middleware).
"""

from __future__ import annotations

import logging
import smtplib
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm.remediation import RemediatedError
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.diagnostics import (
    BOOT_FAILURE_CATALOG,
    HOSTED_WORKER_WRONG_MODE,
    MAGIC_LINK_SMTP_LOGIN_FAILED,
    MAGIC_LINK_SMTP_UNREACHABLE,
    PEP668_NO_PIP,
    PLAIN_HTTP_BASE_URL_REJECTED,
    PYICU_TOOLCHAIN_MISSING,
    classify_team_boot_error,
)
from tests.team_setup_helpers import claim_server


# ---------------------------------------------------------------------------
# BOOT_FAILURE_CATALOG -- the seeded {what, todo, route} entries


@pytest.mark.parametrize(
    "code",
    [
        PEP668_NO_PIP,
        PYICU_TOOLCHAIN_MISSING,
        HOSTED_WORKER_WRONG_MODE,
        MAGIC_LINK_SMTP_UNREACHABLE,
        MAGIC_LINK_SMTP_LOGIN_FAILED,
        PLAIN_HTTP_BASE_URL_REJECTED,
    ],
)
def test_catalog_entry_has_what_and_todo(code: str) -> None:
    entry = BOOT_FAILURE_CATALOG[code]
    assert entry["what"].strip()
    assert entry["todo"].strip()
    # route is either a concrete in-app anchor or an explicit None (no
    # fabricated anchor for a failure that happens before any frisket
    # process is serving requests) -- never missing.
    assert "route" in entry


def test_pyicu_entry_routes_to_the_entities_extra_probe() -> None:
    """The one seeded entry with a LIVE in-app surface (diagnostics.
    entities_report, extended with a platform-aware hint) must point at it."""
    entry = BOOT_FAILURE_CATALOG[PYICU_TOOLCHAIN_MISSING]
    assert entry["route"] == "Settings → Diagnostics → entities extra"


def test_hosted_worker_entry_names_the_fix() -> None:
    entry = BOOT_FAILURE_CATALOG[HOSTED_WORKER_WRONG_MODE]
    assert "frisket worker" in entry["todo"]


def test_smtp_unreachable_entry_names_the_real_env_vars() -> None:
    """The remediation used to name nonexistent
    FRISKET_TEAM_SMTP_* vars -- team/config.py actually reads FRISKET_SMTP_*
    (team_config_from_env)."""
    entry = BOOT_FAILURE_CATALOG[MAGIC_LINK_SMTP_UNREACHABLE]
    assert "FRISKET_SMTP_HOST" in entry["todo"]
    assert "FRISKET_SMTP_PORT" in entry["todo"]
    assert "FRISKET_TEAM_SMTP" not in entry["todo"]


def test_smtp_login_failed_entry_names_the_real_env_vars() -> None:
    entry = BOOT_FAILURE_CATALOG[MAGIC_LINK_SMTP_LOGIN_FAILED]
    assert "FRISKET_SMTP_USERNAME" in entry["todo"]
    assert "FRISKET_SMTP_PASSWORD" in entry["todo"]


# ---------------------------------------------------------------------------
# classify_team_boot_error -- the SMTP transport translation seam, narrowly
# matched narrowly


def test_classify_dns_failure_as_smtp_unreachable() -> None:
    exc = socket.gaierror("Name or service not known")
    remediated = classify_team_boot_error(exc)
    assert isinstance(remediated, RemediatedError)
    assert remediated.code == MAGIC_LINK_SMTP_UNREACHABLE
    assert "SMTP" in remediated.message
    assert remediated.details["route"] == "Settings → Diagnostics"


def test_classify_connection_refused_as_smtp_unreachable() -> None:
    remediated = classify_team_boot_error(ConnectionRefusedError("Connection refused"))
    assert remediated is not None
    assert remediated.code == MAGIC_LINK_SMTP_UNREACHABLE


def test_classify_timeout_as_smtp_unreachable() -> None:
    remediated = classify_team_boot_error(TimeoutError("timed out"))
    assert remediated is not None
    assert remediated.code == MAGIC_LINK_SMTP_UNREACHABLE


def test_classify_smtp_auth_failure_as_login_failed_not_unreachable() -> None:
    """An auth rejection means the host WAS reached
    -- it must not get the "unreachable" (DNS/refused) diagnosis."""
    exc = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    remediated = classify_team_boot_error(exc)
    assert remediated is not None
    assert remediated.code == MAGIC_LINK_SMTP_LOGIN_FAILED
    assert remediated.code != MAGIC_LINK_SMTP_UNREACHABLE
    assert "login" in remediated.message.lower()


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("unrelated"),
        FileNotFoundError("no such file: /etc/frisket/missing.pem"),
        PermissionError("permission denied"),
        smtplib.SMTPRecipientsRefused({"bad@example.test": (550, b"no such user")}),
        smtplib.SMTPSenderRefused(501, b"bad sender", "from@example.test"),
        smtplib.SMTPDataError(554, b"message rejected"),
    ],
)
def test_classify_unrelated_or_non_connection_exceptions_returns_none(
    exc: Exception,
) -> None:
    """Opt-in contract, same as classify_resumable_provider_error: anything
    outside the narrowly-seeded set is the caller's problem, not silently
    swallowed into a wrong translation. The
    pre-fix classifier matched bare OSError, which caught FileNotFoundError/
    PermissionError and every non-connection smtplib rejection too --
    hiding real, different failures behind a false "unreachable" diagnosis."""
    assert classify_team_boot_error(exc) is None


# ---------------------------------------------------------------------------
# TeamConfig: the plain-http rejection now names the actual fix


def test_plain_http_rejection_names_tls_and_the_env_var(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        TeamConfig(
            database_url="sqlite:///:memory:",
            data_dir=tmp_path / "data",
            base_url="http://team.example.test",  # not loopback
            organization_name="Desk",
            admin_emails={"owner@example.com"},
        )
    message = str(excinfo.value)
    assert "TLS" in message
    assert "FRISKET_BASE_URL" in message


# ---------------------------------------------------------------------------
# /auth/request-link: the actual rehearsal bug (raw 500) is now translated


async def _boom_mail(_email: str, _link: str) -> bool:
    raise socket.gaierror("Name or service not known")


async def _oidc(_provider: str, _code: str, _redirect: str, nonce: str) -> dict:
    return {
        "email": "oidc@example.com",
        "email_verified": True,
        "subject": "subject-1",
        "nonce": nonce,
    }


def _config(tmp_path: Path, **overrides) -> TeamConfig:
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        # required unconditionally since 1f40bbc (run-queue locator hard
        # requirement); this suite predates that and was left red.
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Desk",
        admin_emails={"owner@example.com"},
        oidc_providers={
            "work": {
                "issuer": "https://id.test",
                "client_id": "client",
                "client_secret": "secret",
                "authorization_endpoint": "https://id.test/authorize",
                "token_endpoint": "https://id.test/token",
                "jwks_uri": "https://id.test/jwks",
            }
        },
    )
    values.update(overrides)
    return TeamConfig(**values)


def test_request_link_translates_smtp_failure_instead_of_raw_500(
    tmp_path: Path,
) -> None:
    app = create_team_app(
        _config(tmp_path), send_magic_email=_boom_mail, oidc_exchange=_oidc
    )
    client = TestClient(app, raise_server_exceptions=False)
    claim_server(app, client=client)
    resp = client.post("/auth/request-link", json={"email": "owner@example.com"})
    # NOT the pre-fix bare 500 with a raw "Name or service not known" body.
    assert resp.status_code == 502, resp.text
    body = resp.json()
    # Structured detail (code + route), not just a bare string -- the live
    # UI currently ignores response detail entirely and only reads the HTTP
    # status, so this is additive, not a regression.
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert "SMTP" in detail["message"]
    assert "Name or service not known" not in detail["message"]
    assert detail["code"] == MAGIC_LINK_SMTP_UNREACHABLE
    assert detail["route"] == "Settings → Diagnostics"


def test_request_link_logs_the_real_cause(tmp_path: Path, caplog) -> None:
    """The outer team app has no request-error logging middleware (only the
    mounted core app gets one) -- the /auth/request-link catch site must log
    the original exception itself or it never reaches logs at all."""
    app = create_team_app(
        _config(tmp_path), send_magic_email=_boom_mail, oidc_exchange=_oidc
    )
    client = TestClient(app, raise_server_exceptions=False)
    claim_server(app, client=client)
    with caplog.at_level(logging.ERROR, logger="frisket.team"):
        client.post("/auth/request-link", json={"email": "owner@example.com"})
    assert any(
        "magic_link_send_failed" in record.getMessage() for record in caplog.records
    )


async def _unrelated_boom_mail(_email: str, _link: str) -> bool:
    """A failure classify_team_boot_error does NOT recognize (e.g. a bad
    local attachment path) must still reach the caller as a real 500, not
    be mislabeled as an SMTP-unreachable diagnosis."""
    raise FileNotFoundError("no such file: /etc/frisket/attachment-missing.txt")


def test_request_link_reraises_unclassified_mail_failures(tmp_path: Path) -> None:
    app = create_team_app(
        _config(tmp_path), send_magic_email=_unrelated_boom_mail, oidc_exchange=_oidc
    )
    client = TestClient(app, raise_server_exceptions=False)
    claim_server(app, client=client)
    resp = client.post("/auth/request-link", json={"email": "owner@example.com"})
    # Not translated into the (wrong) SMTP-unreachable 502 -- re-raised as a
    # real 500, same as any other unhandled route exception.
    assert resp.status_code == 500
