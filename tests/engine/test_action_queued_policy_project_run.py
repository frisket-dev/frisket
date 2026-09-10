from __future__ import annotations

from frisket.actions.registry import NEW_ACTION_IDS
from frisket.engine.executor.action_specs import (
    PlacementPolicy,
    execution_spec_for,
    queued_project_run_kinds,
)


def _classify_action() -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["text"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "fields": [{"name": "topic", "labels": ["housing", "transit", "other"]}],
        },
        "idempotency_key": "queued-policy@sha256:canonical-scope",
    }


# --- placement is the dispatch source of truth -----------------------------


def test_queued_map_and_media_actions_declare_queued_project_run() -> None:
    for kind in ("map.python", "map.classify", "media.transcribe"):
        spec = execution_spec_for(kind)
        assert spec is not None, kind
        assert spec.lifecycle.placement is PlacementPolicy.QUEUED_PROJECT_RUN, kind


def test_server_dispatch_reads_placement_not_just_the_registry() -> None:
    # The enqueue/dispatch layer must expose a placement-based decision derived
    # from the execution specs, without a parallel dispatch inventory.
    from frisket.engine.executor import action_dispatch

    for kind in queued_project_run_kinds():
        assert action_dispatch.placement_for_kind(kind) is (
            PlacementPolicy.QUEUED_PROJECT_RUN
        ), kind
    # an inline action resolves to inline placement.
    assert action_dispatch.placement_for_kind("cell.edit") is PlacementPolicy.INLINE


def test_queued_registry_is_an_invariant_over_declared_placement() -> None:

    # Every registry entry is a declared queued_project_run placement, and every
    # declared queued_project_run kind has a dispatch entry. Registry == policy.
    assert queued_project_run_kinds() <= NEW_ACTION_IDS


# --- queued v2 payloads decode (clean cut: a pre-v2 payload that carried
# --- params.sheet_id no longer decodes — no dual-read, per the owner's
# --- no-legacy-saved-state ruling) ------------------------------------------


def test_queued_v1_payload_with_canonical_scope_decodes() -> None:
    from frisket.engine.executor.queued_actions import (
        queued_v1_action_request,
        queued_v1_job_payload,
        queued_v1_payload_envelope,
    )

    action = _classify_action()
    request = queued_v1_action_request(action)
    assert request is not None
    reservation = {
        "action_id": "act_canonical",
        "receipt_id": "receipt_canonical",
        "params_hash": request.expected_params_hash,
        "input_column_ids": {"text": 5},
        "input_column_types": {"text": "text"},
        "output_names": request.expected_runner_spec["output_names"],
        "output_target_preconditions": {},
    }
    payload = {
        "project_id": "p1",
        "run_id": 9,
        "spec": request.entry.mark_runner_spec(
            request.expected_runner_spec,
            receipt_id=reservation["receipt_id"],
            action_id=reservation["action_id"],
            params_hash=reservation["params_hash"],
        ),
        **queued_v1_job_payload(request, action, reservation, run_id=9),
    }
    envelope = queued_v1_payload_envelope(payload)
    assert envelope is not None
    assert envelope.action.kind == "map.classify"
    assert envelope.receipt_id == "receipt_canonical"
    assert envelope.params_hash == request.expected_params_hash

    # Clean cut: a payload carrying a pre-v2 action that spells its scope as
    # ``params.sheet_id`` is not re-read; it fails to decode instead.
    legacy = {
        **payload,
        "v1_action": {
            **action,
            "params": {**action["params"], "sheet_id": 1},
        },
    }
    assert queued_v1_payload_envelope(legacy) is None
