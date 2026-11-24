"""Declared action requirements, shared by HTTP, CLI and resolved inner work.

Route-dependent requirements still belong to execution composition. This gate
handles only the capabilities and secrets declared by the resolved action.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from frisket.authoring.action_metadata import (
    CODE_RECIPE_GATED_CAPABILITY_RULES,
    action_available_in_edition,
    action_edition_unavailable_message,
)
from frisket.contracts.action import ActionError
from frisket.project_service_access import project_service_access
from frisket.service_access import ServiceNotAllowed

if TYPE_CHECKING:
    from frisket.actions.core import RegisteredAction
    from frisket.authoring.plugin_registry import RuntimeBindingSpec
    from frisket.engine.store import Project


def missing_action_credential_error(
    action_kind: str, missing: list[str]
) -> ActionError:
    return ActionError(
        code="missing_action_credential",
        message=(
            f"'{action_kind}' requires {', '.join(missing)} to run. Add it in "
            "Settings → Secrets, then try again."
        ),
        action_kind=action_kind,
        details={
            "missing_credentials": missing,
            "settings_scope": "project",
            "settings_section": "secrets",
        },
    )


_SERVER_PATH_KINDS = frozenset({"local_file", "local_dir"})
# The parameters in which an action names a file reference. Anything else in
# the params, row data included, is the caller's own data and is never read
# as a path.
_SERVER_PATH_PARAMS = ("destination", "source", "manifest")


def _server_path_field(params: object) -> str | None:
    if not isinstance(params, Mapping):
        return None
    for name in _SERVER_PATH_PARAMS:
        reference = params.get(name)
        if not isinstance(reference, Mapping):
            continue
        kind = reference.get("kind")
        if isinstance(kind, str) and kind in _SERVER_PATH_KINDS:
            return f"params.{name}.path"
    return None


def server_path_admission_error(action_kind: str, params: object) -> ActionError | None:
    """Refuse a request whose params name a server path source or destination.

    Callers apply this outside the local edition. Any ``local_file`` or
    ``local_dir`` source or destination names a path on the server, whatever
    the action; downloads and project-file destinations stay available.
    """
    field = _server_path_field(params)
    if field is None:
        return None
    return ActionError(
        code="server_path_unavailable",
        message=(
            f"'{action_kind}' names a file path on the server, which is not "
            "available on this deployment; download or upload the file instead"
        ),
        action_kind=action_kind,
        field=field,
    )


def action_admission_error(
    project: Project | None,
    action: RegisteredAction,
    *,
    edition: str | None = None,
    runtime_binding: RuntimeBindingSpec | None = None,
) -> ActionError | None:
    """Return a refusal before any action effect; None admits declared needs.

    A projectless call checks deployment policy only. The resolved inner action must pass this gate itself;
    permission to Retry/Refresh is not permission to its target's capabilities.
    """
    kind = action.action_id
    if edition is not None and not action_available_in_edition(kind, edition):
        return ActionError(
            code="action_unavailable_in_edition",
            message=action_edition_unavailable_message(kind, edition),
            action_kind=kind,
            field="kind",
        )
    capabilities = frozenset(action.required_capabilities)
    if os.environ.get("FRISKET_ALLOW_CODE_RECIPES", "1") != "1":
        for rule in CODE_RECIPE_GATED_CAPABILITY_RULES:
            if rule.capabilities <= capabilities:
                return ActionError(
                    code="code_action_disabled",
                    message=f"the '{kind}' action declares {rule.phrase}, which is disabled on this server; enable it only on trusted/local deployments",
                    action_kind=kind,
                    field="kind",
                )
    if project is None:
        return None
    from frisket.credentials import missing_required_credentials

    if runtime_binding is not None:
        from frisket.authoring.workbench.plugin_subprocess import (
            _missing_project_plugin_secrets,
        )

        # Admission must resolve the same credential owner as the invocation.
        # A plugin's configured key lives in its own scope, not core secrets.
        missing = _missing_project_plugin_secrets(
            project,
            plugin_id=runtime_binding.plugin,
            metadata={"requires_secrets": list(action.required_credentials)},
        )
    else:
        missing = missing_required_credentials(
            project, list(action.required_credentials)
        )
    if missing:
        return missing_action_credential_error(kind, missing)
    external = next(
        (cap for cap in action.required_capabilities if cap.startswith("external:")),
        None,
    )
    if external is None:
        return None
    return external_service_error(project, kind, external)


def external_service_error(
    project: Project, action_kind: str, capability: str
) -> ActionError | None:
    """Refuse an external capability the project's settings do not allow."""
    try:
        project_service_access(project).check_remote_service(capability)
    except ServiceNotAllowed as exc:
        return ActionError(
            code=exc.code,
            message=str(exc),
            action_kind=action_kind,
            details={"capability": capability},
        )
    return None
