"""Every resolved entry point checks declared requirements before effects."""

from contextlib import closing

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.actions import resolve_map_preview
from frisket.engine.executor.action_families.runs import _guard_backfill_run_spec
from frisket.engine.store import Project
from frisket.execution.action_admission import action_admission_error
from frisket.server.sources.runtime import (
    get_source_poller,
    register_source_poller,
    unregister_source_poller,
)
from frisket.server.sources.tiktok import TikTokProfilePoller


def test_direct_source_poll_checks_network_before_creating_source_or_dispatching(
    tmp_path,
):
    calls = []
    previous = get_source_poller("tiktok_profile")
    register_source_poller(
        TikTokProfilePoller(lambda *a, **kw: calls.append(a) or {"entries": []}),
        replace=True,
    )
    try:
        with closing(Project.create(tmp_path / "source.frisket")) as project:
            project.set_network_policy(mode="off")
            result = run_action_spec(
                project,
                {
                    "action_id": "source.poll",
                    "scope": {"kind": "project"},
                    "params": {
                        "source": {
                            "kind": "tiktok_profile",
                            "name": "test",
                            "url": "https://www.tiktok.com/@example",
                            "config": {},
                        }
                    },
                    "idempotency_key": "source-off",
                },
                project_id="test",
            )
            assert result.status == "failed"
            assert result.errors[0].code == "network_disabled"
            assert not calls
            assert project.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        if previous is not None:
            register_source_poller(previous, replace=True)
        else:
            unregister_source_poller("tiktok_profile")


def test_network_off_still_allows_local_action(tmp_path):
    with closing(Project.create(tmp_path / "local.frisket")) as project:
        project.set_network_policy(mode="off")
        assert (
            action_admission_error(project, ACTION_REGISTRY.get("resolve.fill_missing"))
            is None
        )


def test_code_gate_uses_resolved_target_on_retry_and_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    with closing(Project.create(tmp_path / "policy.frisket")) as project:
        sheet = project.add_sheet("source")
        body = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "content"}], {"body": body})
        action = ACTION_REGISTRY.get("research.answer")
        # Use the registered example to retain authoring/validation parity.
        sample = action.catalog_entry()["examples"][0]
        sample["scope"] = {"kind": "sheet_rows", "sheet_id": sheet}
        sample["params"]["source"] = ["body"]
        error = resolve_map_preview(project, sample)
        assert error.code == "code_action_disabled"
        error = _guard_backfill_run_spec(
            {"action_kind": "research.answer"}, project=project
        )
        assert error.code == "code_action_disabled"


def test_missing_declared_credentials_refused_without_presentation_schema(
    tmp_path, monkeypatch
):
    with closing(Project.create(tmp_path / "keys.frisket")) as project:
        monkeypatch.setattr(
            "frisket.credentials.missing_required_credentials", lambda p, keys: keys
        )
        action = ACTION_REGISTRY.get("enrich.census_demographics")
        monkeypatch.setattr(
            type(action),
            "catalog_entry",
            lambda self: pytest.fail(
                "admission must not generate presentation schemas"
            ),
        )
        error = action_admission_error(project, action)
        assert error.code == "missing_action_credential"
        assert error.details["missing_credentials"] == ["CENSUS_API_KEY"]


def test_retry_rechecks_actual_inner_action_current_edition(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
    with closing(Project.create(tmp_path / "edition.frisket")) as project:
        error = _guard_backfill_run_spec(
            {"action_kind": "map.mcp_extract"}, project=project, edition="cloud"
        )
        assert error.code == "action_unavailable_in_edition"
        assert error.action_kind == "run.backfill"


def test_resolved_plugin_capabilities_do_not_depend_on_builtin_id(monkeypatch):
    from dataclasses import replace

    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    action = replace(
        ACTION_REGISTRY.get("research.answer"), action_id="example.research"
    )
    error = action_admission_error(None, action)
    assert error.code == "code_action_disabled"
    assert error.action_kind == "example.research"
