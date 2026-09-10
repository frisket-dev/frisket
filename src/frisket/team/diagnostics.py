"""Canonical bounded sanitizer for open-team diagnostic surfaces, and the
team-edition boot/request-time error-translation catalog.

The catalog below SEEDS the existing translated-error layer
(``frisket.llm.remediation.classify_llm_error`` / ``RemediatedError``) with
common cold-start self-hosting failures that are NOT model-call
errors -- a plain-http ``base_url``, a magic-link email that can't reach an
SMTP host, and ``frisket hosted-worker`` run without its required run-queue
locator. It reuses ``RemediatedError``'s ``{code, message, details}`` shape
(imported from ``frisket.llm.remediation``, not redefined) so every
translated-error surface, model-call or team-boot, looks the same to a
caller: extend the existing mechanism instead of creating a second shape.
"""

from __future__ import annotations

import smtplib
import socket
from typing import Any

from frisket.ai.llm.remediation import RemediatedError
from frisket.redaction import redact_text


def sanitize_text(value: Any, *, limit: int) -> str:
    return redact_text(value, max_chars=limit, one_line=False).replace(
        "[REDACTED]", "[redacted]"
    )


# ---------------------------------------------------------------------------
# Boot/request-time error translation
# ---------------------------------------------------------------------------

# Stable machine codes, same vocabulary style as frisket.llm.remediation's
# MISSING_PROVIDER_KEY / OLLAMA_UNREACHABLE.
PEP668_NO_PIP = "pep668_no_pip"
PYICU_TOOLCHAIN_MISSING = "pyicu_toolchain_missing"
HOSTED_WORKER_WRONG_MODE = "hosted_worker_wrong_mode"
MAGIC_LINK_SMTP_UNREACHABLE = "magic_link_smtp_unreachable"
MAGIC_LINK_SMTP_LOGIN_FAILED = "magic_link_smtp_login_failed"
PLAIN_HTTP_BASE_URL_REJECTED = "plain_http_base_url_rejected"

# Each entry is {what happened, what to do, in-app route/anchor}. ``route``
# is the "Run diagnostics" deep-link target ("Every translated error links
# 'Run diagnostics' into this section anchored to the relevant probe");
# ``None`` means there is no in-app anchor because the failure happens
# before any frisket process is serving requests (PEP 668 is pip's own
# error, before Python runs; the plain-http rejection and the hosted-worker
# misconfiguration both crash at process boot, before a Diagnose panel could
# ever be reached).
BOOT_FAILURE_CATALOG: dict[str, dict[str, str | None]] = {
    PEP668_NO_PIP: {
        "what": (
            "`pip install frisket-data` fails immediately with "
            "'error: externally-managed-environment' (PEP 668) on a stock "
            "Debian/Ubuntu Python -- the system pip refuses a global install."
        ),
        "todo": (
            "Install inside a virtual environment instead of the system "
            "Python: `python3 -m venv .venv && source .venv/bin/activate "
            "&& pip install frisket-data` (or `pipx install frisket-data` / "
            "`uv tool install frisket-data`)."
        ),
        "route": None,
    },
    PYICU_TOOLCHAIN_MISSING: {
        "what": (
            "The optional `entities` extra (followthemoney/normality/pyicu) "
            "fails to build or import -- official PyPI publishes pyicu as "
            "source only, so pip needs a local C++/ICU toolchain on every "
            "platform."
        ),
        "todo": (
            "Base frisket works without it. Run `frisket doctor` for a real "
            "compiler/Python-header/pkg-config/ICU preflight. Install the "
            "reported prerequisites (or conda-forge pyicu in a conda env), "
            "then `pip install 'frisket-data[entities]'`."
        ),
        "route": "Settings → Diagnostics → entities extra",
    },
    HOSTED_WORKER_WRONG_MODE: {
        "what": (
            "`frisket hosted-worker` exited immediately with no queued jobs "
            "ever processed -- it requires the run-queue locator "
            "(FRISKET_RUN_QUEUE_DATABASE_URL) to come from the environment "
            "only, with no workspace directory argument."
        ),
        "todo": (
            "Self-hosted deployments almost always want the plain "
            "`frisket worker <workspace-dir>` command instead -- "
            "`hosted-worker` is the managed-worker mode with arbitrary code "
            "hard-disabled. If you do mean the hosted mode, set "
            "FRISKET_RUN_QUEUE_DATABASE_URL and pass no workspace argument."
        ),
        "route": None,
    },
    MAGIC_LINK_SMTP_UNREACHABLE: {
        "what": (
            "Magic-link sign-in is enabled but sending the email failed -- "
            "the configured SMTP host could not be reached (DNS failure, "
            "connection refused, or a timeout)."
        ),
        "todo": (
            "Verify FRISKET_SMTP_HOST and FRISKET_SMTP_PORT point at a "
            "real, reachable mail server, or switch to OIDC sign-in "
            "(FRISKET_OIDC_PROVIDERS_JSON) if no SMTP relay is available."
        ),
        "route": "Settings → Diagnostics",
    },
    MAGIC_LINK_SMTP_LOGIN_FAILED: {
        "what": (
            "Magic-link sign-in is enabled and the SMTP host was reached, "
            "but login failed -- the configured username/password were "
            "rejected."
        ),
        "todo": (
            "Verify FRISKET_SMTP_USERNAME and FRISKET_SMTP_PASSWORD are "
            "correct for this mail account, and that the account allows "
            "this kind of login (many providers require a generated app "
            "password rather than the account password)."
        ),
        "route": "Settings → Diagnostics",
    },
    PLAIN_HTTP_BASE_URL_REJECTED: {
        "what": (
            "The team server refused to start: FRISKET_BASE_URL is a plain "
            "http:// origin that isn't loopback (127.0.0.1/localhost) -- "
            "sign-in cookies and OAuth redirects are unsafe over unencrypted "
            "HTTP on a public host."
        ),
        "todo": (
            "Put a TLS-terminating reverse proxy (nginx, Caddy, a cloud "
            "load balancer) in front of the team server and set "
            "FRISKET_BASE_URL to the https:// origin it serves. Plain http "
            "stays allowed only for loopback development."
        ),
        "route": None,
    },
}


def classify_team_boot_error(exc: Exception) -> RemediatedError | None:
    """Translate team-edition boot/request-time failures into the SAME
    ``RemediatedError`` shape
    ``frisket.llm.remediation.classify_llm_error`` uses for model-call
    errors. Returns ``None`` for anything not in the seeded set -- callers
    fall back to their own generic handling (re-raise), exactly like
    ``classify_resumable_provider_error``'s opt-in contract.

    Cross-model review fix: the first cut classified EVERY ``OSError`` as
    an unreachable SMTP host. ``smtplib``'s recipient/sender/data rejections
    (``SMTPRecipientsRefused``, ``SMTPSenderRefused``, ``SMTPDataError``,
    ...) are NOT ``OSError`` subclasses so those slipped through as
    "unreachable" too via the ``smtplib.SMTPException`` half of the old
    check -- and, worse, genuinely unrelated ``OSError`` subclasses
    (``FileNotFoundError``, ``PermissionError``) were swallowed into the
    same false DNS/refusal diagnosis. Narrowed to two classes with distinct,
    accurate remediation:

    - ``smtplib.SMTPAuthenticationError`` -- the host answered; the
      credentials didn't. A distinct "login failed" classification, not
      "unreachable" (checked first: it's also unrelated to the
      connection-failure set below).
    - ``ConnectionError`` / ``socket.gaierror`` / ``TimeoutError`` -- a real
      network-level failure (refused, DNS, or timed out). The original
      "unreachable" classification.

    Everything else (SMTP recipient/sender/data rejections,
    ``FileNotFoundError``, ``PermissionError``, or any other exception)
    returns ``None`` and is unclassified -- the caller re-raises rather than
    mislabeling a failure this catalog doesn't actually know how to explain.

    Only classes with a live, reachable exception site are pattern-matched
    here (SMTP transport failures at magic-link request time); PEP 668 and
    the hosted-worker misconfiguration are represented in
    ``BOOT_FAILURE_CATALOG`` for reference/testing but have no exception to
    classify from this seam (PEP 668 never reaches Python; the hosted-worker
    case is handled directly at its CLI call site, cli.py's ``_run_worker``,
    which has the argv context this classifier does not)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        entry = BOOT_FAILURE_CATALOG[MAGIC_LINK_SMTP_LOGIN_FAILED]
        return RemediatedError(
            code=MAGIC_LINK_SMTP_LOGIN_FAILED,
            message=f"{entry['what']} {entry['todo']}",
            details={"route": entry["route"]},
        )
    if isinstance(exc, (ConnectionError, socket.gaierror, TimeoutError)):
        entry = BOOT_FAILURE_CATALOG[MAGIC_LINK_SMTP_UNREACHABLE]
        return RemediatedError(
            code=MAGIC_LINK_SMTP_UNREACHABLE,
            message=f"{entry['what']} {entry['todo']}",
            details={"route": entry["route"]},
        )
    return None


__all__ = [
    "sanitize_text",
    "classify_team_boot_error",
    "BOOT_FAILURE_CATALOG",
    "PEP668_NO_PIP",
    "PYICU_TOOLCHAIN_MISSING",
    "HOSTED_WORKER_WRONG_MODE",
    "MAGIC_LINK_SMTP_UNREACHABLE",
    "MAGIC_LINK_SMTP_LOGIN_FAILED",
    "PLAIN_HTTP_BASE_URL_REJECTED",
]
