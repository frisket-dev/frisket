"""Local tier serves the built web UI — one command, one URL.

`frisket <workspace>` must be a complete experience: the local server
(`frisket.server.app.create_app`) mounts the built SPA the same way the hosted
app does, so a packaged pip/uvx install boots a real UI on :8000 instead of a
headless API. This pins:

  * the shared static-serving primitive extracted from the hosted app
    (`frisket.server.static_serving`),
  * the resolution order explicit FRISKET_STATIC_DIR -> packaged assets -> none
    (dev mode, where vite serves the UI and must keep working),
  * API-route precedence with SPA fallback for client-history routes, and
  * the doctor / CLI human description of the resolved state.

Red-first for onboard-local-ui-serving-v1 (2026-07-03).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server import static_serving
from frisket.server.app import create_app


async def _raw_asgi_get(app: Any, path: str) -> int:
    """GET `path` over a hand-built ASGI scope, bypassing httpx's client-side
    collapse of a literal ".." path component (real HTTP clients/servers do
    not collapse it -- see tests/test_team_static_serving.py's copy of this
    helper for the full rationale)."""
    status_holder: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "scheme": "http",
        "root_path": "",
    }
    await app(scope, receive, send)
    return status_holder["status"]


def _raw_status(app: Any, path: str) -> int:
    return asyncio.run(_raw_asgi_get(app, path))


def _make_static_dir(root: Path) -> Path:
    static_dir = root / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<html><head></head><body>frisket local app</body></html>",
        encoding="utf-8",
    )
    (static_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    assets = static_dir / "assets"
    assets.mkdir()
    (assets / "index-ABCD1234.js").write_text(
        "console.log('frisket')", encoding="utf-8"
    )
    (assets / "plain.js").write_text("console.log('plain')", encoding="utf-8")
    return static_dir


# --------------------------------------------------------------------------
# resolution order: explicit env -> packaged assets -> none
# --------------------------------------------------------------------------


def test_resolve_prefers_explicit_env_over_packaged(tmp_path, monkeypatch):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    # even if packaged assets exist, the explicit env wins
    packaged = _make_static_dir(tmp_path / "packaged_root")
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: packaged)
    resolved = static_serving.resolve_static_dir()
    assert resolved is not None
    assert Path(resolved).resolve() == static_dir.resolve()


def test_resolve_falls_back_to_packaged_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    packaged = _make_static_dir(tmp_path / "packaged_root")
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: packaged)
    resolved = static_serving.resolve_static_dir()
    assert resolved is not None
    assert Path(resolved).resolve() == packaged.resolve()


def test_resolve_allow_packaged_fallback_false_skips_straight_to_none(
    tmp_path, monkeypatch
):
    # The team tier passes allow_packaged_fallback=False: packaged_static_dir()
    # is always the LOCAL edition's build, so a caller for a different
    # edition must not silently pick it up just because no explicit
    # FRISKET_STATIC_DIR/directory was given.
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    packaged = _make_static_dir(tmp_path / "packaged_root")
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: packaged)
    assert static_serving.resolve_static_dir() == packaged
    assert static_serving.resolve_static_dir(allow_packaged_fallback=False) is None


def test_resolve_allow_packaged_fallback_false_still_honors_explicit_env(
    tmp_path, monkeypatch
):
    def _must_not_be_called() -> Path | None:
        raise AssertionError("packaged_static_dir must not be consulted")

    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    monkeypatch.setattr(static_serving, "packaged_static_dir", _must_not_be_called)
    resolved = static_serving.resolve_static_dir(allow_packaged_fallback=False)
    assert resolved is not None
    assert Path(resolved).resolve() == static_dir.resolve()


def test_resolve_ignores_nonexistent_env_dir(tmp_path, monkeypatch):
    # a stale/typo'd FRISKET_STATIC_DIR must not be mounted; fall through
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: None)
    assert static_serving.resolve_static_dir() is None


def test_resolve_returns_none_in_dev_mode(monkeypatch):
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: None)
    assert static_serving.resolve_static_dir() is None


# --------------------------------------------------------------------------
# mount + API precedence + SPA fallback against a fixture static dir
# --------------------------------------------------------------------------


def test_local_app_serves_spa_with_api_precedence(tmp_path, monkeypatch):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", static_dir=static_dir)
    client = TestClient(app)

    # index at root
    index = client.get("/")
    assert index.status_code == 200
    assert "frisket local app" in index.text

    # static asset served directly
    assert client.get("/favicon.svg").text == "<svg/>"

    # hashed asset gets the immutable long-cache header
    hashed = client.get("/assets/index-ABCD1234.js")
    assert hashed.status_code == 200
    assert hashed.headers["cache-control"] == "public, max-age=31536000, immutable"

    # SPA fallback for a client-history browser route
    route = client.get("/p/demo/s/1/review")
    assert route.status_code == 200
    assert "frisket local app" in route.text

    # API routes take precedence over the SPA mount
    health = client.get("/api/health")
    assert health.status_code == 200
    # ok-is-True, not exact-dict: health carries additive blocks (queue
    # staleness from onboard-queue-health); this test pins API precedence.
    assert health.json()["ok"] is True
    assert "text/html" not in health.headers.get("content-type", "")

    # unknown API path must NOT fall back to index.html
    missing_api = client.get("/api/no-such-route")
    assert missing_api.status_code == 404
    assert "frisket local app" not in missing_api.text


def test_local_app_serves_non_enumerated_client_routes_not_just_an_allowlist(
    tmp_path, monkeypatch
):
    # Regression: SPAStaticFiles._is_browser_route() used to check the
    # first path segment against a hardcoded root allowlist -- every route
    # known *at the time*, closed against an open set of real client
    # routes. Any extensionless, non-reserved, nonexistent path must get
    # the SPA fallback, not just an enumerated few (the /apiary-style probe
    # below is NOT under the reserved /api namespace -- component-bounded,
    # "apiary" != "api" -- so it must reach the fallback too).
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", static_dir=static_dir)
    client = TestClient(app)

    for path in ("/apiary", "/apiary/demo", "/authors", "/some-future-route"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "frisket local app" in response.text, path

    # a real asset still serves directly, not the fallback
    assert client.get("/favicon.svg").text == "<svg/>"

    # /api is still a real 404 via the reserved-namespace guard
    assert client.get("/api/no-such-route").status_code == 404

    # a missing static file with an extension is a real 404, not the SPA
    assert client.get("/missing.js").status_code == 404


def test_local_app_dev_mode_is_api_only(tmp_path, monkeypatch):
    # dev mode: no static dir, no packaged assets -> API-only, vite serves UI
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: None)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws")
    client = TestClient(app)

    # API still works
    assert client.get("/api/health").json()["ok"] is True
    # no SPA mount: root is not served by the backend
    assert client.get("/").status_code == 404


def test_serve_spa_false_never_mounts_even_with_static_configured(
    tmp_path, monkeypatch
):
    # Embedders that own the browser-facing mount (hosted per-tenant sub-apps)
    # opt out; a process-wide FRISKET_STATIC_DIR must not leak an inner mount.
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", serve_spa=False)
    client = TestClient(app)

    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/").status_code == 404
    assert client.get("/p/demo/s/1").status_code == 404


# --------------------------------------------------------------------------
# reserved-namespace guard (frisket.server.static_serving._NamespaceGuardedMount)
# --------------------------------------------------------------------------


def test_local_app_declines_dot_segments_under_api_namespace(tmp_path, monkeypatch):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", static_dir=static_dir)

    # raw ".." component (curl --path-as-is / a real ASGI server's verbatim
    # path -- httpx client-side-collapses a literal ".." before sending, so
    # this goes through the raw-scope harness, not TestClient)
    assert _raw_status(app, "/api/../assets/index-ABCD1234.js") == 404
    assert _raw_status(app, "/api/../index.html") == 404

    # percent-encoded ".." (httpx does NOT collapse this client-side)
    client = TestClient(app)
    assert client.get("/api/%2e%2e/assets/index-ABCD1234.js").status_code == 404


def test_local_app_api_namespace_shadows_a_colliding_real_static_file(
    tmp_path, monkeypatch
):
    static_dir = _make_static_dir(tmp_path)
    (static_dir / "api").mkdir()
    (static_dir / "api" / "no-such-route").write_text("collision", encoding="utf-8")
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/api/no-such-route")
    assert response.status_code == 404
    assert "collision" not in response.text


def test_local_app_restores_native_405_and_redirect_slash_for_api_routes(
    tmp_path, monkeypatch
):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "ws", static_dir=static_dir)
    client = TestClient(app, follow_redirects=False)

    # trailing-slash redirect restored (the unconditional mount used to
    # swallow this and 404 instead)
    redirect = client.get("/api/health/")
    assert redirect.status_code == 307
    assert redirect.headers["location"].endswith("/api/health")

    # wrong method on a real route -> 405, not StaticFiles' 404/wrong body
    assert client.request("DELETE", "/api/health").status_code == 405

    # unknown method on an unmatched path -> 404, not StaticFiles' 405
    assert client.post("/api/no-such-route").status_code == 404


# --------------------------------------------------------------------------
# human description of resolved state (doctor / CLI startup)
# --------------------------------------------------------------------------


def test_describe_static_source_reports_explicit_env(tmp_path, monkeypatch):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    desc = static_serving.describe_static_source()
    assert str(static_dir) in desc


def test_describe_static_source_reports_dev_mode(monkeypatch):
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: None)
    desc = static_serving.describe_static_source()
    assert "vite" in desc.lower() or "dev" in desc.lower()


def test_describe_static_source_reports_packaged(tmp_path, monkeypatch):
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    packaged = _make_static_dir(tmp_path / "packaged_root")
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: packaged)
    desc = static_serving.describe_static_source()
    assert "packaged" in desc.lower()
