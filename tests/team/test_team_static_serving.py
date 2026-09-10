"""The self-hosted team image serves its own built web UI.

`frisket.team.app.create_team_app` mounts the built SPA the same way the
local tier and the private hosted layer do (`frisket.server.static_serving`),
so the shipped `frisket-team` image (Dockerfile sets
`FRISKET_STATIC_DIR=/app/static/team`) serves a real UI on :8000 instead of
a headless API-only server. This pins:

  * the resolution order (`FRISKET_STATIC_DIR` -> packaged -> none/dev mode)
    reused as-is from the local tier,
  * mount ordering: the outer app's auth/session/org routes and core's own
    `/api/*` routes all win over the SPA fallback,
  * GET / and unauthenticated client-history deep links serve index.html
    (session auth gates the API, not the static bundle — login must be able
    to load the page that lets you log in),
  * a dev checkout with no built bundle stays headless exactly like before
    (the vite dev-mode signal is preserved), and
  * the optional client-error-capture / basemap-config injection ported
    from the private hosted layer, wired to env vars operators can set.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.server import static_serving
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.config import team_config_from_env
from tests.team_setup_helpers import claim_server


async def _raw_asgi_get(app: Any, path: str) -> tuple[int, bytes]:
    """Invoke `app` over a hand-built ASGI scope carrying `path` verbatim.

    httpx (what `TestClient`/`requests` use) collapses a literal ".." path
    component client-side, per normal URL parsing, before the request is
    ever sent — so `TestClient.get("/api/../x")` cannot reproduce what a
    raw HTTP client (`curl --path-as-is`) or a real ASGI server actually
    hands the app: the *literal* string, unresolved. This harness bypasses
    that client-side normalization to test the server-side guard directly.
    The percent-encoded variant (`%2e%2e`) is NOT collapsed by
    httpx, so it's tested via ordinary `TestClient` calls instead.
    """
    status_holder: dict[str, int] = {}
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

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
    return status_holder["status"], b"".join(chunks)


def _raw_status(app: Any, path: str) -> int:
    return asyncio.run(_raw_asgi_get(app, path))[0]


async def _mail(_email: str, _link: str) -> bool:
    return True


def _config(tmp_path: Path, **overrides: Any) -> TeamConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        # create_team_app now requires a run-queue locator unconditionally
        # (deferred follow-up from the queue-composition audit, "general run-queue
        # mismatch", completed).
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Desk",
        admin_emails={"owner@example.com"},
        static_dir=(
            Path(os.environ["FRISKET_STATIC_DIR"])
            if os.environ.get("FRISKET_STATIC_DIR", "").strip()
            else None
        ),
        client_error_capture=os.environ.get("FRISKET_CLIENT_ERROR_CAPTURE", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        map_tile_url_template=os.environ.get("FRISKET_MAP_TILE_URL_TEMPLATE", ""),
        map_api_key=os.environ.get("FRISKET_MAP_API_KEY", ""),
        map_attribution=os.environ.get("FRISKET_MAP_ATTRIBUTION", ""),
    )
    values.update(overrides)
    return TeamConfig(**values)


def _app(tmp_path: Path, **overrides: Any):
    config = _config(tmp_path, **overrides)
    app = create_team_app(config, send_magic_email=_mail)
    claim_server(app, origin=config.base_url)
    return app


def _make_static_dir(root: Path) -> Path:
    static_dir = root / "team-dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<html><head></head><body>frisket team app</body></html>",
        encoding="utf-8",
    )
    (static_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return static_dir


@pytest.fixture(autouse=True)
def _no_packaged_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests assert the FRISKET_STATIC_DIR / dev-mode boundary; a stray
    # packaged wheel bundle on the test machine must not leak into either
    # side of that assertion.
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: None)


def test_team_app_never_falls_back_to_the_packaged_local_edition_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # packaged_static_dir() is always the LOCAL edition's build
    # (scripts/release/build_frontend.py only ever stages web/dist/local into
    # frisket/web_static/) -- the team tier must never silently serve it
    # just because FRISKET_STATIC_DIR wasn't set (e.g. run from an
    # installed wheel instead of the Docker image, which always sets it).
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    packaged = _make_static_dir(tmp_path / "packaged-local-edition")
    monkeypatch.setattr(static_serving, "packaged_static_dir", lambda: packaged)

    client = TestClient(_app(tmp_path))
    assert client.get("/").status_code == 404  # headless, not the local-edition UI

    # sanity: resolve_static_dir() itself would pick it up for a caller
    # that allows the packaged fallback (e.g. the local tier does).
    assert static_serving.resolve_static_dir() == packaged


def test_team_app_serves_spa_with_api_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    app = _app(tmp_path)
    client = TestClient(app)

    # SPA loads unauthenticated: session auth gates the API, not the bundle.
    index = client.get("/")
    assert index.status_code == 200
    assert "frisket team app" in index.text

    # a deep client-history route falls back to index.html too (still
    # unauthenticated — the SPA itself redirects to sign-in client-side).
    deep = client.get("/p/demo/s/1/review")
    assert deep.status_code == 200
    assert "frisket team app" in deep.text

    # a real static asset is served directly
    assert client.get("/favicon.svg").text == "<svg/>"

    # outer app auth/org routes still win over the SPA mount
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert "text/html" not in client.get("/api/health").headers.get("content-type", "")

    # unknown API path is a real 404, not the SPA fallback
    missing = client.get("/api/no-such-route")
    assert missing.status_code == 404
    assert "frisket team app" not in missing.text


def test_team_app_serves_sign_in_and_other_non_enumerated_client_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the reserve-namespace-guard follow-up bug:
    # _is_browser_route() used to check the first path segment against a
    # hardcoded root allowlist ({"p", "account", "admin", "settings"}) --
    # every route known *at the time*, but closed against an open set.
    # web/src/routes/openRoutes.ts declares /sign-in (team edition) under no
    # such root, so a direct GET /sign-in 404'd instead of booting the app --
    # breaking exactly the bookmark/refresh-on-the-login-page flow this
    # lane exists to enable. Any extensionless, non-reserved, nonexistent
    # path must get the SPA fallback now, not just an enumerated few.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path))

    sign_in = client.get("/sign-in")
    assert sign_in.status_code == 200
    assert "frisket team app" in sign_in.text

    # the /apiary-style positive component-bound probe: NOT under the
    # reserved /api namespace (component-bounded -- "apiary" != "api"), so
    # it must reach the SPA fallback, not the reserved-namespace 404.
    for path in ("/apiary", "/apiary/demo", "/authors"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "frisket team app" in response.text, path

    # a real asset still serves directly, not the fallback
    assert client.get("/favicon.svg").text == "<svg/>"

    # /api and /auth are still real 404s via the reserved-namespace guard,
    # not the SPA fallback -- the fix must not have widened those back open.
    assert client.get("/api/no-such-route").status_code == 404
    assert client.get("/auth/no-such-route").status_code == 404


def test_team_app_headless_without_static_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    app = _app(tmp_path)
    client = TestClient(app)

    # today's API-only behavior is unchanged: no static dir -> no mount.
    assert client.get("/").status_code == 404
    assert client.get("/p/demo/s/1").status_code == 404
    assert client.get("/api/health").status_code == 200


def test_team_app_ignores_nonexistent_static_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(tmp_path / "does-not-exist"))
    app = _app(tmp_path)
    client = TestClient(app)

    assert client.get("/").status_code == 404
    assert client.get("/api/health").status_code == 200


def test_team_app_client_error_capture_flag_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    monkeypatch.delenv("FRISKET_CLIENT_ERROR_CAPTURE", raising=False)
    off_client = TestClient(_app(tmp_path / "off"))
    assert "__FRISKET_CLIENT_ERROR_CAPTURE__" not in off_client.get("/").text

    monkeypatch.setenv("FRISKET_CLIENT_ERROR_CAPTURE", "true")
    on_client = TestClient(_app(tmp_path / "on"))
    body = on_client.get("/").text
    assert "window.__FRISKET_CLIENT_ERROR_CAPTURE__=true;" in body

    # the sink this flag points the SPA at already exists and works
    # unauthenticated (it must — client errors can happen before sign-in).
    report = on_client.post(
        "/api/client-errors",
        json={"source": "browser", "name": "Oops", "message": "boom"},
    )
    assert report.status_code == 202


def test_team_app_map_config_injection_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    for env in (
        "FRISKET_MAP_TILE_URL_TEMPLATE",
        "FRISKET_MAP_API_KEY",
        "FRISKET_MAP_ATTRIBUTION",
    ):
        monkeypatch.delenv(env, raising=False)
    unset_client = TestClient(_app(tmp_path / "unset"))
    assert "__FRISKET_MAP_CONFIG__" not in unset_client.get("/").text

    monkeypatch.setenv(
        "FRISKET_MAP_TILE_URL_TEMPLATE", "https://tiles.example.test/{z}/{x}/{y}.png"
    )
    monkeypatch.setenv("FRISKET_MAP_API_KEY", "self-hosted-key")
    monkeypatch.setenv("FRISKET_MAP_ATTRIBUTION", "(c) Example Tiles")
    set_client = TestClient(_app(tmp_path / "set"))
    body = set_client.get("/").text
    assert "window.__FRISKET_MAP_CONFIG__=" in body
    assert "tiles.example.test" in body
    assert "self-hosted-key" in body


def test_team_app_map_config_injection_cannot_break_out_of_its_script_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An operator-controlled value (FRISKET_MAP_ATTRIBUTION etc.) must not
    # be able to close the injected <script> tag and inject arbitrary HTML
    # into every page the SPA serves.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    monkeypatch.setenv(
        "FRISKET_MAP_ATTRIBUTION",
        "</script><script>window.__pwned__=1;</script>",
    )
    body = TestClient(_app(tmp_path)).get("/").text
    # Only "<" needs escaping (an HTML parser needs a literal "<" to start
    # recognizing a closing tag) -- assert the actual mechanism: every "<"
    # in the operator-controlled value became the JS/JSON-safe <
    # escape, so no literal "<" survives anywhere in the payload.
    assert "</script><script>window.__pwned__" not in body
    assert "\\u003c/script>\\u003cscript>window.__pwned__=1;\\u003c/script>" in body
    payload_start = body.index("window.__FRISKET_MAP_CONFIG__=")
    payload_end = body.index("};</script>", payload_start)
    assert "<" not in body[payload_start:payload_end]


def test_team_app_uses_the_config_snapshot_after_ambient_env_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_a = _make_static_dir(tmp_path / "epoch-a")
    static_b = _make_static_dir(tmp_path / "epoch-b")
    (static_a / "index.html").write_text(
        "<html><head></head><body>epoch A</body></html>", encoding="utf-8"
    )
    (static_b / "index.html").write_text(
        "<html><head></head><body>epoch B</body></html>", encoding="utf-8"
    )
    env_a = {
        "FRISKET_TEAM_DATABASE_URL": f"sqlite:///{tmp_path / 'control.db'}",
        "FRISKET_RUN_QUEUE_DATABASE_URL": f"sqlite:///{tmp_path / 'queue.db'}",
        "FRISKET_DATA_DIR": str(tmp_path / "data"),
        "FRISKET_BASE_URL": "http://testserver",
        "FRISKET_ORGANIZATION_NAME": "Desk",
        "FRISKET_STATIC_DIR": str(static_a),
        "FRISKET_CLIENT_ERROR_CAPTURE": "true",
        "FRISKET_MAP_ATTRIBUTION": "epoch A maps",
    }
    config = team_config_from_env(env_a)

    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_b))
    monkeypatch.setenv("FRISKET_CLIENT_ERROR_CAPTURE", "false")
    monkeypatch.setenv("FRISKET_MAP_ATTRIBUTION", "epoch B maps")
    app = create_team_app(config, send_magic_email=_mail)

    core = next(
        route.app
        for route in app.routes
        if isinstance(getattr(route, "app", None), type(app))
    )
    static_app = next(
        route.app
        for route in core.routes
        if isinstance(getattr(route, "app", None), static_serving.SPAStaticFiles)
    )
    body = static_app._index_response().body.decode()  # noqa: SLF001
    assert Path(static_app.directory) == static_a
    assert "epoch A" in body and "epoch B" not in body
    assert "__FRISKET_CLIENT_ERROR_CAPTURE__=true" in body
    assert "epoch A maps" in body and "epoch B maps" not in body


# --------------------------------------------------------------------------
# reserved-namespace guard (frisket.server.static_serving._NamespaceGuardedMount)
#
# A plain Mount("/") Match.FULLs every path that reaches it, including ones
# under /api or /auth that no real route claimed -- letting a dot-segment
# or a coincidental on-disk file resolve through the SPA mount, and
# suppressing Starlette's own 404/405/redirect-slash fallback for those
# paths (it only runs when NO route matches at all). These tests pin the
# fix: the static mount must never even be reached for /api or /auth.
# --------------------------------------------------------------------------


def test_team_app_declines_raw_dot_segments_under_reserved_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    app = _app(tmp_path)

    assert _raw_status(app, "/api/../assets/favicon.svg") == 404
    assert _raw_status(app, "/api/../index.html") == 404
    assert _raw_status(app, "/auth/../assets/favicon.svg") == 404
    # sanity: the harness itself and the mount both still work normally
    assert _raw_status(app, "/favicon.svg") == 200


def test_team_app_declines_percent_encoded_dot_segments_under_reserved_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path))

    assert client.get("/api/%2e%2e/assets/favicon.svg").status_code == 404
    assert client.get("/auth/%2e%2e/assets/favicon.svg").status_code == 404


def test_team_app_reserved_namespace_shadows_a_colliding_real_static_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real file happening to exist at FRISKET_STATIC_DIR/api/... or
    # .../auth/... (e.g. a build artifact, or a name collision with a
    # future API route) must never be served through the SPA mount -- the
    # mount must not even be reached for those paths, regardless of what a
    # filesystem lookup under it would have found.
    static_dir = _make_static_dir(tmp_path / "static-src")
    (static_dir / "api").mkdir()
    (static_dir / "api" / "no-such-route").write_text("collision", encoding="utf-8")
    (static_dir / "auth").mkdir()
    (static_dir / "auth" / "leak.js").write_text("collision", encoding="utf-8")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path))

    api_collision = client.get("/api/no-such-route")
    assert api_collision.status_code == 404
    assert "collision" not in api_collision.text

    auth_collision = client.get("/auth/leak.js")
    assert auth_collision.status_code == 404
    assert "collision" not in auth_collision.text


def test_team_app_restores_trailing_slash_redirect_for_api_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Starlette only redirect_slashes when no route (including the SPA
    # mount) matched at all -- an unconditional mount used to swallow this.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path), follow_redirects=False)

    response = client.get("/api/health/")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/health")


def test_team_app_restores_405_for_wrong_method_on_a_real_api_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A path match with the wrong method is a Starlette PARTIAL match ->
    # 405; the unconditional mount used to intercept it (Match.FULL) before
    # the router ever got to apply that semantics.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path))

    assert client.request("DELETE", "/api/health").status_code == 405
    assert client.head("/api/health").status_code == 405


def test_team_app_unknown_api_method_stays_404_not_staticfiles_405(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the fix, an unmatched /api POST fell through into StaticFiles
    # (which only supports GET/HEAD) and got its 405, instead of the
    # router's real "no such route" 404.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    client = TestClient(_app(tmp_path))

    assert client.post("/api/no-such-route").status_code == 404


def test_serve_spa_false_on_core_never_leaks_an_inner_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the ordering fix: the SPA must be mounted onto
    # `core` (or after it) — mounting a second app at "/" AFTER
    # `app.mount("/", core)` would be unreachable dead code, since the first
    # "/" mount swallows every path. This asserts the observable outcome
    # (the SPA is actually reachable), which is what would silently regress
    # if a future edit re-ordered the two mounts on the outer `app` instead.
    static_dir = _make_static_dir(tmp_path / "static-src")
    monkeypatch.setenv("FRISKET_STATIC_DIR", str(static_dir))
    app = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/").status_code == 200
