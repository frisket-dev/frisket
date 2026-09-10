"""Runtime config route (onboard-replay-banner-v1): the active cache-mode/
live-call posture, so the frontend can show a persistent banner when replay
mode means live AI calls will not happen.

onboard2-signin-sender-copy-v1: also carries the outbound-email sender
identity (address + name), read by SignIn.tsx instead of a hardcoded
address. The local tier never sends email -- there is no sender to report,
and SignIn is a hosted-only surface (local has no /api/me, never shows
sign-in) -- so the honest local payload is a null sender rather than an
invented identity.

Also carries ``recipe_fence_posture``: what a `map.python` code recipe is
confined by on this server. The browser cannot know the server's platform, and
the answer is per-platform (engine/sandbox/fence.py's matrix), so the editor's
trust line would otherwise have to guess -- which is how it previously claimed
a sandbox nobody had, and then, once a kernel fence landed on Linux, warned
about a perimeter that was no longer missing. Minted in
`frisket.engine.recipe_fence_posture` from the fence's own answer and only
relayed here: the route must not grow a second opinion about the matrix.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from frisket.ai.llm import live_calls_possible
from frisket.authoring.workbench.plugin_runtime_shared import (
    active_plugin_composition_policy,
)
from frisket.contracts.http.instance_runtime import (
    AuthMethods,
    RuntimeConfigResponse,
    RuntimeConfigUpdateRequest,
)
from frisket.engine.recipe_fence_posture import recipe_fence_posture
from frisket.server.route_errors import http_error_responses
from frisket.server.workspace import Workspace


def in_container() -> bool:
    """True when this server is running inside a container.

    ``cache_mode`` is set by an environment variable and applied at restart,
    but HOW you set it and restart differs: a compose deployment edits its
    ``.env`` and runs ``docker compose restart``, while a ``frisket <dir>``
    launch sets the variable in the shell it launches from. Settings used to
    hardcode the compose instructions for everyone, which is wrong copy for
    anybody who started the server directly -- so the posture is reported and
    the frontend describes the mechanism the reader actually has.

    ``/.dockerenv`` is Docker's own marker; ``FRISKET_IN_CONTAINER`` is the
    explicit override for runtimes that do not write it.
    """

    override = os.environ.get("FRISKET_IN_CONTAINER")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return Path("/.dockerenv").exists()


def register_runtime_config_routes(
    app: FastAPI,
    *,
    workspace: Workspace,
    allow_updates: bool,
    auth_methods: AuthMethods | None = None,
    product_telemetry_available: bool = False,
) -> None:
    effective_auth_methods = auth_methods or AuthMethods(
        password=False, magic_link=False, oidc=[]
    )

    def response() -> RuntimeConfigResponse:
        mode = workspace.cache_mode
        plugin_policy = active_plugin_composition_policy()
        return RuntimeConfigResponse.model_validate(
            {
                "cache_mode": mode,
                "live_calls_possible": live_calls_possible(mode),
                "cache_mode_editable": allow_updates,
                "cost_preapproval_usd": workspace.cost_preapproval_usd,
                "cost_preapproval_editable": allow_updates,
                "in_container": in_container(),
                "recipe_fence_posture": recipe_fence_posture(),
                "email_from_address": None,
                "email_from_name": None,
                "auth_methods": effective_auth_methods,
                "plugins_available": plugin_policy.exposes_any_plugins,
                "plugin_management_available": plugin_policy.nonbundled_enabled,
                "product_telemetry_available": product_telemetry_available,
            }
        )

    @app.get(
        "/api/config",
        response_model=RuntimeConfigResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(500),
    )
    def runtime_config() -> RuntimeConfigResponse:
        return response()

    if allow_updates:

        @app.patch(
            "/api/config",
            response_model=RuntimeConfigResponse,
            response_model_exclude_unset=True,
            responses=http_error_responses(409, 422, 500),
        )
        def update_runtime_config(
            body: RuntimeConfigUpdateRequest,
        ) -> RuntimeConfigResponse:
            if body.cache_mode is None and body.cost_preapproval_usd is None:
                raise HTTPException(422, "choose a runtime setting to update")
            current = workspace.cache_mode
            # Saving the preference does not itself spend. It does select the
            # posture later runs may use, so every move to a live-capable mode
            # requires acknowledgement. Run-level cost gates remain the
            # authority for actual provider spend.
            selects_live_mode = (
                body.cache_mode is not None
                and body.cache_mode != current
                and live_calls_possible(body.cache_mode)
            )
            if selects_live_mode and not body.confirmed:
                raise HTTPException(
                    409,
                    "Type confirm before enabling a mode that can make live AI calls.",
                )
            if body.cache_mode is not None:
                workspace.set_local_cache_mode(body.cache_mode)
            if body.cost_preapproval_usd is not None:
                try:
                    workspace.set_local_cost_preapproval_usd(body.cost_preapproval_usd)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            return response()
