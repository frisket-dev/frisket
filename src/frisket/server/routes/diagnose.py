"""In-app Diagnose route: the same self-probe logic `frisket doctor`
prints to a terminal (frisket.diagnostics, extracted from cli.py so this
route and the CLI share one implementation), served as JSON for an in-app
Diagnose panel reachable from LLM-run error states.

The route reflects the CALLER's actual
effective router (``Workspace.diagnostic_router()``) rather than a bare
env-only ``ModelRouter()`` -- local tier picks up the workspace's
``.frisket/provider_keys.json`` file keys, hosted tier reports the
signed-in org's own router (each org already has an isolated
``Workspace``/``create_app`` -- see ``hosted/app.py``'s
``_build_tenant_app``), and a platform env key is never fabricated for a
tenant that has not configured one. ``frisket doctor`` (cli.py) is
unaffected -- it calls ``diagnostics.run_diagnostics()`` directly with no
router.

The route also threads the workspace's run queue (worker heartbeat / queue staleness --
the SAME DTO ``/api/health`` serves, see ``diagnostics.queue_health_report``)
and the workspace root (free-disk probe) through.

There are TWO routes, and the split is a
security boundary, not a convenience.

``GET /api/diagnose`` is the workspace-wide probe and can name no project at
all. Its plugin-runtime-health probe always reports the honest "no project
in scope" skip rather than inventing project plugin state.

``GET /api/projects/{pid}/diagnose`` is the project-scoped probe. The project
rides on the PATH because the role ladder resolves a project only from
``path_params["pid"]`` (``team/enforcement.py``, and the same line in a
downstream composition's tenant dispatch). While the project arrived as a
``project_id`` QUERY parameter on ``/api/diagnose``, the catalog could only
classify that route ``project_role=None``, so the ladder never ran for the
project the handler then opened: any org member could read any sibling
project's plugin health, a bad slug degraded to ``available:false`` and made
the route a project-existence oracle, and the probe's first-touch
``bootstrap_project_bundled_plugins`` was a latent WRITE into a bundle the
caller holds no role on. Naming the project on the path closes the shape --
the ladder is exhaustive over ``{pid}`` routes by construction -- rather than
bolting a second, forgettable check onto this one handler.

The per-probe ``_info_probe`` guarantee is unchanged WITHIN a response: one
probe's bad input still cannot break the others. What changed is that an
unreadable project id is now refused by the fence in front of the handler
(403/404) instead of being swallowed into a 200 -- the swallow was the oracle.
"""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.diagnostics import DiagnosticsReport
from frisket.server.workspace import Workspace


def register_diagnose_routes(
    app: FastAPI,
    *,
    workspace: Workspace,
    liveness_window_seconds: float = 90.0,
    queue_timeout_seconds: float | None = None,
) -> None:
    def _run(project: object | None, project_id: str | None) -> dict:
        from frisket.operability import diagnostics

        return diagnostics.run_diagnostics(
            router=workspace.diagnostic_router(),
            queue=workspace.queue,
            liveness_window_seconds=liveness_window_seconds,
            queue_timeout_seconds=queue_timeout_seconds,
            workspace_root=workspace.root,
            project=project,
            project_id=project_id,
        )

    @app.get("/api/diagnose", response_model=DiagnosticsReport)
    def diagnose() -> dict:
        # No project parameter of any kind: there is nothing here for a caller
        # to name past the gate.
        return _run(None, None)

    @app.get("/api/projects/{pid}/diagnose", response_model=DiagnosticsReport)
    def project_diagnose(pid: str) -> dict:
        # ``pid`` has already been through the project-role ladder on every
        # deployment that has one. ``workspace.get`` raising (a slug that is
        # syntactically fine but absent) surfaces as its own HTTPException
        # rather than degrading to a 200 -- the degrade was the oracle.
        return _run(workspace.get(pid), pid)
