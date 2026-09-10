"""export.google_sheets edition/composition gate (bugfix, 2026-07-30).

The action is registered unconditionally in the catalog
(actions/exports.py) but only actually
runs when this composition wired `ExecutorDeps.connected_account_resolver` —
the bare local single-user tier (`frisket.server.app.create_app` with no
`executor_deps_factory`) never does; team/hosted do. Before this fix, the
local tier showed the action, let a user fill out the whole form, and only
then failed with `connected_account_not_found` at run time. This pins the
read-only catalog mirror (`_apply_connected_account_hints`,
server/action_catalog_hints.py) and its live-route wiring
(server/routes/actions.py), the SAME "read-only mirror of an authoritative
gate" pattern as `missing_credentials`/`network_disabled` right above it in
that file.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.engine.executor import (
    CellEditQueryLimits,
    ExecutorDeps,
    ImportWorkloadLimits,
    UrlImportLimits,
)
from frisket.engine.store import Project
from frisket.server.action_catalog_hints import (
    project_action_catalog_payload_with_launcher_hints,
)
from frisket.server.app import create_app
from frisket.server.exports.sheet_csv import SheetExportLimits


def _hints_payload(project, **kwargs):
    return project_action_catalog_payload_with_launcher_hints(
        project, sidecar_capabilities={}, **kwargs
    )


def _google_sheets_entry(payload):
    return next(a for a in payload["actions"] if a["kind"] == "export.google_sheets")


class TestCatalogHintFunction:
    """Fast, no-HTTP proof of the hint-computation function itself."""

    def test_unavailable_when_no_resolver_configured(self, tmp_path) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            entry = _google_sheets_entry(
                _hints_payload(project, connected_account_resolver_configured=False)
            )
            reason = entry["ui_hints"].get("unavailable_reason")
            assert reason, entry["ui_hints"]
            assert "team edition" in reason.lower() or "team" in reason.lower()
        finally:
            project.close()

    def test_available_when_resolver_configured(self, tmp_path) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            entry = _google_sheets_entry(
                _hints_payload(project, connected_account_resolver_configured=True)
            )
            assert "unavailable_reason" not in entry["ui_hints"]
        finally:
            project.close()

    def test_default_is_available_unchanged_behavior_for_untouched_callers(
        self, tmp_path
    ) -> None:
        """A caller that doesn't know about this new kwarg yet (there is
        exactly one production caller, server/routes/actions.py, and it DOES
        pass it — but the permissive default matters for any other/future
        caller and for direct unit-test use like `_hints_payload` above)
        must see unchanged (available) behavior, not a surprise regression."""
        project = Project.create(tmp_path / "p.frisket")
        try:
            entry = _google_sheets_entry(_hints_payload(project))
            assert "unavailable_reason" not in entry["ui_hints"]
        finally:
            project.close()

    def test_only_export_google_sheets_is_gated(self, tmp_path) -> None:
        """The gate must not spill onto unrelated actions merely because no
        resolver is configured -- only kinds actually declared in
        _CONNECTED_ACCOUNT_UNAVAILABLE_REASONS."""
        project = Project.create(tmp_path / "p.frisket")
        try:
            payload = _hints_payload(
                project, connected_account_resolver_configured=False
            )
            gated = [
                a["kind"]
                for a in payload["actions"]
                if "unavailable_reason" in (a.get("ui_hints") or {})
            ]
            assert gated == ["export.google_sheets"]
        finally:
            project.close()


class TestLiveRoute:
    """End-to-end through the real HTTP catalog route, proving
    server/routes/actions.py actually threads
    `workspace.executor_deps_factory is not None` -- not just that the
    underlying function behaves correctly in isolation (TestCatalogHintFunction
    above already proves that; this proves the wire is connected)."""

    def test_bare_local_tier_reports_unavailable(self, tmp_path) -> None:
        # The exact composition a real `frisket <workspace-dir>` local-tier
        # server uses: no private executor_deps_factory.  Default-unlimited
        # policy objects must not synthesize one either, because its presence
        # is the public catalog signal that connected accounts are available.
        client = TestClient(create_app(tmp_path / "workspace"))
        assert client.app.state.workspace.executor_deps_factory is None
        pid = client.post("/api/projects", json={"name": "Local Tier"}).json()["id"]

        catalog = client.get(f"/api/projects/{pid}/actions/v1/catalog")
        assert catalog.status_code == 200, catalog.text
        entry = _google_sheets_entry(catalog.json())
        reason = entry["ui_hints"].get("unavailable_reason")
        assert reason, entry["ui_hints"]
        assert "team" in reason.lower()

    def test_explicit_import_limit_composes_and_enforces_without_private_factory(
        self, tmp_path
    ) -> None:
        """A finite deployment policy still needs the wrapper that carries it.

        This distinguishes the Solo-default repair from simply removing
        create_app's executor-dependency composition altogether.
        """
        client = TestClient(
            create_app(
                tmp_path / "workspace",
                import_workload_limits=ImportWorkloadLimits(max_rows=1),
            )
        )
        factory = client.app.state.workspace.executor_deps_factory
        assert factory is not None
        assert factory("unused-project-id", None).import_workload_limits == (
            ImportWorkloadLimits(max_rows=1)
        )

        pid = client.post("/api/projects", json={"name": "Limited Tier"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "import.rows",
                "scope": {"kind": "project"},
                "sheet_name": "Must not publish",
                "params": {
                    "columns": [{"name": "value", "type": "text"}],
                    "rows": [{"value": "one"}, {"value": "two"}],
                    "source": {"kind": "inline", "label": "limit proof"},
                },
                "idempotency_key": "limited-import@sha256:two-rows",
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["errors"][0]["code"] == "import_workload_limit_exceeded"
        project = client.app.state.workspace.get(pid)
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0

    def test_private_dependencies_survive_omitted_policies_and_sheet_override(
        self, tmp_path
    ) -> None:
        """A service default must not overwrite private request dependencies."""
        private_sheet_limits = SheetExportLimits(max_rows=7)
        private_url_limits = UrlImportLimits(max_urls=8)
        private_workload_limits = ImportWorkloadLimits(max_rows=9)
        private_query_edit_limits = CellEditQueryLimits(max_rows=10)
        router_sentinel = object()
        sheets_client_sentinel = object()
        private_deps = ExecutorDeps(
            router=router_sentinel,
            google_sheets_client=sheets_client_sentinel,
            sheet_export_limits=private_sheet_limits,
            url_import_limits=private_url_limits,
            import_workload_limits=private_workload_limits,
            cell_edit_query_limits=private_query_edit_limits,
        )

        omitted_calls: list[tuple[str, object]] = []

        def private_factory(project_id, request):
            omitted_calls.append((project_id, request))
            return private_deps

        client = TestClient(
            create_app(tmp_path / "omitted", executor_deps_factory=private_factory)
        )
        factory = client.app.state.workspace.executor_deps_factory
        assert factory is not None
        observed = factory("private-project", None)
        assert omitted_calls == [("private-project", None)]
        assert observed.sheet_export_limits is private_sheet_limits
        assert observed.url_import_limits is private_url_limits
        assert observed.import_workload_limits is private_workload_limits
        assert observed.cell_edit_query_limits is private_query_edit_limits
        assert observed.router is router_sentinel
        assert observed.google_sheets_client is sheets_client_sentinel

        override_calls: list[tuple[str, object]] = []

        def override_factory(project_id, request):
            override_calls.append((project_id, request))
            return private_deps

        top_level_sheet_limits = SheetExportLimits(max_rows=10)
        override_client = TestClient(
            create_app(
                tmp_path / "override",
                executor_deps_factory=override_factory,
                sheet_export_limits=top_level_sheet_limits,
            )
        )
        override = override_client.app.state.workspace.executor_deps_factory
        assert override is not None
        overridden = override("private-project", None)
        assert override_calls == [("private-project", None)]
        assert overridden.sheet_export_limits is top_level_sheet_limits
        assert overridden.url_import_limits is private_url_limits
        assert overridden.import_workload_limits is private_workload_limits
        assert overridden.cell_edit_query_limits is private_query_edit_limits
        assert overridden.router is router_sentinel
        assert overridden.google_sheets_client is sheets_client_sentinel

    def test_a_composition_with_a_resolver_reports_available(self, tmp_path) -> None:
        def deps_factory(_project_id, _request):
            return ExecutorDeps(
                connected_account_resolver=lambda provider, connection_id: None,
                google_sheets_client=object(),
            )

        client = TestClient(
            create_app(tmp_path / "workspace", executor_deps_factory=deps_factory)
        )
        pid = client.post("/api/projects", json={"name": "Wired Tier"}).json()["id"]

        catalog = client.get(f"/api/projects/{pid}/actions/v1/catalog")
        assert catalog.status_code == 200, catalog.text
        entry = _google_sheets_entry(catalog.json())
        assert "unavailable_reason" not in entry["ui_hints"]
