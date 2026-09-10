from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ProjectCapabilitySpec,
    _PROJECT_CAPABILITY_SPECS,
    _ProjectAction,
    _project_capability_registry,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor import actions as executor_actions
from frisket.engine.executor import mutation_action, operation_action
from frisket.engine.executor import source_action, source_poll_action
from frisket.engine.executor import sheet_refresh_action
from frisket.engine.executor import plugin_load_action
from frisket.engine.executor import run_backfill_action
from frisket.engine.executor import enclosure_action, temporal_extract_action
from frisket.engine.executor.action_families import embeddings, exports


class _Output(BaseModel):
    value: str


class _Capability:
    pass


def _spec() -> ProjectCapabilitySpec:
    return ProjectCapabilitySpec(
        _Capability,
        str,
        _Output,
        [["invalid", "Invalid."]],  # type: ignore[arg-type]
        ["write_receipt"],  # type: ignore[arg-type]
        ["project:write"],  # type: ignore[arg-type]
        True,
    )


def test_project_capability_spec_is_deeply_frozen() -> None:
    spec = _spec()

    assert spec.errors == (("invalid", "Invalid."),)
    assert spec.side_effects == ("write_receipt",)
    assert spec.required_capabilities == ("project:write",)
    with pytest.raises(FrozenInstanceError):
        spec.writes_project = False  # type: ignore[misc]


def test_project_capability_registry_rejects_duplicate_authority() -> None:
    spec = _spec()

    with pytest.raises(ValueError, match="capabilities must be unique"):
        _project_capability_registry(spec, spec)


@pytest.mark.parametrize(
    "updates",
    [
        {"cost_kind": "model_metered"},
        {"cost_kind": "external_metered"},
        {"requires_confirmation": True},
    ],
)
def test_callable_capabilities_refuse_unimplemented_cost_admission(updates) -> None:
    with pytest.raises(ValueError, match="invalid project capability metadata"):
        replace(_spec(), callable_host=True, **updates)


@pytest.mark.parametrize("field", ["errors", "side_effects", "required_capabilities"])
def test_project_capability_rejects_string_collections(field: str) -> None:
    with pytest.raises(ValueError, match="must contain sequences"):
        replace(_spec(), **{field: "read"})


def _project_actions() -> list[tuple[Any, _ProjectAction[Any, Any]]]:
    return [
        (registered, terminal)
        for registered in ACTION_REGISTRY.actions
        if isinstance((terminal := registered.definition.run), _ProjectAction)
    ]


def test_registered_capabilities_are_used_and_catalog_uses_the_spec() -> None:
    actions = _project_actions()
    # Installed Actions may consume host capabilities absent from builtins.
    assert {terminal.single_capability() for _, terminal in actions} <= set(
        _PROJECT_CAPABILITY_SPECS
    )
    for registered, terminal in actions:
        spec = _PROJECT_CAPABILITY_SPECS[terminal.single_capability()]
        entry = registered.catalog_entry()
        assert terminal.single_spec() is spec
        assert terminal.output_model is (
            terminal.return_type if terminal.callable_host else spec.output_model
        )
        assert entry["errors"] == [
            {"code": code, "message": message} for code, message in spec.errors
        ]
        assert entry["side_effects"] == list(spec.side_effects)
        assert entry["required_capabilities"] == list(spec.required_capabilities)
        assert entry["writes_project"] is spec.writes_project
        assert entry["ui_hints"]["form"] == registered.definition.form


def test_every_project_action_has_exactly_one_host_owner() -> None:
    from frisket.actions.cluster_types import ValueClusterer
    from frisket.actions.find_types import FindScanner
    from frisket.actions.group_summary_types import GroupSummarizer
    from frisket.actions.page_capture_types import PageCapturer

    # Mirrors the owner table ``executor_actions.run_action_spec`` dispatches
    # project actions through.
    predicates = (
        mutation_action.supports_typed_mutation_action,
        operation_action.supports_typed_operation_action,
        source_action.supports_typed_source_action,
        source_poll_action.supports_typed_source_poll_action,
        sheet_refresh_action.supports_typed_sheet_refresh_action,
        lambda terminal: terminal.callable_host,
        exports.supports_typed_export_action,
        embeddings.supports_typed_embedding_action,
        plugin_load_action.supports_typed_plugin_load_action,
        run_backfill_action.supports_typed_backfill_action,
        enclosure_action.supports_typed_enclosure_action,
        temporal_extract_action.supports_typed_temporal_extract_action,
        lambda terminal: terminal.capabilities == (ValueClusterer,),
        lambda terminal: terminal.capabilities == (PageCapturer,),
        lambda terminal: terminal.capabilities == (GroupSummarizer,),
        lambda terminal: terminal.capabilities == (FindScanner,),
    )
    for registered, terminal in _project_actions():
        assert sum(supports(terminal) for supports in predicates) == 1, (
            registered.action_id
        )


_SOURCE_CREATE = {
    "action_id": "source.create",
    "scope": {"kind": "project"},
    "params": {
        "name": "Feed",
        "kind": "rss",
        "url": "https://example.test/feed.xml",
        "schedule": "@hourly",
        "enabled": True,
    },
    "idempotency_key": "source-create-ownership",
}


@pytest.mark.parametrize(
    ("source_support", "mutation_support", "matches"),
    [(False, False, "none"), (True, True, "mutation, source")],
)
def test_project_dispatch_rejects_invalid_owner_count(
    monkeypatch: pytest.MonkeyPatch,
    source_support: bool,
    mutation_support: bool,
    matches: str,
) -> None:
    monkeypatch.setattr(
        source_action,
        "supports_typed_source_action",
        lambda _terminal: source_support,
    )
    monkeypatch.setattr(
        mutation_action,
        "supports_typed_mutation_action",
        lambda _terminal: mutation_support,
    )

    with pytest.raises(TypeError, match=rf"exactly one host owner; matched {matches}"):
        executor_actions.run_action_spec(None, _SOURCE_CREATE, project_id="p1")
