"""Closure fences for the queued v1 seam.

- G1: placement decides dispatch FIRST. A kind whose declared placement is
  QUEUED_PROJECT_RUN can never silently degrade to inline execution because
  the queued request could not be built (the original bug's structural survivor:
  ``except ValueError: return None`` + request-before-placement). Queued +
  unbuildable request == terminal failed ActionResult.
- G1 round-trip: every registry kind's catalog example builds a queued
  request through the canonical-params re-validation path (a divergence
  between contract validation and the registry's params model fails here).
- G2: queued payload codecs treat ABSENCE as a defect, not an empty value —
  a declared key missing from the reservation fails the enqueue, and a
  declared ``v1_*`` key missing from the worker payload raises at decode
  instead of finalizing against empty inputs with a completed status.
"""

from __future__ import annotations

import logging
from pathlib import Path

from frisket.execution.provider import ExecutionCompositionContext
import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor.action_specs import queued_project_run_kinds
from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
    queued_v1_finalize_kwargs,
    queued_v1_payload_metadata,
)
from frisket.engine.jobs.queue import QUEUE_DB_NAME, SqliteJobQueue
from frisket.engine.store import Project


def _classify_action(sheet_id: int = 1, *, source: str = "story") -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": [source],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "fields": [{"name": "topic", "labels": ["housing", "transit", "other"]}],
        },
        "idempotency_key": "g1-fence@sha256:v1",
    }


def test_every_registry_kind_example_builds_a_queued_request() -> None:
    # The static queued table is retired: every queued_project_run kind is
    # reconstructed from its canonical ActionRequest, so the round-trip walks
    # the declared placement and each kind's catalog examples.
    kinds = sorted(queued_project_run_kinds())
    assert len(kinds) >= 20
    for kind in kinds:
        examples = ACTION_REGISTRY.get(kind).catalog_entry()["examples"]
        assert examples, f"{kind} ships no catalog example to round-trip"
        for index, example in enumerate(examples):
            request = queued_v1_action_request(example)
            assert request is not None, (
                f"{kind} example {index} fails the queued-path canonical "
                "re-validation: with placement-first it would fail loudly, "
                "but the divergence itself is a seam defect"
            )
            assert request.entry.kind == kind
            assert (
                request.entry.params_model
                is ACTION_REGISTRY.get(kind).definition.run.params_model
            )


def test_queued_placement_with_unbuildable_request_fails_not_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.server import action_enqueue

    project = Project.create(tmp_path / "fence.frisket", name="G1 fence")
    try:
        sheet_id = project.add_sheet("notes")
        columns = {"note": project.add_column(sheet_id, "note", type="text")}
        project.add_rows(sheet_id, [{"note": "call 212-555-0123"}], columns)

        action_body = _classify_action(sheet_id, source="note")
        # Simulate original-class failure: the queued request cannot be built
        # (canonical re-validation ValueError / registry divergence).
        monkeypatch.setattr(
            action_enqueue,
            "queued_v1_action_request",
            lambda body, **_kwargs: None,
        )
        queue = SqliteJobQueue(tmp_path / QUEUE_DB_NAME)
        try:
            ctx = action_enqueue.QueuedV1ActionRunContext(
                queue=queue,
                workspace_root=tmp_path,
                router_for=lambda _project: None,  # type: ignore[arg-type]
                execution_composition_for=lambda _project, _router, _context: None,  # type: ignore[arg-type]
                active_runs={},
                run_jobs={},
                queue_payload_extra={},
                logger=logging.getLogger("test.g1"),
            )
            result = action_enqueue.queue_v1_action_run(
                project,
                "project-g1-fence",
                action_body,
                ctx=ctx,
                execution_context=ExecutionCompositionContext.direct(),
            )
        finally:
            queue.close()
        # NEVER None: None means "run it inline", silently discarding the
        # declared queued placement.
        assert result is not None
        assert result.status == "failed"
        assert result.errors
        assert result.errors[0].code == "invalid_params"
        assert "queued" in result.errors[0].message
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# G2 — codec strictness


def _queued_entry():
    request = queued_v1_action_request(_classify_action())
    assert request is not None
    return request.entry


def test_enqueue_metadata_requires_every_declared_codec_key() -> None:
    entry = _queued_entry()
    assert entry.payload_keys  # the fence is vacuous otherwise
    complete = {key: {} for key in entry.payload_keys}
    encoded = queued_v1_payload_metadata(entry, complete)
    assert set(encoded) == {f"v1_{key}" for key in entry.payload_keys}
    for key in entry.payload_keys:
        broken = dict(complete)
        del broken[key]
        with pytest.raises(ValueError, match=key):
            queued_v1_payload_metadata(entry, broken)


def test_finalize_decode_raises_on_missing_declared_key() -> None:
    entry = _queued_entry()
    payload = {f"v1_{key}": {} for key in entry.payload_keys}
    decoded = queued_v1_finalize_kwargs(entry, payload)
    assert set(decoded) == set(entry.payload_keys)
    for key in entry.payload_keys:
        broken = dict(payload)
        del broken[f"v1_{key}"]
        with pytest.raises(ValueError, match=key):
            queued_v1_finalize_kwargs(entry, broken)


def test_finalize_decode_raises_for_every_registry_kind_on_absence() -> None:
    # Corrupt-one-key, generalized: no kind's decoder may map a missing
    # declared key to an empty value.
    entries = {
        kind: queued_v1_action_request(
            ACTION_REGISTRY.get(kind).catalog_entry()["examples"][0]
        ).entry
        for kind in queued_project_run_kinds()
    }
    assert entries
    for kind, entry in sorted(entries.items()):
        if not entry.payload_codecs:
            continue
        first = entry.payload_codecs[0].key
        payload = {f"v1_{codec.key}": {} for codec in entry.payload_codecs}
        del payload[f"v1_{first}"]
        with pytest.raises(ValueError):
            queued_v1_finalize_kwargs(entry, payload)
