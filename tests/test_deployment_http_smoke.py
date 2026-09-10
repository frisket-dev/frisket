from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tomllib
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts.smoke import deployment_http_smoke as smoke
from tests.deterministic_time import ControlledTime, controlled_time


ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


class _Scenario:
    claimed = False
    code_version = "a" * 40
    email = smoke.DEFAULT_EMAIL
    password = "correct-horse-battery-smoke"
    project_id = "deterministic-deployment-smoke-abc123"
    sheet_id = 1
    run_id = 7
    job_id = 9
    receipt_id = "receipt_deployment_smoke"
    redirect_setup_to: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(
        self,
        status: int,
        value: Any = None,
        *,
        content_type: str = "application/json",
        cookie: str | None = None,
    ) -> None:
        if content_type == "application/json":
            body = json.dumps(value).encode()
        else:
            body = str(value or "").encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _redirect(self, location: str, *, cookie: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _authenticated(self) -> bool:
        return "frisket_session=valid" in self.headers.get("Cookie", "")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        scenario = self.server.scenario
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/ready":
            self._send(
                200,
                {
                    "schema_version": smoke.READY_SCHEMA,
                    "identity": {
                        "schema_version": smoke.IDENTITY_SCHEMA,
                        "package_version": VERSION,
                        "code_version": scenario.code_version,
                    },
                    "ok": True,
                    "failures": [],
                },
            )
            return
        if path == "/setup":
            if scenario.claimed:
                self._send(404, {"detail": "setup is unavailable"})
            else:
                self._send(200, "setup", content_type="text/html")
            return
        if path == "/":
            self._send(200, "frisket", content_type="text/html")
            return
        if not self._authenticated():
            self._send(401, {"detail": "authentication required"})
            return
        if path == f"/api/projects/{scenario.project_id}":
            self._send(200, {"id": scenario.project_id, "name": "smoke"})
        elif path == (
            f"/api/projects/{scenario.project_id}/actions/runs/{scenario.run_id}/status"
        ):
            self._send(
                200,
                {
                    "run": {
                        "public_status": {
                            "status": "completed",
                            "action_kind": smoke.ACTION_KIND,
                            "total": 1,
                            "completed": 1,
                            "failed": 0,
                        }
                    }
                },
            )
        elif path == (
            f"/api/projects/{scenario.project_id}/actions/v1/receipts/"
            f"{scenario.receipt_id}"
        ):
            self._send(
                200,
                {
                    "receipt_id": scenario.receipt_id,
                    "project_id": scenario.project_id,
                    "run_id": scenario.run_id,
                    "action_kind": smoke.ACTION_KIND,
                    "status": "completed",
                },
            )
        elif path == (
            f"/api/projects/{scenario.project_id}/sheets/{scenario.sheet_id}/data"
        ):
            self._send(
                200,
                {
                    "columns": [
                        {"id": 1, "name": "note"},
                        {"id": 2, "name": "phone"},
                    ],
                    "rows": [
                        {
                            "id": 1,
                            "cells": {
                                "1": "Call 212-555-0123",
                                "2": smoke.EXPECTED_PHONE,
                            },
                        }
                    ],
                    "total": 1,
                },
            )
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        scenario = self.server.scenario
        path = urllib.parse.urlsplit(self.path).path
        body = self._body()
        if path == "/setup":
            if scenario.redirect_setup_to is not None:
                self.send_response(307)
                self.send_header("Location", f"{scenario.redirect_setup_to}/setup")
                self.end_headers()
                return
            form = urllib.parse.parse_qs(body.decode())
            valid = (
                self.headers.get("Origin") == self.server.origin
                and form.get("claim_token") == ["setup-code"]
                and form.get("password") == [scenario.password]
                and form.get("password_confirmation") == [scenario.password]
            )
            if not valid or scenario.claimed:
                # Deliberately emulate a bad/misrouted server that echoes the
                # submitted secret-bearing form. The smoke client must never
                # copy this response body into its error output.
                self._send(403, {"detail": body.decode()})
                return
            scenario.claimed = True
            self._redirect("/", cookie="frisket_session=setup; Path=/")
            return
        if path == "/auth/password-login":
            form = json.loads(body)
            valid = (
                self.headers.get("Origin") == self.server.origin
                and form.get("email") == scenario.email
                and form.get("password") == scenario.password
            )
            if not valid:
                self._send(401, {"detail": "invalid login"})
                return
            self._send(
                200,
                {"ok": True},
                cookie="frisket_session=valid; Path=/",
            )
            return
        if not self._authenticated():
            self._send(401, {"detail": "authentication required"})
            return
        if path == "/api/projects":
            request = json.loads(body)
            assert request["name"] == "Deterministic Deployment Smoke"
            self._send(200, {"id": scenario.project_id})
        elif path == f"/api/projects/{scenario.project_id}/import/csv":
            assert self.headers.get("Content-Type", "").startswith(
                "multipart/form-data; boundary=frisket-smoke-"
            )
            assert b"212-555-0123" in body
            self._send(200, {"sheet_id": scenario.sheet_id, "rows": 1})
        elif path == f"/api/projects/{scenario.project_id}/actions/v1/run":
            request = json.loads(body)
            assert set(request) == {
                "action_id",
                "scope",
                "params",
                "output_names",
                "idempotency_key",
            }
            assert request["action_id"] == smoke.ACTION_KIND
            assert request["scope"] == {
                "kind": "sheet_rows",
                "sheet_id": scenario.sheet_id,
            }
            assert request["params"]["pattern"] == r"\d{3}-\d{3}-\d{4}"
            assert request["params"]["input_columns"] == ["note"]
            assert request["output_names"] == {"extracted": "phone"}
            assert request["idempotency_key"] == ("deployment-smoke/regex@sha256:v1")
            self._send(
                200,
                {
                    "status": "queued",
                    "run_id": scenario.run_id,
                    "job_id": scenario.job_id,
                    "receipt_id": scenario.receipt_id,
                },
            )
        else:
            self._send(404, {"detail": "not found"})


class _Server(ThreadingHTTPServer):
    scenario: _Scenario
    origin: str


@contextmanager
def _server(t: ControlledTime) -> Iterator[tuple[str, _Scenario]]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.scenario = _Scenario()
    host, port = server.server_address
    server.origin = f"http://{host}:{port}"
    t.background(server.serve_forever)
    try:
        yield server.origin, server.scenario
    finally:
        server.shutdown()
        server.server_close()


def test_seed_then_fresh_login_verify_round_trip(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "smoke-state.json"
    monkeypatch.setenv(smoke.SETUP_CODE_ENV, "setup-code")
    monkeypatch.setenv(smoke.DEFAULT_PASSWORD_ENV, _Scenario.password)
    with controlled_time() as t, _server(t) as (origin, _scenario):
        assert (
            smoke.main(
                [
                    "seed",
                    "--base-url",
                    origin,
                    "--state-file",
                    str(state_file),
                    "--ready-timeout",
                    "2",
                    "--action-timeout",
                    "2",
                ]
            )
            == 0
        )
        monkeypatch.delenv(smoke.SETUP_CODE_ENV)
        assert (
            smoke.main(
                [
                    "verify",
                    "--base-url",
                    origin,
                    "--state-file",
                    str(state_file),
                    "--ready-timeout",
                    "2",
                ]
            )
            == 0
        )

    mode = stat.S_IMODE(state_file.stat().st_mode)
    assert mode == 0o600
    state = json.loads(state_file.read_text())
    assert state["schema_version"] == smoke.STATE_SCHEMA
    assert state["readiness_identity"]["code_version"] == "a" * 40
    assert "password" not in state
    assert "setup" not in state


def test_verify_rejects_changed_runtime_identity(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    state_file = tmp_path / "smoke-state.json"
    monkeypatch.setenv(smoke.SETUP_CODE_ENV, "setup-code")
    monkeypatch.setenv(smoke.DEFAULT_PASSWORD_ENV, _Scenario.password)
    with controlled_time() as t, _server(t) as (origin, scenario):
        assert (
            smoke.main(["seed", "--base-url", origin, "--state-file", str(state_file)])
            == 0
        )
        scenario.code_version = "b" * 40
        assert (
            smoke.main(
                ["verify", "--base-url", origin, "--state-file", str(state_file)]
            )
            == 1
        )

    assert (
        "runtime package/code identity changed across restart"
        in capsys.readouterr().err
    )


def test_setup_failure_never_prints_echoed_form_secrets(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    setup_code = "setup-code-that-must-not-leak"
    password = "password-that-must-not-leak"
    monkeypatch.setenv(smoke.SETUP_CODE_ENV, setup_code)
    monkeypatch.setenv(smoke.DEFAULT_PASSWORD_ENV, password)
    with controlled_time() as t, _server(t) as (origin, _scenario):
        assert (
            smoke.main(
                [
                    "seed",
                    "--base-url",
                    origin,
                    "--state-file",
                    str(tmp_path / "state.json"),
                ]
            )
            == 1
        )
    error = capsys.readouterr().err
    assert "POST /setup returned HTTP 403" in error
    assert setup_code not in error
    assert password not in error


def test_seed_rejects_unknown_release_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(smoke.SETUP_CODE_ENV, "setup-code")
    monkeypatch.setenv(smoke.DEFAULT_PASSWORD_ENV, _Scenario.password)
    with controlled_time() as t, _server(t) as (origin, scenario):
        scenario.code_version = "unknown"
        assert (
            smoke.main(
                [
                    "seed",
                    "--base-url",
                    origin,
                    "--state-file",
                    str(tmp_path / "state.json"),
                ]
            )
            == 1
        )


def test_secret_form_rejects_cross_origin_redirect_without_replay(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    setup_code = "setup-code"
    password = _Scenario.password
    monkeypatch.setenv(smoke.SETUP_CODE_ENV, setup_code)
    monkeypatch.setenv(smoke.DEFAULT_PASSWORD_ENV, password)
    with controlled_time() as t, _server(t) as (target_origin, target_scenario):
        with _server(t) as (source_origin, source_scenario):
            source_scenario.redirect_setup_to = target_origin
            assert (
                smoke.main(
                    [
                        "seed",
                        "--base-url",
                        source_origin,
                        "--state-file",
                        str(tmp_path / "state.json"),
                    ]
                )
                == 1
            )
        assert target_scenario.claimed is False
    error = capsys.readouterr().err
    assert "refused a cross-origin HTTP redirect" in error
    assert setup_code not in error
    assert password not in error


def test_release_bundle_wrapper_refuses_a_parallel_fixed_project_smoke(
    tmp_path: Path,
) -> None:
    fcntl = pytest.importorskip("fcntl")
    if shutil.which("flock") is None:
        pytest.skip("flock is unavailable")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n")
    fake_docker.chmod(0o755)
    bundle = tmp_path / "candidate.tar.gz"
    bundle.touch()
    wrapper = Path(__file__).parents[1] / "scripts/smoke/smoke_release_bundle.sh"

    with Path("/tmp/frisket-release-smoke.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["sh", str(wrapper), str(bundle)],
            env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "TMPDIR": str(tmp_path),
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 1
    assert "another release-bundle smoke already owns" in result.stderr
