"""Open team enforcement layer: session/PAT auth, admin gate, project RBAC.

The core app (:func:`frisket.server.app.create_app`) is UNAUTHENTICATED: it
trusts its caller. In an external managed composition the only thing that
ever authenticated an ``/api`` request was the fleet dispatch middleware,
whose service also owns commerce (credit gates, reservations, spend caps) —
so an open edition could not reuse it without dragging commerce along
(the public package boundary).

This module is the open replacement. :func:`protect_core_app` wraps ONE core app
in place with an ASGI middleware that answers, for every registered ``/api``
route, exactly one enforcement decision:

* ``public``          — served without an actor (``/api/health``, ``/api/config``);
* ``session_or_pat``  — a browser session cookie OR a personal access token;
* ``browser_session`` — session only; a PAT is refused (403) with the declared
  detail;
* ``admin``           — a browser session whose email is in ``admin_emails()``;
* ``project_role``    — the viewer/reviewer/editor/owner ladder, resolved
  through the injected project-access port.

Coverage is EXHAUSTIVE BY CONSTRUCTION (acceptance gate 5): the decision table
is compiled from the neutral base catalog
(:data:`frisket.contracts.http.endpoint_catalog.BASE_ENDPOINT_CATALOG`) against
the app's own registered route inventory, with the shared edition-neutral
compiler. A registered ``/api`` route that no declaration classifies makes
:func:`protect_core_app` RAISE — the server refuses to start rather than
default-allowing an unclassified route. The inventory is re-verified on startup
so a route registered after protection cannot slip through either.

Every collaborator arrives as an injected port, which is what keeps external
managed compositions out of the open package: this module imports FastAPI,
Starlette and the neutral catalog, nothing else. It knows nothing about money.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from frisket.contracts.http.endpoint_catalog import (
    BASE_ENDPOINT_CATALOG,
    CompiledRoutePolicy,
    LOCAL_CONFIGURATION_ENDPOINT_IDS,
    RoutePolicyDecision,
    compile_route_policy,
)


# The core app registers the shared endpoints the base catalog declares under
# the ``tenant`` route owner (the ``outer`` owner is a control-plane concept of
# the app that FRONTS the core app; the open team entrypoint owns that half
# separately).
CORE_ROUTE_OWNER = "tenant"

# Resolvers that inspect the request body. Mirrors an external composition's
# rule: a route whose policy needs the body requires a JSON object body (400
# otherwise).
_BODY_RESOLVERS = frozenset(
    {"action.code_execution", "notification.owner", "review.decision"}
)

# Resolvers that may CHANGE the required project role, so they must run before
# the role ladder is checked (a review decision downgrades editor -> reviewer;
# a notification the actor owns downgrades owner -> editor).
_ROLE_RESOLVERS = frozenset(
    {"notification.owner", "notification.route_test", "review.decision"}
)

CODE_EXECUTION_DENIED_DETAIL = "code execution is disabled on this server"


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


class ProjectAccess(Protocol):
    """Project identity + role ladder. Duck-compatible with the control-plane
    project-access service the team entrypoint injects."""

    def resolve_project_for_user_id(
        self,
        user_id: int,
        slug: str,
        *,
        preferred_org_id: int | None = None,
        scope_org_id: int | None = None,
    ) -> dict[str, Any] | None: ...

    def can_on_project(
        self, org_id: int, slug: str, user_id: int, need: str
    ) -> bool: ...


ResolveUser = Callable[[Request], dict[str, Any] | None]
AdminEmails = Callable[[], set[str]]
OrgRole = Callable[[int, int], str | None]
DenyCodeExecution = Callable[..., bool]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


class TeamEnforcementError(RuntimeError):
    """The enforcement layer cannot be compiled — refuse to start."""


class _Refused(Exception):
    status_code = 403

    def __init__(self, detail: Any) -> None:
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.detail = detail


class _Unauthorized(_Refused):
    status_code = 401


class _Forbidden(_Refused):
    status_code = 403


class _BadRequest(_Refused):
    status_code = 400


class _Conflict(_Refused):
    status_code = 409


# ---------------------------------------------------------------------------
# the compiled decision table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RegisteredRoute:
    route_owner: str
    route_name: str
    method: str
    path: str


def _registered_api_routes(app: FastAPI) -> tuple[_RegisteredRoute, ...]:
    return tuple(
        _RegisteredRoute(
            route_owner=CORE_ROUTE_OWNER,
            route_name=str(route.name),
            method=method.upper(),
            path=route.path,
        )
        for route in app.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api")
        for method in sorted(route.methods or ())
    )


def _core_declarations() -> tuple[Any, ...]:
    return tuple(
        entry
        for entry in BASE_ENDPOINT_CATALOG
        if (
            entry.route_owner == CORE_ROUTE_OWNER
            and entry.id not in LOCAL_CONFIGURATION_ENDPOINT_IDS
        )
    )


def _compile(
    app: FastAPI, resolvers: Mapping[str, Callable[[Any], None]]
) -> CompiledRoutePolicy:
    """Compile the team decision table, or refuse to start.

    The same edition-neutral compiler an external composition uses. It fails
    closed on a registered route no declaration classifies, on a declaration
    with no registered route, on an ambiguous wire path, and on a missing
    resolver.
    """

    inventory = _registered_api_routes(app)
    declarations = _core_declarations()
    try:
        return compile_route_policy(declarations, inventory, resolvers)
    except (ValueError, LookupError) as exc:
        declared = {
            (entry.route_owner, entry.route_name, entry.method)
            for entry in declarations
        }
        unclassified = sorted(
            f"{route.method} {route.path} (route_name={route.route_name!r})"
            for route in inventory
            if (route.route_owner, route.route_name, route.method) not in declared
        )
        if unclassified:
            raise TeamEnforcementError(
                "refusing to start: unclassified /api route(s) on the protected"
                " team app — every registered route must resolve to an"
                " enforcement decision in the base endpoint catalog"
                f" (frisket.contracts.http.endpoint_catalog): {unclassified}"
            ) from exc
        raise TeamEnforcementError(
            f"refusing to start: the team route policy did not compile: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# one request's evaluation
# ---------------------------------------------------------------------------


@dataclass
class _Evaluation:
    decision: RoutePolicyDecision
    user: dict[str, Any]
    path_params: dict[str, Any]
    body: dict[str, Any] = field(default_factory=dict)
    required_project_role: str | None = None
    project_context: dict[str, Any] | None = None


class _Enforcer:
    """The policy decisions themselves — framework-free apart from the ports."""

    def __init__(
        self,
        *,
        app: FastAPI,
        resolve_user: ResolveUser,
        admin_emails: AdminEmails,
        project_access: ProjectAccess,
        org_role: OrgRole | None,
        deny_code_execution: DenyCodeExecution | None,
    ) -> None:
        self._app = app
        self._resolve_user = resolve_user
        self._admin_emails = admin_emails
        self._project_access = project_access
        self._org_role = org_role
        self._deny_code_execution = deny_code_execution
        self._policy = _compile(app, self.resolvers())

    # -- construction-time guarantees ------------------------------------

    def resolvers(self) -> dict[str, Callable[[_Evaluation], None]]:
        """The open resolver set. The commerce resolvers of an external
        composition (credit gate, reservation) have no counterpart here and no
        route on the team app declares them — a declaration that did would
        fail to compile.
        """

        return {
            "action.code_execution": self._policy_action_code_execution,
            "notification.owner": self._policy_notification_owner,
            "notification.route_test": self._policy_notification_route_test,
            "org.spend": self._policy_org_spend,
            "review.decision": self._policy_review_decision,
        }

    def verify_inventory(self) -> None:
        """Re-compile against the CURRENT inventory (startup): a route added
        after the layer was installed must not slip past the decision table."""

        _compile(self._app, self.resolvers())

    # -- request-time enforcement ----------------------------------------

    def actor(self, request: Request) -> dict[str, Any] | None:
        return self._resolve_user(request)

    def match(self, method: str, path: str) -> tuple[APIRoute, dict[str, Any]] | None:
        scope = {
            "type": "http",
            "method": method.upper(),
            "path": path,
            "root_path": "",
            "headers": (),
            "query_string": b"",
            "app": self._app,
        }
        for route in self._app.router.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
                continue
            matched, child_scope = route.matches(scope)
            if matched is Match.FULL:
                return route, dict(child_scope.get("path_params") or {})
        return None

    def decision_for(self, route: APIRoute, method: str) -> RoutePolicyDecision:
        # LookupError here would mean the compiled table and the live router
        # disagree, which construction and startup already refuse; fail closed.
        return self._policy.resolve(CORE_ROUTE_OWNER, str(route.name), method)

    def authenticate(
        self, decision: RoutePolicyDecision, user: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if decision.auth == "public":
            return user
        if user is None:
            raise _Unauthorized("not signed in")
        if decision.auth == "browser_session" and user.get("auth") == "pat":
            raise _Forbidden(
                decision.pat_forbidden_detail or "browser session required"
            )
        if decision.auth == "admin":
            if user.get("auth") == "pat":
                raise _Forbidden(
                    decision.pat_forbidden_detail
                    or "admin routes require a browser session"
                )
            if user["email"] not in self._admin_emails():
                raise _Forbidden("not an admin")
        return user

    def needs_body(self, decision: RoutePolicyDecision) -> bool:
        return bool(_BODY_RESOLVERS.intersection(decision.resolvers))

    def authorize(
        self,
        *,
        decision: RoutePolicyDecision,
        user: dict[str, Any],
        path_params: Mapping[str, Any],
        body: bytes,
    ) -> _Evaluation:
        evaluation = _Evaluation(
            decision=decision,
            user=user,
            path_params=dict(path_params),
            body=_body_peek(body, strict=self.needs_body(decision)),
            required_project_role=decision.project_role,
        )

        project_id = evaluation.path_params.get("pid")
        if decision.project_role is not None:
            if not isinstance(project_id, str) or not project_id:
                raise _Forbidden("project route has no project identity")
            try:
                project_ctx = self._project_access.resolve_project_for_user_id(
                    int(user["id"]),
                    project_id,
                    preferred_org_id=int(user["org_id"]),
                    scope_org_id=_pat_scope(user),
                )
            except ValueError as exc:
                raise _Conflict("ambiguous project id") from exc
            if project_ctx is None:
                raise _Forbidden("no access to this project")
            evaluation.project_context = project_ctx

        resolvers = self.resolvers()
        for resolver_id in decision.resolvers:
            if resolver_id in _ROLE_RESOLVERS:
                resolvers[resolver_id](evaluation)

        if evaluation.required_project_role is not None:
            project_ctx = evaluation.project_context
            if project_ctx is None or not isinstance(project_id, str):
                raise _Forbidden("no access to this project")
            # Break-glass: a server admin who already holds project access may
            # delete a whole project without the project-owner role. Scoped to
            # whole-project DELETE only; do not widen it.
            admin_break_glass = (
                decision.route_name == "delete_project"
                and user["email"] in self._admin_emails()
            )
            if not admin_break_glass and not self._project_access.can_on_project(
                int(project_ctx["org_id"]),
                project_id,
                int(user["id"]),
                evaluation.required_project_role,
            ):
                raise _Forbidden(
                    "your project role cannot perform this action "
                    f"(requires '{evaluation.required_project_role}')"
                )

        for resolver_id in decision.resolvers:
            if resolver_id not in _ROLE_RESOLVERS:
                resolvers[resolver_id](evaluation)
        return evaluation

    # -- resolvers --------------------------------------------------------

    def _policy_review_decision(self, policy: _Evaluation) -> None:
        """A review decision is a reviewer's job, not an editor's: downgrade the
        required role so a reviewer may submit one on an otherwise editor route.
        """

        if policy.body.get("action_id") == "review.decision":
            policy.required_project_role = "reviewer"

    def _policy_notification_owner(self, policy: _Evaluation) -> None:
        """A user-owned notification route is the actor's own business (editor);
        anything org/project-owned requires the project owner."""

        owner_kind = str(policy.body.get("owner_kind") or "")
        owner_ref = policy.body.get("owner_ref")
        actor_id = f"user:{int(policy.user['id'])}"
        if (
            owner_kind == "user"
            and policy.user.get("auth") != "pat"
            and (owner_ref is None or str(owner_ref) == actor_id)
        ):
            policy.required_project_role = "editor"
        else:
            policy.required_project_role = "owner"

    def _policy_notification_route_test(self, policy: _Evaluation) -> None:
        policy.required_project_role = "owner"
        if policy.user.get("auth") == "pat":
            return
        try:
            route_id = int(policy.path_params["route_id"])
            actor_id = f"user:{int(policy.user['id'])}"
            project = self._workspace().get(str(policy.path_params["pid"]))
            route = project.notification_route(route_id)
        except Exception:  # noqa: BLE001 - fail closed to the owner gate
            return
        if (
            route is not None
            and str(route["owner_kind"]) == "user"
            and str(route["owner_ref"]) == actor_id
        ):
            policy.required_project_role = "editor"

    def _policy_org_spend(self, policy: _Evaluation) -> None:
        """Org-wide usage is an org-administration view.

        With an ``org_role`` port injected, the org's own owner/admin see it.
        Without one the open default is fail-closed: only a server admin
        (``admin_emails``) may read another member's usage.
        """

        if self._org_role is not None:
            role = self._org_role(int(policy.user["id"]), int(policy.user["org_id"]))
            if role in ("owner", "admin"):
                return
        elif policy.user["email"] in self._admin_emails():
            return
        raise _Forbidden("only an org owner or admin can view org spend")

    def _policy_action_code_execution(self, policy: _Evaluation) -> None:
        if self._denied(body=policy.body):
            raise _Forbidden(CODE_EXECUTION_DENIED_DETAIL)

    def _denied(self, **kwargs: Any) -> bool:
        """A team server runs its own workers, so arbitrary code execution is
        allowed unless the deployment injects a policy that denies it."""

        if self._deny_code_execution is None:
            return False
        return bool(self._deny_code_execution(**kwargs))

    def _workspace(self) -> Any:
        workspace = getattr(self._app.state, "workspace", None)
        if workspace is None:
            raise _Forbidden("project workspace is unavailable")
        return workspace


def _pat_scope(user: Mapping[str, Any]) -> int | None:
    return int(user["org_id"]) if user.get("auth") == "pat" else None


def _body_peek(body: bytes, *, strict: bool = False) -> dict[str, Any]:
    if strict and not body:
        raise _BadRequest("request body is required by route policy")
    try:
        parsed = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        if strict:
            raise _BadRequest("request body must be a JSON object") from exc
        return {}
    if not isinstance(parsed, dict):
        if strict:
            raise _BadRequest("request body must be a JSON object")
        return {}
    return parsed


# ---------------------------------------------------------------------------
# the ASGI middleware
# ---------------------------------------------------------------------------


async def _drain_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_body(body: bytes) -> Receive:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class TeamEnforcementMiddleware:
    """Pure-ASGI so the request body can be read for body-dependent policy and
    replayed to the core app untouched."""

    def __init__(self, app: ASGIApp, *, enforcer: _Enforcer) -> None:
        self.app = app
        self._enforcer = enforcer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api"):
            # Non-API surfaces (docs, static/SPA when the deployment serves one)
            # carry no policy declarations and no project data.
            await self.app(scope, receive, send)
            return

        method = str(scope["method"]).upper()
        matched = self._enforcer.match(method, str(scope["path"]))
        if matched is None:
            # Not a registered route: the app answers 404/405 itself.
            await self.app(scope, receive, send)
            return
        route, path_params = matched
        decision = self._enforcer.decision_for(route, method)

        request = Request(scope, receive)
        try:
            user = self._enforcer.authenticate(decision, self._enforcer.actor(request))
        except _Refused as refusal:
            await _refuse(refusal, scope, receive, send)
            return

        if decision.auth == "public" or user is None:
            await self.app(scope, receive, send)
            return

        body = b""
        if method in {"POST", "PUT", "PATCH", "DELETE"} and (
            self._enforcer.needs_body(decision)
        ):
            body = await _drain_body(receive)
            receive = _replay_body(body)

        try:
            self._enforcer.authorize(
                decision=decision,
                user=user,
                path_params=path_params,
                body=body,
            )
        except _Refused as refusal:
            await _refuse(refusal, scope, receive, send)
            return

        # The core app attributes notification ownership and audit entries to
        # request.state.user; stamp the authenticated actor for it.
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["user"] = user
        await self.app(scope, receive, send)


async def _refuse(
    refusal: _Refused, scope: Scope, receive: Receive, send: Send
) -> None:
    detail = refusal.detail
    payload = detail if isinstance(detail, dict) else {"detail": str(detail)}
    response = JSONResponse(payload, status_code=refusal.status_code)
    await response(scope, receive, send)


# ---------------------------------------------------------------------------
# the one public factory
# ---------------------------------------------------------------------------


def protect_core_app(
    *,
    app: FastAPI,
    resolve_user: ResolveUser,
    admin_emails: AdminEmails,
    project_access: ProjectAccess,
    org_role: OrgRole | None = None,
    deny_code_execution: DenyCodeExecution | None = None,
) -> FastAPI:
    """Install open enforcement on ONE core app and return it.

    Ports (everything the layer needs; nothing else is required, which is what
    keeps commerce out of the open package):

    * ``app`` — the core app from :func:`frisket.server.app.create_app`.
    * ``resolve_user`` — ``Request -> actor dict | None``. Cookie/Bearer parsing
      is the port's business; the actor dict carries ``id``, ``email``,
      ``org_id`` and ``auth == "pat"`` for token actors.
    * ``admin_emails`` — ``() -> set[str]``; an empty set means no admin.
    * ``project_access`` — the project identity + role-ladder port.
    * ``org_role`` — optional ``(user_id, org_id) -> role``; without it the
      org-wide usage view is restricted to server admins.
    * ``deny_code_execution`` — optional deployment policy; without it a team
      server (which runs its own workers) allows code recipes.

    Raises :class:`TeamEnforcementError` if any registered ``/api`` route is not
    classified by the base endpoint catalog: an unclassified route must stop the
    server, never default-allow.
    """

    enforcer = _Enforcer(
        app=app,
        resolve_user=resolve_user,
        admin_emails=admin_emails,
        project_access=project_access,
        org_role=org_role,
        deny_code_execution=deny_code_execution,
    )
    app.add_middleware(TeamEnforcementMiddleware, enforcer=enforcer)
    app.router.on_startup.append(enforcer.verify_inventory)
    return app


__all__ = [
    "CODE_EXECUTION_DENIED_DETAIL",
    "CORE_ROUTE_OWNER",
    "AdminEmails",
    "DenyCodeExecution",
    "OrgRole",
    "ProjectAccess",
    "ResolveUser",
    "TeamEnforcementError",
    "TeamEnforcementMiddleware",
    "protect_core_app",
]
