"""Workbench plugin services for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.server.workspace import Workspace
from frisket.authoring.workbench.marketplace_policy import (
    workbench_marketplace_install_attempt,
    workbench_marketplace_policy,
)
from frisket.authoring.workbench.plugin_runtime import (
    bootstrap_project_bundled_plugins,
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
    disable_workbench_plugin,
    execute_workbench_plugin_local_install_plan,
    uninstall_workbench_plugin,
)
from frisket.authoring.workbench.plugin_runtime_settings import (
    delete_workbench_plugin_env_var,
    list_workbench_plugin_env_vars,
    list_workbench_plugin_settings,
    patch_workbench_plugin_settings,
    set_workbench_plugin_env_var,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    workbench_plugin_frontend_component_module,
    workbench_plugin_runtime_index,
)


class WorkbenchService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace
        # Projects created before bundled plugins existed never ran the
        # creation-time bootstrap, so their runtime index is empty and every
        # bundled surface (the frisket.geo map view) silently disappears for
        # them. bootstrap_project_bundled_plugins is idempotent BY CONTRACT
        # (same idempotency keys; explicit operator disable/uninstall is left
        # alone), so ensuring it at index-read time is safe; the per-process
        # set just keeps repeat reads cheap.
        self._bundled_bootstrapped: set[str] = set()

    def plugin_index(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project_id not in self._bundled_bootstrapped:
            bootstrap_project_bundled_plugins(project, project_id=project_id)
            self._bundled_bootstrapped.add(project_id)
        return workbench_plugin_runtime_index(
            project,
            project_id=project_id,
        )

    def frontend_component_module(
        self,
        project_id: str,
        *,
        plugin_id: str,
        contribution_id: str,
        expected_package_sha256: str | None = None,
    ) -> str:
        return workbench_plugin_frontend_component_module(
            self._workspace.get(project_id),
            plugin_id=plugin_id,
            contribution_id=contribution_id,
            expected_package_sha256=expected_package_sha256,
        )

    def marketplace(
        self,
        project_id: str,
        *,
        contribution_id: str,
        text: str,
    ) -> dict[str, Any]:
        self._workspace.get(project_id)
        return workbench_marketplace_policy(
            project_id=project_id,
            contribution_id=contribution_id,
            text=text,
        )

    def marketplace_install_attempt(
        self,
        project_id: str,
        *,
        plugin_id: str,
        version: str | None,
        source: dict[str, Any],
        arbitrary_package_load_allowed: bool,
    ) -> tuple[int, dict[str, Any]]:
        self._workspace.get(project_id)
        return workbench_marketplace_install_attempt(
            project_id=project_id,
            plugin_id=plugin_id,
            version=version,
            source=source,
            arbitrary_package_load_allowed=arbitrary_package_load_allowed,
        )

    def install_local(
        self,
        project_id: str,
        *,
        plugin_id: str,
        source: dict[str, Any],
        arbitrary_package_load_allowed: bool,
    ) -> tuple[int, dict[str, Any]]:
        with self._workspace.plugin_package_lifecycle():
            return execute_workbench_plugin_local_install_plan(
                self._workspace.get(project_id),
                project_id=project_id,
                plugin_id=plugin_id,
                source=source,
                arbitrary_package_load_allowed=arbitrary_package_load_allowed,
            )

    def plugin_env_vars(self, project_id: str, *, plugin_id: str) -> dict[str, Any]:
        return list_workbench_plugin_env_vars(
            self._workspace.get(project_id),
            project_id=project_id,
            plugin_id=plugin_id,
        )

    def set_plugin_env_var(
        self,
        project_id: str,
        *,
        plugin_id: str,
        name: str,
        value: str,
    ) -> dict[str, Any]:
        return set_workbench_plugin_env_var(
            self._workspace.get(project_id),
            project_id=project_id,
            plugin_id=plugin_id,
            name=name,
            value=value,
        )

    def delete_plugin_env_var(
        self,
        project_id: str,
        *,
        plugin_id: str,
        name: str,
    ) -> dict[str, Any]:
        return delete_workbench_plugin_env_var(
            self._workspace.get(project_id),
            project_id=project_id,
            plugin_id=plugin_id,
            name=name,
        )

    def plugin_settings(self, project_id: str, *, plugin_id: str) -> dict[str, Any]:
        return list_workbench_plugin_settings(
            self._workspace.get(project_id),
            project_id=project_id,
            plugin_id=plugin_id,
        )

    def patch_plugin_settings(
        self,
        project_id: str,
        *,
        plugin_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        return patch_workbench_plugin_settings(
            self._workspace.get(project_id),
            project_id=project_id,
            plugin_id=plugin_id,
            values=values,
        )

    def activate_manifest(
        self,
        project_id: str,
        *,
        plugin_id: str,
        receipt_id: str,
        trust_acknowledged: bool,
        permissions_accepted: list[str],
        arbitrary_package_load_allowed: bool,
    ) -> dict[str, Any]:
        with self._workspace.plugin_package_lifecycle():
            return activate_workbench_plugin_manifest(
                self._workspace.get(project_id),
                project_id=project_id,
                plugin_id=plugin_id,
                receipt_id=receipt_id,
                trust_acknowledged=trust_acknowledged,
                permissions_accepted=permissions_accepted,
                arbitrary_package_load_allowed=arbitrary_package_load_allowed,
            )

    def activate_backend(
        self,
        project_id: str,
        *,
        plugin_id: str,
        trust_acknowledged: bool,
        arbitrary_package_load_allowed: bool,
        executable_handlers_allowed: bool,
    ) -> dict[str, Any]:
        with self._workspace.plugin_package_lifecycle():
            return activate_workbench_plugin_backend_contributions(
                self._workspace.get(project_id),
                project_id=project_id,
                plugin_id=plugin_id,
                trust_acknowledged=trust_acknowledged,
                arbitrary_package_load_allowed=arbitrary_package_load_allowed,
                executable_handlers_allowed=executable_handlers_allowed,
            )

    def disable(self, project_id: str, *, plugin_id: str) -> dict[str, Any]:
        with self._workspace.plugin_package_lifecycle():
            return disable_workbench_plugin(
                self._workspace.get(project_id),
                project_id=project_id,
                plugin_id=plugin_id,
            )

    def uninstall(self, project_id: str, *, plugin_id: str) -> dict[str, Any]:
        with self._workspace.plugin_package_lifecycle():
            project = self._workspace.get(project_id)
            workspace_projects = tuple(
                self._workspace.get(path.stem)
                for path in self._workspace.root.glob("*.frisket")
                if (path / "project.db").is_file()
            )
            return uninstall_workbench_plugin(
                project,
                project_id=project_id,
                plugin_id=plugin_id,
                workspace_projects=workspace_projects,
            )
