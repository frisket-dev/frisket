"""Durable canonical-writer and plugin action-kind integration checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import ActionError
from frisket.engine.executor import resolve_map_preview, run_action_spec
from frisket.engine.runner import MapRunner
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from runner_test_helpers import run_with_output_claim


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_sheet(project: Project) -> int:
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(
        sheet,
        [{"text": "call 212-555-0123"}, {"text": "no phone"}],
        cols,
    )
    return sheet


def _run_row(project: Project, run_id: int) -> Any:
    return project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def _run_recipe(project: Project, spec: dict) -> Any:
    router = ModelRouter(keys={"anthropic": "k"})
    return asyncio.run(
        run_with_output_claim(
            MapRunner(project, router, authority=UnroutedOnlyAuthority(project)),
            spec,
        )
    )


def _python_request(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["text"],
            "code": "result = row['text']",
            "return_schema": {"type": "string"},
            "output_routes": [
                {
                    "name": "copy",
                    "path": "$",
                    "target": {"kind": "column", "type": "text"},
                }
            ],
        },
        "output_names": {"copy": "copy"},
        "idempotency_key": "canonical-python-kind",
    }


# ---------------------------------------------------------------------------
# canonical writers (start_run dual-write)
# ---------------------------------------------------------------------------


class TestStartRunCanonicalIdentity:
    def test_start_run_persists_only_canonical_action_identity(
        self, tmp_path: Path
    ) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            op_id = project.append_op("map", {"recipe": "extract"}, label="seed")
            run_id = RunResultStore(project).start_run(
                op_id, sheet, "map.extract", action_version="2"
            )
            row = _run_row(project, run_id)
            assert row["action_kind"] == "map.extract"
            assert row["action_version"] == "2"
            assert "recipe" not in row.keys()
            assert "recipe_version" not in row.keys()
        finally:
            project.close()

    def test_start_run_rejects_bare_implementation_identity(
        self, tmp_path: Path
    ) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            op_id = project.append_op("map", {"recipe": "plugin_owned"}, label="seed")
            with pytest.raises(ValueError, match="not canonical"):
                RunResultStore(project).start_run(op_id, sheet, "plugin_owned")
            assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            project.close()

    def test_start_run_unmapped_recipe_id_falls_back_to_identity(
        self, tmp_path: Path
    ) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            op_id = project.append_op("embedding.index_refresh", {}, label="seed")
            run_id = RunResultStore(project).start_run(
                op_id, sheet, "embedding.index_refresh"
            )
            row = _run_row(project, run_id)
            assert row["action_kind"] == "embedding.index_refresh"
            assert "recipe" not in row.keys()
        finally:
            project.close()


class TestMapRunnerCanonicalWriter:
    def test_recipe_only_runner_spec_is_terminally_refused(
        self, tmp_path: Path
    ) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            with pytest.raises(ValueError, match="must not carry top-level"):
                _run_recipe(
                    project,
                    {
                        "recipe": "regex_extract",
                        "sheet_id": sheet,
                        "input_columns": ["text"],
                        "pattern": r"\d{3}-\d{3}-\d{4}",
                        "output_name": "phone",
                    },
                )
            assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            project.close()

    @pytest.mark.parametrize(
        "invalid_spec",
        [
            {},
            {"action_kind": None},
            {"action_kind": ""},
            {"action_kind": " map.regex_extract"},
            {"action_kind": "map.regex_extract "},
            {"action_kind": "python"},
            {"action_kind": "agent"},
            {"action_kind": "classify"},
            {"action_kind": "regex_extract"},
            {"action_kind": "plugin_owned"},
            {"action_kind": "map.regex_extract", "recipe": "regex_extract"},
        ],
    )
    def test_invalid_runner_identity_refuses_before_recipe_lookup(
        self,
        invalid_spec: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from frisket.engine.runner import validation

        calls: list[str] = []

        def unexpected_lookup(name: str) -> Any:
            calls.append(name)
            raise AssertionError("recipe lookup must follow canonical admission")

        monkeypatch.setattr(validation, "get_recipe", unexpected_lookup)
        with pytest.raises(ValueError):
            validation.recipe_for_spec(invalid_spec)
        assert calls == []

    def test_spec_carrying_action_kind_resolves_and_stamps_it(
        self, tmp_path: Path
    ) -> None:
        """The typed program persists the same canonical identity as admission."""
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            result = run_action_spec(
                project, _python_request(sheet), project_id="canonical-python-kind"
            )
            assert result.status == "completed", result.errors
            row = _run_row(project, result.run_id)
            assert row["action_kind"] == "map.python"
            assert row["action_version"] == "1"
            assert "recipe" not in row.keys()
            # The persisted runner spec carries canonical identity only.
            params = json.loads(row["params"])
            assert params["action_kind"] == "map.python"
            assert "recipe" not in params
        finally:
            project.close()


# ---------------------------------------------------------------------------
# the SDK declaration carries one canonical identity
# ---------------------------------------------------------------------------


class TestSdkDeclContract:
    def test_op_declaration_exposes_no_runner_identity_shadow(self) -> None:
        from frisket.sdk.declaration import Op

        fields = set(Op.__dataclass_fields__)
        assert "runner_kind" not in fields
        assert "recipe_name" not in fields

    def _minimal_op_kwargs(self) -> dict[str, Any]:
        from frisket.contracts.action import (
            CostPolicy,
            IdempotencyPolicy,
            RetryPolicy,
        )

        return {
            "kind": "map.example",
            "title": "Example",
            "description": "Example op",
            "params_model": object,
            "output_model": object,
            "errors": (),
            "side_effects": (),
            "cost": CostPolicy(kind="none"),
            "idempotency": IdempotencyPolicy(
                supported=True,
                scope="project",
                key_field="idempotency_key",
                behavior="n/a",
            ),
            "retry": RetryPolicy(supported=True, strategy="idempotency_replay"),
            "examples": (),
            "primary_fields": (),
        }

    def test_op_factory_rejects_the_retired_recipe_name_kwarg(self) -> None:
        from frisket.sdk.declaration import op

        with pytest.raises(TypeError):
            op(recipe_name="extract", **self._minimal_op_kwargs())

    def test_op_factory_carries_only_the_canonical_kind(self) -> None:
        from frisket.sdk.declaration import op

        decl = op(**self._minimal_op_kwargs())
        assert decl.kind == "map.example"

    def test_first_party_declarations_use_only_typed_canonical_kinds(self) -> None:
        import importlib
        import pkgutil

        import frisket.sdk.ops as ops_pkg
        from frisket.actions.registry import ACTION_REGISTRY
        from frisket.sdk.declaration import Op

        decls: list[Op] = []
        for mod_info in pkgutil.iter_modules(ops_pkg.__path__):
            module = importlib.import_module(f"frisket.sdk.ops.{mod_info.name}")
            decls.extend(
                value for value in vars(module).values() if isinstance(value, Op)
            )
        assert decls == [], "first-party actions must not retain SDK controllers"
        for kind in (
            "map.ask",
            "map.summarize",
            "map.classify",
            "map.ner",
            "map.translate",
            "map.mcp_extract",
            "map.extract",
            "map.judge",
        ):
            assert ACTION_REGISTRY.get(kind).action_id == kind

    def test_generated_runner_specs_carry_only_action_kind_identity(
        self,
        tmp_path: Path,
    ) -> None:
        project = Project.create(tmp_path / "p.frisket")
        try:
            sheet = _seed_sheet(project)
            plan = resolve_map_preview(project, _python_request(sheet))
            assert not isinstance(plan, ActionError), plan
            assert plan.runner_spec["action_kind"] == "map.python"
            assert plan.runner_spec["sheet_id"] == sheet
            assert "recipe" not in plan.runner_spec
            assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            project.close()

    def test_get_recipe_refuses_typed_action_kinds(self) -> None:
        from frisket.ops.builtin import get_recipe

        # Typed actions execute through an explicit MapRunner program and are
        # deliberately absent from the legacy recipe registry.
        for kind in (
            "map.extract",
            "map.classify",
            "map.ner",
            "map.translate",
            "map.summarize",
            "map.judge",
            "map.ask",
            "map.mcp_extract",
        ):
            with pytest.raises(ValueError, match="unknown action kind"):
                get_recipe(kind)
        with pytest.raises(ValueError, match="unknown action kind"):
            get_recipe("media.ytdlp_download")
        with pytest.raises(ValueError, match="unknown action kind"):
            get_recipe("map.template")
        with pytest.raises(ValueError, match="unknown action kind"):
            get_recipe("map.clean_dates")
        with pytest.raises(ValueError, match="unknown action kind"):
            get_recipe("map.python")
        # Short implementation names are private details, not lookup keys.
        with pytest.raises(ValueError, match="not canonical"):
            get_recipe("extract")
        with pytest.raises(ValueError, match="not canonical"):
            get_recipe("download_media")


# ---------------------------------------------------------------------------
# A plugin can register an
# action kind equal to a built-in recipe IMPL id ("python", "agent"). Plugin
# activation rejected only V1_ACTION_KINDS ("map.python"), NOT the legacy impl
# ids. A run stamped action_kind="python" then had run_row_action_kind return
# the RAW "python", so team enforcement authorized "python" while a canonical
# "map.python" deny-list never matched -> the hosted deny-callback path
# executed get_recipe("python")=PythonRecipe on stored code: a code-execution
# bypass. Fixes: (1) run_row_action_kind canonicalizes the stamped column too;
# (2) plugin activation rejects any kind get_recipe() resolves.
# ---------------------------------------------------------------------------


class TestCanonicalLookupBoundary:
    @pytest.mark.parametrize(
        "kind", ["agent", "regex_extract", "classify", "plugin_owned", "python"]
    )
    def test_bare_implementation_ids_are_refused(self, kind: str) -> None:
        from frisket.ops.builtin import get_recipe

        with pytest.raises(ValueError, match="not canonical"):
            get_recipe(kind)

    @pytest.mark.parametrize(
        "kind", ["agent", "regex_extract", "classify", "plugin_owned"]
    )
    def test_plugin_activation_rejects_noncanonical_ids(self, kind: str) -> None:
        from types import SimpleNamespace

        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            _validate_public_action_collision,
        )
        from frisket.authoring.workbench.plugin_runtime_shared import (
            WorkbenchPluginActivationError,
        )

        with pytest.raises(WorkbenchPluginActivationError):
            _validate_public_action_collision(SimpleNamespace(kind=kind))

    def test_namespaced_plugin_kind_is_valid(self) -> None:
        from types import SimpleNamespace

        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            _validate_public_action_collision,
        )

        _validate_public_action_collision(SimpleNamespace(kind="demo.plugin.op.clean"))
