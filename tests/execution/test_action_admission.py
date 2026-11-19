"""Every resolved entry point checks declared requirements before effects."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

from contextlib import closing

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.actions import resolve_map_preview
from frisket.engine.executor.action_families.runs import _guard_backfill_run_spec
from frisket.engine.store import Project
from frisket.execution.action_admission import action_admission_error
from frisket.feeds.runtime import (
    get_feed_checker,
    register_feed_checker,
    unregister_feed_checker,
)
from frisket.feeds.tiktok import TikTokProfileFeedChecker


def test_direct_source_poll_checks_network_before_creating_source_or_dispatching(
    tmp_path,
):
    calls = []
    previous = get_feed_checker("tiktok_profile")
    register_feed_checker(
        TikTokProfileFeedChecker(lambda *a, **kw: calls.append(a) or {"entries": []}),
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
            register_feed_checker(previous, replace=True)
        else:
            unregister_feed_checker("tiktok_profile")


def test_network_off_still_allows_local_action(make_project):
    project = make_project("local")
    project.set_network_policy(mode="off")
    assert (
        action_admission_error(project, ACTION_REGISTRY.get("resolve.fill_missing"))
        is None
    )


def test_code_gate_uses_resolved_target_on_retry_and_preview(make_project, monkeypatch):
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    project = make_project("policy")
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
    make_project, monkeypatch
):
    project = make_project("keys")
    monkeypatch.setattr(
        "frisket.credentials.missing_required_credentials", lambda p, keys: keys
    )
    action = ACTION_REGISTRY.get("enrich.census_demographics")
    monkeypatch.setattr(
        type(action),
        "catalog_entry",
        lambda self: pytest.fail("admission must not generate presentation schemas"),
    )
    error = action_admission_error(project, action)
    assert error.code == "missing_action_credential"
    assert error.details["missing_credentials"] == ["CENSUS_API_KEY"]


def test_retry_rechecks_actual_inner_action_current_edition(make_project, monkeypatch):
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
    project = make_project("edition")
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


def _declare_external(monkeypatch, action_id):
    """Exercise the declared capability seam without depending on engine choice."""
    from frisket.actions.core import RegisteredAction

    original = RegisteredAction.required_capabilities
    monkeypatch.setattr(
        RegisteredAction,
        "required_capabilities",
        property(
            lambda self: (
                ("external:test",)
                if self.action_id == action_id
                else original.fget(self)
            )
        ),
    )


@pytest.mark.parametrize("family", ["ocr", "transcribe"])
def test_scratch_preview_declared_gate_precedes_media_probe(
    make_project, monkeypatch, family
):
    from frisket.server.services.scratch_ocr import paid_ocr_scratch_plan
    from frisket.server.services.scratch_transcribe import paid_transcribe_scratch_plan
    from frisket.preview.common import ComparePreviewError

    action_id = "media.ocr" if family == "ocr" else "media.transcribe"
    _declare_external(monkeypatch, action_id)
    project = make_project("scratch")
    project.set_network_policy(mode="off")
    prepare = paid_ocr_scratch_plan if family == "ocr" else paid_transcribe_scratch_plan
    with pytest.raises(ComparePreviewError) as caught:
        prepare(
            project,
            media_bytes=b"not-media",
            payload={"engine": "local"},
            composition=None,
        )
    assert caught.value.code == "network_disabled"


def test_refresh_checks_resolved_derive_action_before_read_or_publish(
    tmp_path, monkeypatch
):
    from tests.engine.test_sheet_refresh import _build, _refresh

    project, _, _, child_id, _ = _build(tmp_path)
    try:
        _declare_external(monkeypatch, "derive.table_from_list")
        project.set_network_policy(mode="off")
        before = tuple(project.db.iterdump())
        result = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="blocked-refresh"),
            project_id="p-ref",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "network_disabled"
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


def test_queued_cluster_rechecks_declared_requirements_after_admission(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from frisket.ai.llm import ModelRouter
    from frisket.server.app import create_app
    from frisket.engine.executor.queued_actions import queued_v1_payload_envelope

    with TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY),
        )
    ) as client:
        pid = client.post("/api/projects", json={"name": "Gate"}).json()["id"]
        imported = client.post(
            f"/api/projects/{pid}/import/csv",
            files={"file": ("names.csv", "name\nJon Smith\nSmith Jon\n", "text/csv")},
        )
        response = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "cluster.values",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": imported.json()["sheet_id"],
                },
                "params": {"source": "name"},
                "idempotency_key": "cluster-gate",
            },
        )
        body = response.json()
        assert body["status"] == "queued", body
        job = client.app.state.workspace.queue.get(body["job_id"])
        project = client.app.state.workspace.get(pid)
        envelope = queued_v1_payload_envelope(job.payload)
        _declare_external(monkeypatch, "cluster.values")
        project.set_network_policy(mode="off")
        assert envelope.pre_run_error(project).code == "network_disabled"
