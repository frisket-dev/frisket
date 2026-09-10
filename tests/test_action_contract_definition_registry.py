from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "src" / "frisket" / "contracts"


def test_action_presentation_catalog_projection_emits_served_truth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.ci.gen_action_presentation_catalog_fixture import main

    assert main(["--kind", "map.ner", "--kind", "cluster.values"]) == 0
    emitted = capsys.readouterr()
    payload = json.loads(emitted.out)
    assert payload["schema_version"] == "frisket.action_catalog.v2"
    assert [entry["kind"] for entry in payload["actions"]] == [
        "map.ner",
        "cluster.values",
    ]
    assert all(
        "available" not in engine
        for entry in payload["actions"]
        for engine in entry["ui_hints"].get("engines", [])
    )

    assert main(["--check"]) == 0
    assert "projection is valid" in capsys.readouterr().out


def test_action_presentation_catalog_generator_check_starts_cold() -> None:
    """The generated catalog check must work before any package is imported."""
    env = os.environ.copy()
    source_root = str(ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/ci/gen_action_presentation_catalog_fixture.py"),
            "--check",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "normalized served action-catalog projection is valid" in result.stdout


def test_typed_catalog_authoring_contract() -> None:
    from frisket.contracts.action import (
        CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
        ActionCatalogEntry,
    )
    from frisket.actions.system import root_action_catalog

    merged = root_action_catalog()
    assert merged.schema_version == "frisket.action_catalog.v2"
    assert CURRENT_ACTION_AUTHORING_CONTRACT_VERSION == 1
    assert merged.actions
    assert all(
        entry.authoring_contract_version == CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
        for entry in merged.actions
    )
    dual_prompt_targets = sorted(
        entry.kind
        for entry in merged.actions
        if {"instruction", "question"}
        <= set((entry.input_schema.get("properties") or {}))
    )
    assert not dual_prompt_targets

    current_entry = merged.actions[0]
    current_payload = current_entry.model_dump()
    current_payload.pop("authoring_contract_version")
    assert (
        ActionCatalogEntry.model_validate(current_payload).authoring_contract_version
        == CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
    )
    assert (
        ActionCatalogEntry.model_validate(
            {
                **current_payload,
                "authoring_contract_version": CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
            }
        ).authoring_contract_version
        == CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
    )
    for invalid_version in (False, True, "1", 0, 1.0, -1, 2):
        with pytest.raises(ValueError):
            ActionCatalogEntry.model_validate(
                {**current_payload, "authoring_contract_version": invalid_version}
            )

    assert not (CONTRACTS / "actions/catalog_entries.py").exists()
    assert not (CONTRACTS / "actions/validation_prechecks.py").exists()
    assert not (CONTRACTS / "actions/definitions").exists()


def test_action_job_binding_registry_invariants() -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor.action_bindings import (
        _contributed_action_job_executors,
        action_job_bindings,
    )
    from frisket.engine.executor.temporal_extract_action import (
        run_typed_temporal_extract_job,
    )
    from frisket.engine.executor.action_specs import (
        declared_queued_action_job_kinds,
    )
    from frisket.engine.executor.temporal_split import (
        ACTION_KIND as TEMPORAL_SPLIT_ACTION_KIND,
    )
    from frisket.engine.executor.table_action import run_typed_table_action_job

    rows = _contributed_action_job_executors()
    temporal_extract_range_kind = "temporal.extract_range"

    assert rows["derive.transcript_segments"] is run_typed_table_action_job
    assert rows[TEMPORAL_SPLIT_ACTION_KIND] is run_typed_table_action_job
    assert rows[temporal_extract_range_kind] is run_typed_temporal_extract_job
    assert len(rows) == 5
    assert tuple(inspect.signature(rows["export.google_sheets"]).parameters) == (
        "project",
        "envelope",
    )
    assert not hasattr(action_job_bindings, "cache_clear")

    bindings_by_kind = action_job_bindings()
    assert set(bindings_by_kind) <= set(ACTION_REGISTRY.action_ids)
    assert set(bindings_by_kind) == {
        "derive.transcript_segments",
        TEMPORAL_SPLIT_ACTION_KIND,
        temporal_extract_range_kind,
        "export.google_sheets",
        "map.find",
    }
    for kind, executor in (
        (TEMPORAL_SPLIT_ACTION_KIND, run_typed_table_action_job),
        (temporal_extract_range_kind, run_typed_temporal_extract_job),
    ):
        assert bindings_by_kind[kind] is executor
        assert kind in declared_queued_action_job_kinds()
        assert ACTION_REGISTRY.get(kind).action_id == kind
    assert "derive.transcript_segments" in declared_queued_action_job_kinds()
    assert (
        ACTION_REGISTRY.get("derive.transcript_segments").action_id
        == "derive.transcript_segments"
    )
    assert "export.google_sheets" in declared_queued_action_job_kinds()
    assert "map.find" in declared_queued_action_job_kinds()
    assert tuple(inspect.signature(rows["map.find"]).parameters) == (
        "project",
        "envelope",
    )
    assert (
        ACTION_REGISTRY.get("export.google_sheets").action_id == "export.google_sheets"
    )
    assert "embedding.index_refresh" not in bindings_by_kind


def test_google_sheets_action_job_binding_resolves_composition_deps_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor import ExecutorDeps
    from frisket.engine.executor import action_bindings

    project = object()
    envelope = SimpleNamespace(project_id="project-action-06c")
    first_deps = ExecutorDeps()
    second_deps = ExecutorDeps()
    factory_calls: list[tuple[str, object | None, str]] = []
    executor_calls: list[tuple[object, object, ExecutorDeps]] = []
    sentinel = object()

    def factory_for(label: str, deps: ExecutorDeps):
        def factory(project_id: str, request: object | None) -> ExecutorDeps:
            factory_calls.append((project_id, request, label))
            return deps

        return factory

    def fake_run_export(
        received_project: object,
        received_envelope: object,
        *,
        deps: ExecutorDeps,
    ) -> object:
        executor_calls.append((received_project, received_envelope, deps))
        return sentinel

    monkeypatch.setattr(
        action_bindings,
        "execute_google_sheets_job",
        fake_run_export,
    )
    first = action_bindings.action_job_bindings(
        executor_deps_factory=factory_for("first", first_deps)
    )
    second = action_bindings.action_job_bindings(
        executor_deps_factory=factory_for("second", second_deps)
    )

    # Composition is pure: dependency lookup belongs to operation execution.
    assert factory_calls == []
    first_operation = first["export.google_sheets"]
    second_operation = second["export.google_sheets"]
    assert tuple(inspect.signature(first_operation).parameters) == (
        "project",
        "envelope",
    )
    assert first_operation(project, envelope) is sentinel
    assert second_operation(project, envelope) is sentinel
    assert first_operation(project, envelope) is sentinel
    assert factory_calls == [
        ("project-action-06c", None, "first"),
        ("project-action-06c", None, "second"),
        ("project-action-06c", None, "first"),
    ]
    assert [call[2] for call in executor_calls] == [
        first_deps,
        second_deps,
        first_deps,
    ]

    none_factory = action_bindings.action_job_bindings(
        executor_deps_factory=lambda _project_id, _request: None
    )["export.google_sheets"]
    bare = action_bindings.action_job_bindings()["export.google_sheets"]
    none_factory(project, envelope)
    bare(project, envelope)
    bare(project, envelope)
    fallback_deps = [call[2] for call in executor_calls[-3:]]
    assert all(isinstance(deps, ExecutorDeps) for deps in fallback_deps)
    assert fallback_deps[0] is not fallback_deps[1]
    assert fallback_deps[1] is not fallback_deps[2]


def test_action_job_binding_registry_rejects_misplaced_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor import action_bindings

    executor = next(iter(action_bindings._contributed_action_job_executors().values()))
    monkeypatch.setattr(
        action_bindings,
        "_contributed_action_job_executors",
        lambda **_kwargs: {"cell.edit": executor},
    )
    with pytest.raises(
        RuntimeError,
        match="action-job action bindings lack declared queued placement",
    ):
        action_bindings.action_job_bindings()


def test_action_job_binding_registry_rejects_missing_canonical_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor import action_bindings
    from frisket.engine.executor.temporal_split import ACTION_KIND

    contributed = action_bindings._contributed_action_job_executors()
    monkeypatch.setattr(
        action_bindings,
        "_contributed_action_job_executors",
        lambda **_kwargs: contributed,
    )
    definitions = dict(action_bindings.ACTION_REGISTRY._actions)
    definitions.pop(ACTION_KIND)
    monkeypatch.setattr(action_bindings.ACTION_REGISTRY, "_actions", definitions)
    with pytest.raises(
        RuntimeError,
        match="action-job action bindings lack canonical definitions",
    ):
        action_bindings.action_job_bindings()
