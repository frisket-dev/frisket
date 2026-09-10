#!/usr/bin/env python3
"""Provider-neutral HTTP smoke for a fresh Frisket Server deployment.

``seed`` claims an unconfigured server and exercises a deterministic queued
action.  ``verify`` starts with a new login session and proves the resulting
project, output, run, and receipt survived a service restart.  Lifecycle is
deliberately outside this script: a Compose host, PaaS, VM, or test harness can
restart the service between the two commands without changing the HTTP proof.

Secrets are accepted only through ``FRISKET_SMOKE_SETUP_CODE`` and
``FRISKET_SMOKE_PASSWORD``.  The state file contains IDs and expected public
values, never either secret.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


STATE_SCHEMA = "frisket.deployment_http_smoke.v1"
READY_SCHEMA = "frisket.server_readiness.v1"
IDENTITY_SCHEMA = "frisket.server_identity.v1"
ACTION_KIND = "map.regex_extract"
EXPECTED_PHONE = "212-555-0123"
DEFAULT_EMAIL = "deployment-smoke@example.com"
DEFAULT_PASSWORD_ENV = "FRISKET_SMOKE_PASSWORD"
SETUP_CODE_ENV = "FRISKET_SMOKE_SETUP_CODE"
TERMINAL_RUN_STATES = frozenset({"completed", "partial", "failed", "cancelled"})


class SmokeError(RuntimeError):
    """A bounded, operator-readable deployment smoke failure."""


def _url_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SmokeError("URL contains an invalid port") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SmokeError("URL must use http(s) with a host and no credentials")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _origin(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SmokeError(
            "--base-url must be an http(s) origin with no path or credentials"
        )
    return _url_origin(candidate)


def _bounded_response(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:500] + ("..." if len(text) > 500 else "")


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        super().__init__()
        self.origin = origin

    def redirect_request(  # type: ignore[override]
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(request.full_url, new_url)
        if _url_origin(target) != self.origin:
            raise SmokeError("refused a cross-origin HTTP redirect")
        # Only 303 may discard a browser-form body; never replay writes through
        # another redirect, even to the same host.
        if request.get_method() != "GET" and code != 303:
            raise SmokeError(f"refused HTTP {code} redirect for a write request")
        return super().redirect_request(
            request, file_pointer, code, message, headers, target
        )


class HttpSession:
    """Small cookie-aware JSON/form client using only the Python standard library."""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self.base_url = _origin(base_url)
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _SameOriginRedirectHandler(self.base_url),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: frozenset[int] = frozenset({200}),
        include_error_body: bool = True,
    ) -> tuple[int, bytes, dict[str, str]]:
        if not path.startswith("/"):
            raise SmokeError(f"internal smoke path must be absolute: {path!r}")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            if _url_origin(response.geturl()) != self.base_url:
                raise SmokeError("HTTP response escaped the configured origin")
            status = int(response.status)
            payload = response.read()
            response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if _url_origin(exc.geturl()) != self.base_url:
                raise SmokeError(
                    "HTTP error response escaped the configured origin"
                ) from exc
            status = int(exc.code)
            payload = exc.read()
            response_headers = dict(exc.headers.items())
        except (OSError, urllib.error.URLError) as exc:
            raise SmokeError(f"{method} {path} could not reach Frisket: {exc}") from exc
        if status not in expected:
            detail = _bounded_response(payload) if include_error_body else ""
            suffix = f": {detail}" if detail else ""
            raise SmokeError(f"{method} {path} returned HTTP {status}{suffix}")
        return status, payload, response_headers

    def json(
        self,
        method: str,
        path: str,
        *,
        value: Any | None = None,
        expected: frozenset[int] = frozenset({200}),
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if value is not None:
            body = json.dumps(value, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        _, payload, _ = self.request(
            method, path, body=body, headers=headers, expected=expected
        )
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError(f"{method} {path} did not return JSON") from exc

    def form(self, path: str, values: dict[str, str]) -> None:
        body = urllib.parse.urlencode(values).encode()
        self.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base_url,
            },
            # Never expose a secret-bearing form response in operator output.
            include_error_body=False,
        )

    def upload_csv(self, path: str, *, filename: str, contents: bytes) -> Any:
        boundary = f"frisket-smoke-{secrets.token_hex(16)}"
        marker = boundary.encode("ascii")
        body = b"\r\n".join(
            (
                b"--" + marker,
                (
                    b'Content-Disposition: form-data; name="file"; filename="'
                    + filename.encode("ascii")
                    + b'"'
                ),
                b"Content-Type: text/csv",
                b"",
                contents,
                b"--" + marker + b"--",
                b"",
            )
        )
        _, payload, _ = self.request(
            "POST",
            path,
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError(f"POST {path} did not return JSON") from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeError(f"{label} was not a JSON object")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmokeError(f"{label} was missing or invalid")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SmokeError(f"{label} was missing or invalid")
    return value


def wait_ready(session: HttpSession, *, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    last_error = "server did not respond"
    while time.monotonic() < deadline:
        try:
            report = _require_mapping(session.json("GET", "/api/ready"), "readiness")
            if report.get("schema_version") != READY_SCHEMA:
                raise SmokeError("readiness returned an unexpected schema version")
            if report.get("ok") is True:
                return report
            last_error = f"readiness failures: {report.get('failures', [])!r}"
        except SmokeError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise SmokeError(
        f"Frisket did not become ready within {deadline_seconds:g}s: {last_error}"
    )


def _readiness_identity(report: dict[str, Any]) -> dict[str, str]:
    identity = _require_mapping(report.get("identity"), "readiness.identity")
    if identity.get("schema_version") != IDENTITY_SCHEMA:
        raise SmokeError("readiness identity returned an unexpected schema version")
    result = {
        "schema_version": IDENTITY_SCHEMA,
        "package_version": _require_nonempty_string(
            identity.get("package_version"), "readiness.identity.package_version"
        ),
        "code_version": _require_nonempty_string(
            identity.get("code_version"), "readiness.identity.code_version"
        ),
    }
    if any(value.strip().lower() == "unknown" for value in result.values()):
        raise SmokeError("readiness identity contains an unknown value")
    return result


def _login(base_url: str, *, email: str, password: str, timeout: float) -> HttpSession:
    session = HttpSession(base_url, timeout=timeout)
    session.request(
        "POST",
        "/auth/password-login",
        body=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json", "Origin": session.base_url},
        include_error_body=False,
    )
    if not list(session.cookies):
        raise SmokeError("password login returned no session cookie")
    return session


def _setup_is_closed(session: HttpSession) -> None:
    session.request("GET", "/setup", expected=frozenset({404}))


def _poll_completed_run(
    session: HttpSession,
    *,
    project_id: str,
    run_id: int,
    deadline_seconds: float,
) -> dict[str, Any]:
    path = f"/api/projects/{urllib.parse.quote(project_id, safe='')}/actions/runs/{run_id}/status"
    deadline = time.monotonic() + deadline_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        payload = _require_mapping(session.json("GET", path), "run status")
        run = _require_mapping(payload.get("run"), "run status.run")
        public = _require_mapping(run.get("public_status"), "run public status")
        last_status = str(public.get("status") or "unknown")
        if last_status in TERMINAL_RUN_STATES:
            if last_status != "completed":
                raise SmokeError(
                    f"queued {ACTION_KIND} run ended as {last_status}: "
                    f"{public.get('error') or public.get('row_errors') or 'no detail'}"
                )
            if (
                public.get("action_kind") != ACTION_KIND
                or public.get("total") != 1
                or public.get("completed") != 1
                or public.get("failed") != 0
            ):
                raise SmokeError(
                    "queued run completed with unexpected action/count metadata"
                )
            return payload
        time.sleep(0.5)
    raise SmokeError(
        f"queued {ACTION_KIND} run did not finish within {deadline_seconds:g}s "
        f"(last status {last_status})"
    )


def _verify_persisted_evidence(session: HttpSession, state: dict[str, Any]) -> None:
    project_id = _require_nonempty_string(state.get("project_id"), "state.project_id")
    sheet_id = _require_positive_int(state.get("sheet_id"), "state.sheet_id")
    run_id = _require_positive_int(state.get("run_id"), "state.run_id")
    receipt_id = _require_nonempty_string(state.get("receipt_id"), "state.receipt_id")

    project = _require_mapping(
        session.json("GET", f"/api/projects/{urllib.parse.quote(project_id, safe='')}"),
        "persisted project",
    )
    if project.get("id") != project_id:
        raise SmokeError("persisted project identity did not match smoke state")

    status = _require_mapping(
        session.json(
            "GET",
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/actions/runs/{run_id}/status",
        ),
        "persisted run status",
    )
    public = _require_mapping(
        _require_mapping(status.get("run"), "persisted run").get("public_status"),
        "persisted public run status",
    )
    if (
        public.get("status") != "completed"
        or public.get("action_kind") != ACTION_KIND
        or public.get("completed") != 1
        or public.get("failed") != 0
    ):
        raise SmokeError("completed queued run did not persist across restart")

    receipt = _require_mapping(
        session.json(
            "GET",
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/actions/v1/receipts/"
            f"{urllib.parse.quote(receipt_id, safe='')}",
        ),
        "persisted receipt",
    )
    if (
        receipt.get("receipt_id") != receipt_id
        or receipt.get("project_id") != project_id
        or receipt.get("run_id") != run_id
        or receipt.get("action_kind") != ACTION_KIND
        or receipt.get("status") != "completed"
    ):
        raise SmokeError(
            "completed action receipt did not persist or match the queued run"
        )

    grid = _require_mapping(
        session.json(
            "GET",
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/sheets/{sheet_id}/data",
        ),
        "persisted sheet",
    )
    columns = grid.get("columns")
    rows = grid.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or len(rows) != 1:
        raise SmokeError("persisted smoke sheet did not contain exactly one row")
    phone_column = next(
        (
            column
            for column in columns
            if isinstance(column, dict) and column.get("name") == "phone"
        ),
        None,
    )
    if not isinstance(phone_column, dict):
        raise SmokeError("queued regex output column did not persist")
    column_id = phone_column.get("id")
    cells = rows[0].get("cells") if isinstance(rows[0], dict) else None
    if not isinstance(cells, dict) or cells.get(str(column_id)) != EXPECTED_PHONE:
        raise SmokeError("queued regex output value did not persist")


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path = path.expanduser()
    if path.exists() or path.is_symlink():
        raise SmokeError(f"state file already exists; refusing to overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
        state = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"could not read smoke state {path}: {exc}") from exc
    result = _require_mapping(state, "smoke state")
    if result.get("schema_version") != STATE_SCHEMA:
        raise SmokeError("smoke state has an unsupported schema version")
    return result


def seed(args: argparse.Namespace) -> dict[str, Any]:
    base_url = _origin(args.base_url)
    setup_code = os.environ.get(SETUP_CODE_ENV, "")
    password = os.environ.get(DEFAULT_PASSWORD_ENV, "")
    if not setup_code:
        raise SmokeError(f"{SETUP_CODE_ENV} is required for seed")
    if len(password) < 12:
        raise SmokeError(f"{DEFAULT_PASSWORD_ENV} must contain at least 12 characters")

    claim_session = HttpSession(base_url, timeout=args.request_timeout)
    readiness_identity = _readiness_identity(
        wait_ready(claim_session, deadline_seconds=args.ready_timeout)
    )
    claim_session.form(
        "/setup",
        {
            "claim_token": setup_code,
            "workspace_name": args.workspace_name,
            "owner_name": args.owner_name,
            "email": args.email,
            "password": password,
            "password_confirmation": password,
        },
    )
    _setup_is_closed(claim_session)

    # A fresh cookie jar proves ordinary login before data creation.
    session = _login(
        base_url, email=args.email, password=password, timeout=args.request_timeout
    )
    project = _require_mapping(
        session.json("POST", "/api/projects", value={"name": args.project_name}),
        "created project",
    )
    project_id = _require_nonempty_string(project.get("id"), "created project.id")
    imported = _require_mapping(
        session.upload_csv(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/import/csv",
            filename="deployment-smoke.csv",
            contents=(
                b'note\n"Call 212-555-0123 for deterministic deployment proof"\n'
            ),
        ),
        "CSV import",
    )
    if imported.get("rows") != 1:
        raise SmokeError("CSV import did not create exactly one row")
    sheet_id = _require_positive_int(imported.get("sheet_id"), "CSV import.sheet_id")

    action = {
        "action_id": ACTION_KIND,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
        "output_names": {"extracted": "phone"},
        "idempotency_key": "deployment-smoke/regex@sha256:v1",
    }
    queued = _require_mapping(
        session.json(
            "POST",
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/actions/v1/run",
            value=action,
        ),
        "queued action response",
    )
    if queued.get("status") != "queued":
        raise SmokeError("deterministic action was not queued")
    run_id = _require_positive_int(queued.get("run_id"), "queued action.run_id")
    job_id = _require_positive_int(queued.get("job_id"), "queued action.job_id")
    receipt_id = _require_nonempty_string(
        queued.get("receipt_id"), "queued action.receipt_id"
    )
    _poll_completed_run(
        session,
        project_id=project_id,
        run_id=run_id,
        deadline_seconds=args.action_timeout,
    )
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA,
        "base_url": base_url,
        "email": args.email,
        "project_id": project_id,
        "sheet_id": sheet_id,
        "run_id": run_id,
        "job_id": job_id,
        "receipt_id": receipt_id,
        "action_kind": ACTION_KIND,
        "expected_phone": EXPECTED_PHONE,
        "readiness_identity": readiness_identity,
    }
    _verify_persisted_evidence(session, state)
    _write_state(args.state_file, state)
    return state


def verify(args: argparse.Namespace) -> dict[str, Any]:
    base_url = _origin(args.base_url)
    password = os.environ.get(DEFAULT_PASSWORD_ENV, "")
    if len(password) < 12:
        raise SmokeError(f"{DEFAULT_PASSWORD_ENV} must contain at least 12 characters")
    state = _read_state(args.state_file)
    if state.get("base_url") != base_url:
        raise SmokeError("--base-url does not match the origin recorded by seed")
    email = _require_nonempty_string(state.get("email"), "state.email")

    probe = HttpSession(base_url, timeout=args.request_timeout)
    readiness_identity = _readiness_identity(
        wait_ready(probe, deadline_seconds=args.ready_timeout)
    )
    stored_identity = _require_mapping(
        state.get("readiness_identity"), "state.readiness_identity"
    )
    if readiness_identity != stored_identity:
        raise SmokeError("runtime package/code identity changed across restart")
    _setup_is_closed(probe)
    session = _login(
        base_url, email=email, password=password, timeout=args.request_timeout
    )
    _verify_persisted_evidence(session, state)
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("seed", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--base-url", required=True)
        child.add_argument("--state-file", required=True, type=Path)
        child.add_argument("--request-timeout", type=float, default=15.0)
        child.add_argument("--ready-timeout", type=float, default=120.0)
    seed_parser = subparsers.choices["seed"]
    seed_parser.add_argument("--action-timeout", type=float, default=120.0)
    seed_parser.add_argument("--email", default=DEFAULT_EMAIL)
    seed_parser.add_argument("--workspace-name", default="Deployment Smoke Workspace")
    seed_parser.add_argument("--owner-name", default="Deployment Smoke Owner")
    seed_parser.add_argument("--project-name", default="Deterministic Deployment Smoke")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seed":
            state = seed(args)
            print(
                "frisket-smoke: seed passed "
                f"project={state['project_id']} run={state['run_id']} "
                f"receipt={state['receipt_id']}"
            )
        else:
            state = verify(args)
            print(
                "frisket-smoke: persistence passed "
                f"project={state['project_id']} run={state['run_id']} "
                f"receipt={state['receipt_id']}"
            )
    except SmokeError as exc:
        print(f"frisket-smoke: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
