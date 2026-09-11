"""Shared SPA static-file serving for both frisket tiers.

The hosted app mounts the built web bundle so
a browser hitting the deployment gets the UI; the local server was API-only, so
a packaged `pip`/`uvx` install of `frisket <workspace>` booted a headless :7331
(onboarding research friction #1). This module is the single home for that
primitive so both tiers mount the same behaviour:

  * `SPAStaticFiles` — serve real files, but fall back to `index.html` for the
    client-router history routes so a deep link like `/p/{id}/s/{sheet}` still
    boots the app instead of 404ing.
  * `resolve_static_dir()` — the resolution order the local server uses:
    explicit `FRISKET_STATIC_DIR` -> packaged assets shipped in the wheel
    (`frisket/web_static/`) -> `None` (dev mode: vite serves the UI and proxies
    `/api` to :7331, see `web/vite.config.ts`).
  * `mount_spa_static()` — attach the mount at `/` after all API routes so API
    routes take precedence. Registration order alone is not a namespace
    guarantee: a plain `Mount("/")` Match.FULLs *every* path that reaches
    it, including ones under `/api`/`/auth` that no real route claimed, and
    Starlette only runs its own 404/405/redirect-slash fallback when no
    route matches at all — so an unconditional mount here would silently
    swallow that fallback for those namespaces, and (since Starlette
    resolves `..`/percent-encoding *inside* `StaticFiles`, after this
    mount already claimed the request) let a path like
    `/api/../assets/app.js` reach a real static file through a namespace
    the rest of the app treats as API-only. `_NamespaceGuardedMount`
    declines to match `/api` and `/auth` at Starlette's route-matching
    stage instead, before any of that can happen, so unmatched requests
    under those prefixes fall through to the router's native handling
    exactly as if this mount were not registered.
  * `describe_static_source()` — the human string `frisket doctor` and CLI
    startup print so the operator can see the true serving state.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount, get_route_path
from starlette.types import Scope

STATIC_DIR_ENV = "FRISKET_STATIC_DIR"

# Wire-level namespaces every edition reserves for its own routes (every API
# route lives under /api, every browser-facing auth route under /auth — see
# frisket.contracts.http.endpoint_catalog and frisket.team.app). The SPA
# mount must never be reachable for a request under either, component-
# bounded (`/api`/`/api/...` but not `/apiary`).
_RESERVED_NAMESPACES = ("/api", "/auth")


class _NamespaceGuardedMount(Mount):
    """A `Mount("/")` that never Match.FULLs a reserved-namespace request.

    Plain `Mount.matches()` regex-matches the *entire* remaining path and
    always succeeds for a mount at `/`, so it would ordinarily intercept
    every request that reaches it — including malformed/unmatched `/api`
    and `/auth` ones a real route never claimed. Declining here, before any
    routing happens, means the Starlette router treats this mount as if it
    were not registered for those paths, restoring its native fallback
    (404 for a genuinely unmatched route, 405 for a path match with the
    wrong method, 307 for a missing/extra trailing slash) instead of
    letting `StaticFiles`'s own path resolution (which normalizes `..` and
    percent-encoding) decide what a reserved-namespace URL means.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope.get("type") in ("http", "websocket"):
            path = get_route_path(scope)
            for namespace in _RESERVED_NAMESPACES:
                if path == namespace or path.startswith(namespace + "/"):
                    return Match.NONE, {}
        return super().matches(scope)


# index.html must always revalidate (it is the deploy's version pointer); the
# hashed asset filenames are content-addressed, so they can cache for a year.
SPA_HTML_CACHE_CONTROL = "no-cache, must-revalidate"
HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
HASHED_ASSET_RE = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


class SPAStaticFiles(StaticFiles):
    """Serve static assets normally, but route app paths back to index.html.

    Starlette's html=True handles `/` and real files; browser-history routes
    like `/p/{project}/s/{sheet}` still need an explicit SPA fallback.
    """

    def __init__(
        self,
        *args: Any,
        client_error_capture: bool = False,
        map_config: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.client_error_capture = client_error_capture
        self.map_config = map_config or {}

    def _index_response(self) -> HTMLResponse:
        index_path = Path(self.directory) / "index.html"
        html = index_path.read_text(encoding="utf-8")
        statements: list[str] = []
        if self.client_error_capture:
            statements.append("window.__FRISKET_CLIENT_ERROR_CAPTURE__=true;")
        if self.map_config:
            # Escape "<" so a value can never break out of the <script> tag.
            payload = json.dumps(self.map_config).replace("<", "\\u003c")
            statements.append(f"window.__FRISKET_MAP_CONFIG__={payload};")
        if statements:
            script = "<script>" + "".join(statements) + "</script>"
            if "</head>" in html:
                html = html.replace("</head>", f"    {script}\n  </head>", 1)
            else:
                html = f"{script}\n{html}"
        return HTMLResponse(html, headers={"Cache-Control": SPA_HTML_CACHE_CONTROL})

    def _apply_static_cache_headers(self, path: str, response: Response) -> Response:
        normalized = path.strip("/")
        name = Path(normalized).name
        if normalized.startswith("assets/") and HASHED_ASSET_RE.fullmatch(name):
            response.headers["Cache-Control"] = HASHED_ASSET_CACHE_CONTROL
        elif name == "index.html":
            response.headers["Cache-Control"] = SPA_HTML_CACHE_CONTROL
        return response

    async def get_response(self, path: str, scope: dict) -> Response:
        normalized = path.strip("/")
        if normalized in {"", ".", "index.html"}:
            return self._index_response()
        try:
            response = await super().get_response(path, scope)
            return self._apply_static_cache_headers(path, response)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return self._index_response()
            raise


def packaged_static_dir() -> Path | None:
    """The built web bundle shipped inside the wheel, or None.

    `scripts/release/release_preflight.py --build` (and the Docker web stage) copy
    `web/dist` into `frisket/web_static/` before `uv build`, so an installed
    package carries the UI. In an editable source checkout that directory does
    not exist (it is gitignored) and this returns None, which is exactly the
    dev-mode signal — vite serves the UI in that case.
    """
    candidate = Path(__file__).resolve().parent.parent / "web_static"
    if (candidate / "index.html").is_file():
        return candidate
    return None


def resolve_static_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    allow_packaged_fallback: bool = True,
) -> Path | None:
    """Resolve which directory (if any) holds the built UI to serve.

    Order: an explicit path / `FRISKET_STATIC_DIR` that exists -> packaged
    assets in the wheel -> None (dev mode). A configured-but-missing
    `FRISKET_STATIC_DIR` is ignored rather than mounted (a typo must not take
    the UI down); it falls through to the packaged bundle or dev mode.

    `allow_packaged_fallback=False` skips the packaged-wheel tier entirely
    (still tries explicit/`FRISKET_STATIC_DIR` first, then `None`) — for a
    caller whose packaged wheel assets are built for a *different* edition.
    `packaged_static_dir()` is always the LOCAL edition
    (`scripts/release/build_frontend.py` only ever stages `web/dist/local` into
    `frisket/web_static/`), so the team tier passes this to avoid silently
    serving the wrong SPA build (local capabilities, not team's) if it is
    ever run from an installed wheel with no `FRISKET_STATIC_DIR` set — the
    shipped Docker image always sets `FRISKET_STATIC_DIR` explicitly, so
    this only changes behavior for that unsupported combination, turning a
    wrong-edition 200 into the same headless 404 dev mode already is.
    """
    env_value = explicit if explicit is not None else os.environ.get(STATIC_DIR_ENV, "")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_dir() and (env_path / "index.html").is_file():
            return env_path
    if not allow_packaged_fallback:
        return None
    return packaged_static_dir()


def mount_spa_static(
    app: FastAPI,
    directory: str | os.PathLike[str],
    *,
    client_error_capture: bool = False,
    map_config: dict[str, str] | None = None,
) -> None:
    """Mount the built SPA at `/`. Call AFTER registering API routes so API
    routes win over the catch-all mount.

    Uses `_NamespaceGuardedMount` rather than `FastAPI.mount()`'s plain
    `Mount` (`FastAPI.mount()` hardcodes the class, so it is constructed
    directly and appended the same way `Router.mount()` does internally) —
    see the module docstring and `_NamespaceGuardedMount` for why an
    unconditional mount here is not actually safe.
    """
    app.router.routes.append(
        _NamespaceGuardedMount(
            "/",
            app=SPAStaticFiles(
                directory=str(directory),
                html=True,
                client_error_capture=client_error_capture,
                map_config=map_config,
            ),
            name="static",
        )
    )


def describe_static_source(explicit: str | os.PathLike[str] | None = None) -> str:
    """One-line human description of the resolved serving state for
    `frisket doctor` and CLI startup."""
    env_value = explicit if explicit is not None else os.environ.get(STATIC_DIR_ENV, "")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_dir() and (env_path / "index.html").is_file():
            return f"{env_path} (FRISKET_STATIC_DIR)"
        return (
            f"{env_path} (FRISKET_STATIC_DIR set but missing/invalid — falling through)"
        )
    packaged = packaged_static_dir()
    if packaged is not None:
        return f"{packaged} (packaged web bundle)"
    return "unset (dev mode: vite serves the UI, proxying /api to this server)"
