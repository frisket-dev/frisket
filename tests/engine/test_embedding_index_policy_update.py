"""``embedding.index_update_policy`` edits maintenance/provider policy.

Metadata-only, idempotent. Crucially it does NOT bypass the automatic-remote /
cost gates: those fire at refresh time, so a policy update only stores fields; the
next automatic refresh still gates.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from frisket.ai.embeddings import EmbeddingStore, build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.jobs import (
    EMBEDDING_REFRESH_KIND,
    HandlerRegistry,
    SqliteJobQueue,
    Worker,
    enqueue_due_scheduled_refreshes,
    find_scheduled_indexes,
    register_embedding_refresh_handler,
)
from frisket.engine.store import Project
from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.embeddings import IndexUpdatePolicyParams
from frisket.actions.system import BoundTypedActionRequest, typed_action_for_request
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    EmbeddingIndexPolicy,
    EmbeddingIndexPolicyUpdater,
)
from frisket.engine.executor.action_families.embeddings import (
    run_typed_embedding_action,
)
from frisket.engine.executor.map_rows_action import typed_request_hash


class _Gateway:
    def embed(self, texts, *, provider, model, modality):
        return build_batch_result(
            [[1.0] + [0.0] * 1535 for _ in texts],
            provider_id=provider or "openai",
            provider_kind="platform_api",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _project(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet, [{"headline": t} for t in ("cat", "kitten")], {"headline": col}
    )
    return project, sheet


def _create(
    project, sheet, *, provider="openai", policy=None, maintenance=None, key="c@1"
):
    r = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "maintenance_policy": maintenance or {"mode": "manual"},
                "provider_policy": policy or {"allow_remote": False},
            },
            "idempotency_key": key,
        },
        project_id="p",
    )
    assert r.status == "completed", r.errors
    return r.outputs[0].ref["index_id"]


def _update(project, index_id, *, maintenance=None, provider=None, key="u@1"):
    params = {"index_id": index_id}
    if maintenance is not None:
        params["maintenance_policy"] = maintenance
    if provider is not None:
        params["provider_policy"] = provider
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_update_policy",
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": key,
        },
        project_id="p",
    )


def _policies(project, index_id):
    index = EmbeddingStore(project).get_index(index_id)
    return (
        json.loads(index["maintenance_policy_json"]),
        json.loads(index["provider_policy_json"]),
    )


def _typed_request(target, **params):
    return {
        "action_id": "embedding.index_update_policy",
        "scope": {"kind": "project"},
        "params": {"index_id": target, **params},
        "idempotency_key": "typed-policy",
    }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"maintenance_policy": None},
        {"maintenance_policy": []},
        {"provider_policy": {}, "unknown": True},
        {"index_id": " ", "provider_policy": {}},
    ],
)
def test_typed_policy_shape_rejects_before_project_access(params):
    result = run_action_spec(None, _typed_request("missing", **params), project_id="p")
    assert result.errors[0].code == "invalid_action_request"


def test_policy_defaults_normalize_but_empty_object_is_a_replacement(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet, maintenance={"mode": "on_source_append"})
    request = _typed_request(index_id, provider_policy={})
    explicit = deepcopy(request)
    explicit["params"]["maintenance_policy"] = None
    assert typed_request_hash(typed_action_for_request(request)) == typed_request_hash(
        typed_action_for_request(explicit)
    )
    first = run_action_spec(project, request, project_id="p")
    assert first.status == "completed"
    assert _policies(project, index_id) == ({"mode": "on_source_append"}, {})
    replay = run_action_spec(project, explicit, project_id="p")
    assert replay.receipt_id == first.receipt_id
    explicit["params"]["maintenance_policy"] = {}
    conflict = run_action_spec(project, explicit, project_id="p")
    assert conflict.errors[0].code == "idempotency_conflict"
    explicit["idempotency_key"] = "clear-maintenance"
    assert run_action_spec(project, explicit, project_id="p").status == "completed"
    assert _policies(project, index_id) == ({}, {})
    project.close()


def test_policy_semantics_are_deferred_until_after_replay_and_index_lookup(tmp_path):
    project, sheet = _project(tmp_path)
    request = _typed_request("missing", maintenance_policy={"mode": []})
    assert IndexUpdatePolicyParams.model_validate(request["params"])
    missing = run_action_spec(project, request, project_id="p")
    assert missing.errors[0].code == "embedding_index_not_found"
    index_id = _create(project, sheet)
    first = _update(project, index_id, provider={})
    assert first.status == "completed"
    conflict = _update(project, index_id, maintenance={"mode": []})
    assert conflict.errors[0].code == "idempotency_conflict"
    invalid = _update(project, index_id, maintenance={"mode": []}, key="invalid")
    assert invalid.errors[0].code == "invalid_params"
    project.close()


def test_policy_catalog_and_host_have_one_inline_zero_cost_owner():
    from frisket.actions.system import root_action_catalog
    from frisket.engine.executor.action_dispatch import is_queued

    kind = "embedding.index_update_policy"
    [entry] = [entry for entry in root_action_catalog().actions if entry.kind == kind]
    assert entry.execution_mode == "whole_project"
    assert entry.async_mode == "sync"
    assert entry.cost_policy.kind == "none"
    assert entry.required_capabilities == ["project:write"]
    assert entry.side_effects == ["update_embedding_index_policy", "write_receipt"]
    assert not is_queued(kind)
    from frisket.engine.executor import resolve_map_preview

    preview = resolve_map_preview(None, _typed_request("idx", provider_policy={}))
    assert preview.code == "unsupported_action_kind"
    legacy = run_action_spec(
        None,
        {
            "schema_version": "frisket.action.v2",
            "kind": kind,
            "capabilities": ["project:write"],
            "params": {"index_id": "idx", "provider_policy": {}},
            "idempotency_key": "legacy",
        },
        project_id="p",
    )
    assert legacy.status == "failed"


def test_policy_update_has_exact_receipt_facts_without_provider_or_vector_work(
    tmp_path, monkeypatch
):
    from frisket.engine.executor.action_families import embeddings

    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)

    def forbidden(*args, **kwargs):
        pytest.fail("policy update attempted provider or vector work")

    monkeypatch.setattr(embeddings, "EmbeddingGateway", forbidden)
    monkeypatch.setattr(embeddings, "VectorBackend", forbidden)
    result = _update(project, index_id, maintenance={}, provider={"allow_remote": True})
    assert result.status == "completed", result.errors
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()[0]
    )
    index = EmbeddingStore(project).get_index(index_id)
    assert result.run_id is None
    assert receipt["provider_use"] == []
    assert receipt["inputs"][0]["ref"] == {
        "kind": "embedding_index_ref",
        "index_id": index_id,
        "space_id": index["space_id"],
    }
    assert (
        result.outputs[0].ref
        == receipt["outputs"][0]["ref"]
        == {
            "kind": "embedding_index_update_policy",
            "index_id": index_id,
            "space_id": index["space_id"],
            "maintenance_policy": {},
            "provider_policy": {"allow_remote": True},
        }
    )
    assert receipt["evidence"][0]["ref"] == {
        "kind": "embedding_index_policy",
        "maintenance_policy": {},
        "provider_policy": {"allow_remote": True},
    }
    assert receipt["evidence"][0]["retention"] == "pinned"
    project.close()


@pytest.mark.parametrize("tamper", ["input", "output", "evidence", "space"])
def test_policy_replay_rejects_tampered_evidence_and_current_space(tmp_path, tamper):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    request = _typed_request(index_id, provider_policy={})
    first = run_action_spec(project, request, project_id="p")
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
    ).fetchone()
    receipt = json.loads(row[0])
    if tamper == "input":
        receipt["inputs"][0]["ref"]["index_id"] = "another-index"
    elif tamper == "output":
        receipt["outputs"][0]["ref"]["provider_policy"] = {"allow_remote": True}
    elif tamper == "evidence":
        receipt["evidence"] = []
    else:
        # A second admitted space makes the live index point somewhere else.
        other = _create(project, sheet, provider="fastembed", key="other-space")
        other_space = EmbeddingStore(project).get_index(other)["space_id"]
        project.db.execute(
            "UPDATE embedding_indexes SET space_id=? WHERE id=?",
            (other_space, index_id),
        )
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(receipt), first.receipt_id),
    )
    project.db.commit()
    replay = run_action_spec(project, request, project_id="p")
    assert replay.errors[0].code == "stale_replay"
    project.close()


class _RepairParams(ActionParams):
    target: str
    permit_remote: bool
    cadence: str = "mode:manual"


def _repair_policy(
    params: _RepairParams, indexes: EmbeddingIndexPolicyUpdater
) -> EmbeddingIndexPolicy:
    result = indexes.update_policy(
        params.target.removeprefix("repair:"),
        maintenance_policy={"mode": params.cadence.removeprefix("mode:")},
        provider_policy={"allow_remote": params.permit_remote},
    )
    result.provider_policy["allow_remote"] = False
    return result


def test_reused_capability_uses_derived_arguments_and_host_results(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    custom = action(
        name="repair",
        title="Repair",
        description="Repair policy.",
        category=ActionCategory.SOURCES,
        run=_repair_policy,
    )
    registered = ActionRegistry((ActionNamespace("custom", actions=(custom,)),)).get(
        "custom.repair"
    )
    request = ActionRequest(
        action_id="custom.repair",
        scope={"kind": "project"},
        params={"target": f"repair:{index_id}", "permit_remote": True},
        idempotency_key="custom-policy",
    )
    bound = BoundTypedActionRequest.bind(registered, request)
    result = run_typed_embedding_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["index_id"] == index_id
    assert result.outputs[0].ref["provider_policy"] == {"allow_remote": True}
    assert _policies(project, index_id)[1] == {"allow_remote": True}
    assert (
        run_typed_embedding_action(project, "p", bound).receipt_id == result.receipt_id
    )
    project.close()


@pytest.mark.parametrize(
    ("missing", "cadence", "code"),
    [
        (True, "mode:manual", "embedding_index_not_found"),
        (False, "mode:invalid", "invalid_params"),
    ],
)
def test_reused_capability_refusals_do_not_invent_params_fields(
    tmp_path, missing, cadence, code
):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    custom = action(
        name="repair",
        title="Repair",
        description="Repair policy.",
        category=ActionCategory.SOURCES,
        run=_repair_policy,
    )
    registered = ActionRegistry((ActionNamespace("custom", actions=(custom,)),)).get(
        "custom.repair"
    )
    request = ActionRequest(
        action_id="custom.repair",
        scope={"kind": "project"},
        params={
            "target": "repair:missing" if missing else f"repair:{index_id}",
            "permit_remote": True,
            "cadence": cadence,
        },
        idempotency_key="custom-refused",
    )
    before = _policies(project, index_id)
    bound = BoundTypedActionRequest.bind(registered, request)
    result = run_typed_embedding_action(project, "p", bound)
    assert result.status == "failed"
    assert result.errors[0].code == code
    assert result.errors[0].field == "params"
    assert result.errors[0].action_kind == "custom.repair"
    assert result.receipt_id is None
    assert _policies(project, index_id) == before
    project.close()


def _refresh(project, index_id, *, trigger=None, key="r@1", gateway=None):
    params = {"index_id": index_id, "mode": "full"}
    if trigger is not None:
        params["trigger_ref"] = trigger
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": key,
        },
        project_id="p",
        deps=None if gateway is None else ExecutorDeps(embedding_gateway=gateway),
    )


# --------------------------------------------------------------------------


def test_update_replaces_policies_and_is_idempotent(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    result = _update(
        project,
        index_id,
        maintenance={"mode": "scheduled", "schedule": "@hourly"},
        provider={
            "allow_remote": True,
            "allow_remote_automatic_refresh": True,
            "max_cost_usd_per_refresh": 0.5,
        },
    )
    assert result.status == "completed", result.errors
    maintenance, provider = _policies(project, index_id)
    assert maintenance == {"mode": "scheduled", "schedule": "@hourly"}
    assert (
        provider["allow_remote"] is True and provider["max_cost_usd_per_refresh"] == 0.5
    )
    # idempotent replay: same key -> same receipt, no change
    again = _update(
        project,
        index_id,
        maintenance={"mode": "scheduled", "schedule": "@hourly"},
        provider={
            "allow_remote": True,
            "allow_remote_automatic_refresh": True,
            "max_cost_usd_per_refresh": 0.5,
        },
    )
    assert again.receipt_id == result.receipt_id
    project.close()


def test_update_leaves_unprovided_policy_unchanged(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet, maintenance={"mode": "on_source_append"})
    _update(project, index_id, provider={"allow_remote": True})
    maintenance, provider = _policies(project, index_id)
    assert maintenance == {"mode": "on_source_append"}  # untouched
    assert provider == {"allow_remote": True}
    project.close()


def test_update_rejects_unknown_mode_and_scheduled_without_schedule(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    bad_mode = _update(project, index_id, maintenance={"mode": "live_on_write"})
    assert bad_mode.status == "failed"
    assert bad_mode.errors[0].code == "invalid_params"
    no_sched = _update(project, index_id, maintenance={"mode": "scheduled"}, key="u@2")
    assert no_sched.status == "failed"
    assert no_sched.errors[0].code == "invalid_params"
    project.close()


@pytest.mark.parametrize("cap", [0, -1])
def test_update_rejects_non_positive_cost_cap(tmp_path, cap):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet)
    result = _update(
        project,
        index_id,
        provider={"max_cost_usd_per_refresh": cap},
        key=f"non-positive-{cap}",
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_params"
    project.close()


def test_update_missing_index_fails(tmp_path):
    project, sheet = _project(tmp_path)
    result = _update(project, "embidx_missing", provider={"allow_remote": True})
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_index_not_found"
    project.close()


def test_scheduled_state_visible_after_update(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet, provider="fastembed")
    assert find_scheduled_indexes(project) == []
    _update(project, index_id, maintenance={"mode": "scheduled", "schedule": "@hourly"})
    scheduled = [i["id"] for i in find_scheduled_indexes(project)]
    assert scheduled == [index_id]
    project.close()


def test_update_does_not_bypass_remote_and_cost_gates(tmp_path):
    # Pre-authorizing remote + automatic refresh via update must NOT let an
    # automatic refresh skip the gates: without a cost ceiling it still blocks.
    project, sheet = _project(tmp_path)
    queue = SqliteJobQueue(tmp_path / ".q.db")
    index_id = _create(project, sheet)
    # preauthorize automatic remote refresh but DO NOT set a cost ceiling; make it
    # scheduled (never refreshed -> due). A scheduled refresh is automatic.
    _update(
        project,
        index_id,
        maintenance={"mode": "scheduled", "schedule": "@hourly"},
        provider={"allow_remote": True, "allow_remote_automatic_refresh": True},
    )
    project.close()

    enqueued = enqueue_due_scheduled_refreshes(workspace_root=tmp_path, queue=queue)
    assert len(enqueued) == 1
    reg = HandlerRegistry()
    captured: list[int] = []

    class _Spy:
        def embed(self, texts, *, provider, model, modality):
            captured.append(1)
            return build_batch_result(
                [[1.0] + [0.0] * 1535 for _ in texts],
                provider_id="openai",
                provider_kind="platform_api",
                actual_model_id="m",
                modality=modality,
            )

    register_embedding_refresh_handler(reg, workspace_root=tmp_path, gateway=_Spy())
    worker = Worker(queue, reg)
    assert worker.run_once() is True
    # the cost gate fired before any provider call
    assert captured == []
    # 9dc75097 ("queued execution as placement policy") migrated embedding
    # refresh jobs onto the generic action.run queue kind carrying an
    # ActionJobEnvelope; EMBEDDING_REFRESH_KIND == ACTION_RUN_KIND now
    # (frisket.jobs.embeddings), matching the sibling native-embedding tests
    # (test_native_embedding_scheduled_refresh.py et al.).
    [job] = queue.list_project_jobs("p", kind=EMBEDDING_REFRESH_KIND)
    assert job.result["error_code"] == "embedding_cost_requires_confirmation"
    queue.close()


def test_corrupt_remote_consent_and_cost_never_egress(tmp_path):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet, policy={"allow_remote": True})
    calls: list[int] = []

    class _Spy:
        def embed(self, texts, *, provider, model, modality):
            calls.append(1)
            return _Gateway().embed(
                texts, provider=provider, model=model, modality=modality
            )

    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        ('{"allow_remote":"false"}', index_id),
    )
    project.db.commit()
    manual = _refresh(project, index_id, key="r@manual", gateway=_Spy())
    assert manual.status == "failed"
    assert manual.errors[0].code == "embedding_remote_confirmation_required"
    assert calls == []

    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        (
            '{"allow_remote":true,"allow_remote_automatic_refresh":true,'
            '"max_cost_usd_per_refresh":"1"}',
            index_id,
        ),
    )
    project.db.commit()
    automatic = _refresh(
        project,
        index_id,
        trigger={"trigger_kind": "schedule"},
        key="r@automatic",
        gateway=_Spy(),
    )
    assert automatic.status == "failed"
    assert automatic.errors[0].code == "embedding_cost_requires_confirmation"
    assert calls == []

    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        (
            '{"allow_remote":true,"allow_remote_automatic_refresh":"true",'
            '"max_cost_usd_per_refresh":1}',
            index_id,
        ),
    )
    project.db.commit()
    wrong_auto = _refresh(
        project,
        index_id,
        trigger={"trigger_kind": "schedule"},
        key="r@wrong-auto",
        gateway=_Spy(),
    )
    assert wrong_auto.status == "failed"
    assert wrong_auto.errors[0].code == "embedding_remote_confirmation_required"
    assert calls == []
    project.close()


@pytest.mark.parametrize("cap", [0, -1])
def test_non_positive_legacy_automatic_cost_cap_never_egresses(tmp_path, cap):
    project, sheet = _project(tmp_path)
    index_id = _create(project, sheet, policy={"allow_remote": True})
    calls: list[int] = []

    class _Spy:
        def embed(self, texts, *, provider, model, modality):
            calls.append(1)
            return _Gateway().embed(
                texts, provider=provider, model=model, modality=modality
            )

    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "allow_remote": True,
                    "allow_remote_automatic_refresh": True,
                    "max_cost_usd_per_refresh": cap,
                }
            ),
            index_id,
        ),
    )
    project.db.commit()
    automatic = _refresh(
        project,
        index_id,
        trigger={"trigger_kind": "schedule"},
        key=f"non-positive-automatic-{cap}",
        gateway=_Spy(),
    )
    assert automatic.status == "failed"
    assert automatic.errors[0].code == "embedding_cost_requires_confirmation"
    assert calls == []
    project.close()
