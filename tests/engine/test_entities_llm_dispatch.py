from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import frisket.ops.ner_evidence as ner_module
from typed_model_fixtures import model_plan, render_ner
from frisket.actions.ner import NerModelOutput, normalize_ner_output
from executor_harness import run_action_with_confirmation
from frisket.actions.system import validate_root_action as validate_action_spec
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ops.ner_evidence import _NER_EVIDENCE_KEY
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


PROJECT_ID = "project-ner-llm-dispatch"


# ---------- unit: is_llm/render/postprocess_value ----------


@pytest.mark.parametrize("engine", ["spacy", "gliner", "llm"])
def test_model_dispatch_is_selected_from_typed_engine(engine):
    spec = {
        "action_kind": "map.ner",
        "input_columns": ["body"],
        "labels": ["person"],
        "engine": engine,
    }
    if engine == "llm":
        spec["model"] = "anthropic/claude-haiku-4-5"
    plan = model_plan(spec)
    assert plan.program.is_llm(plan.spec_dict()) is (engine == "llm")


def test_model_schema_excludes_derived_fingerprint():
    assert (
        "fingerprint"
        not in NerModelOutput.model_json_schema()["$defs"]["EntityModelValue"][
            "properties"
        ]
    )
    plan = model_plan(
        {"action_kind": "map.ner", "input_columns": ["body"], "labels": ["person"]}
    )
    assert "fingerprint" in str(plan.output_fields[0]["schema"])


def test_render_instruction_includes_labels_and_extra_instructions():
    call = render_ner(
        {"body": "text"},
        {"labels": ["person"], "extra_instructions": "Only named people."},
    )
    assert "Only named people." in json.dumps(call.messages)
    assert "person" in json.dumps(call.messages)


def test_render_keeps_labels_for_multi_column_inputs():
    call = render_ner({"speaker": "Ada", "body": "wrote notes"}, {"labels": ["person"]})
    assert call.messages[1]["content"] == [
        {"type": "text", "text": "speaker: Ada\nbody: wrote notes"}
    ]


def test_render_refuses_empty_labels():
    with pytest.raises(ValueError):
        render_ner({"body": "text"}, {"labels": []})


def test_completion_derives_fingerprint_from_entity_values():
    assert normalize_ner_output(
        [{"text": "Ada", "label": "person", "start": 0, "end": 3, "score": 0.9}]
    ).entities == [
        {
            "text": "Ada",
            "type": "person",
            "start": 0,
            "end": 3,
            "score": 0.9,
            "fingerprint": "ada",
        }
    ]


def _seed_project(
    project_path: Path,
    *,
    body: str = "Ada Lovelace wrote notes for Charles Babbage.",
) -> int:
    project = Project.create(project_path, name="NER LLM Dispatch")
    try:
        sheet_id = project.add_sheet("Docs")
        columns = {"body": project.add_column(sheet_id, "body", type="text")}
        project.add_rows(
            sheet_id,
            [{"body": body}],
            columns,
        )
        return sheet_id
    finally:
        project.close()


def test_estimate_prices_llm_engine_as_a_real_model_call(tmp_path: Path) -> None:
    project_path = tmp_path / "ner-llm-estimate.frisket"
    sheet_id = _seed_project(project_path)
    project = Project(project_path)
    try:
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        llm_spec = {
            "action_kind": "map.ner",
            "sheet_id": sheet_id,
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "output_name": "entities",
        }
        gliner_spec = {**llm_spec, "engine": "gliner", "model": None}
        llm = model_plan(llm_spec, project)
        gliner = model_plan(gliner_spec, project)
        runner.allow_action_lifecycle_only_recipes = True
        llm_est = runner.estimate(llm.spec_dict(), program=llm.program)
        gliner_est = runner.estimate(gliner.spec_dict(), program=gliner.program)
        assert "avg_input_tokens" in llm_est
        assert gliner_est["cost"] == 0.0
    finally:
        project.close()


def test_llm_engine_declares_model_cost_while_local_engine_is_free():
    llm = model_plan(
        {
            "action_kind": "map.ner",
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
        }
    )
    local = model_plan(
        {
            "action_kind": "map.ner",
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "spacy",
        }
    )
    assert llm.program.cost_class == "metered"
    assert local.program.external_cost_is_known_zero is True


# ---------- end-to-end: kind="map.ner", engine="llm", through run_action_spec ----------


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=42,
            tokens_out=17,
            cost=0.004,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _map_ner_llm_action(
    sheet_id: int,
    *,
    idempotency_key: str,
    capabilities: list[str] | None = None,
    labels: list[str] | None = None,
    model: str | None = "anthropic/claude-haiku-4-5",
) -> dict[str, Any]:
    return {
        "action_id": "map.ner",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["body"],
            "labels": labels if labels is not None else ["person"],
            "engine": "llm",
            "model": model,
            "extra_instructions": "Only named people.",
        },
        "output_names": {"entities": "entities"},
        "idempotency_key": idempotency_key,
        **({"capabilities": capabilities} if capabilities is not None else {}),
    }


def test_map_ner_llm_engine_validates_through_the_contract_layer():
    action = _map_ner_llm_action(1, idempotency_key="map_ner_llm_dispatch@sha256:a")
    result = validate_action_spec(action)
    assert result.ok is True, result.error


def test_map_ner_llm_engine_requires_model_complete_capability():
    action = _map_ner_llm_action(
        1,
        idempotency_key="map_ner_llm_dispatch@sha256:b",
        capabilities=["project:write"],
    )
    result = validate_action_spec(action)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def test_map_ner_llm_engine_requires_a_model():
    action = _map_ner_llm_action(
        1, idempotency_key="map_ner_llm_dispatch@sha256:c", model=None
    )
    result = validate_action_spec(action)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def test_map_ner_llm_engine_runs_and_writes_unified_entities(
    tmp_path: Path,
) -> None:
    """The core dispatch proof: kind="map.ner", engine="llm" through the
    REAL executor (`run_action_spec`, no manual re-packaging into a
    map.extract action) completes and writes the SAME unified
    {text,type,start,end,score} shape spacy/gliner write."""
    project_path = tmp_path / "ner-llm-dispatch.frisket"
    sheet_id = _seed_project(project_path)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:run"
    )
    router, adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada Lovelace",
                    "label": "person",  # raw "label" key -- canonicalize_entities renames it
                    "start": 0,
                    "end": 12,
                    "score": 0.92,
                }
            ]
        }
    )

    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        # stays a genuine map.ner run, not a re-labeled map.extract one.
        assert result.action.kind == "map.ner"
        assert len(adapter.requests) == 1
        assert "Only named people." in json.dumps(
            [m for m in adapter.requests[0].messages]
        )

        column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
        assert column["type"] == "json"
        values = project.get_values(sheet_id, column["id"])
        (row_id, entities) = next(iter(values.items()))
        # unified shape: "label" -> "type", canonicalized -- same contract
        # spacy/gliner write (tests/test_entities_output_spans.py).
        assert entities == [
            {
                "text": "Ada Lovelace",
                "type": "person",
                "start": 0,
                "end": 12,
                "score": 0.92,
                "fingerprint": "ada lovelace",
            }
        ]

        # output refs: column + named_result, matching the gliner/spacy shape
        # (tests/test_map_ner_executor.py) -- llm dispatch does not change
        # the receipt's public contract.
        assert [(output.kind, output.name) for output in result.outputs] == [
            ("column", "entities"),
            ("named_result", "entities"),
        ]

        # grounding/evidence per the NER precedent: map.ner's own span writer
        # ran (entities-unified-output-and-spans-v1), not map.extract's
        # generic grounding wrapper.
        evidence = list_cell_evidence(
            project, sheet_id=sheet_id, row_id=row_id, column_id=column["id"]
        )
        assert evidence["links"], "map.ner(engine=llm) must write an evidence link"
        link = evidence["links"][0]
        assert link["span_count"] == 1
        viewer = resolve_evidence_viewer(project, link["id"])
        artifact = viewer["artifacts"][0]
        assert artifact["artifact_kind"] == "text"
        span = artifact["spans"][0]
        assert span["quote"] == "Ada Lovelace"
        assert span["selector"]["char_start"] == 0
        assert span["selector"]["char_end"] == 12
    finally:
        project.close()


def test_returned_ner_checkpoint_keeps_private_evidence_when_publish_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / "ner-llm-evidence-rollback.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada Lovelace",
                    "type": "person",
                    "start": 0,
                    "end": 12,
                    "score": 0.92,
                }
            ]
        }
    )
    original = ner_module._write_ner_result_evidence

    def fail_after_evidence(*args, **kwargs):  # noqa: ANN002, ANN003
        original(*args, **kwargs)
        raise RuntimeError("evidence write failed")

    monkeypatch.setattr(ner_module, "_write_ner_result_evidence", fail_after_evidence)
    project = Project(project_path)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_evidence_rollback@sha256:stable"
    )
    try:
        result = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert result.status == "failed"
        assert result.run_id is not None and result.receipt_id is not None
        assert [error.code for error in result.errors] == ["project_write_failed"]
        assert result.errors[0].message == "project write failed"
        assert len(adapter.requests) == 1
        run = project.db.execute("SELECT * FROM runs").fetchone()
        assert run["status"] == "failed"
        assert run["current_attempt_id"] is None
        assert run["cost_actual"] == pytest.approx(0.004)
        checkpoint = project.db.execute(
            "SELECT state,payload FROM effect_checkpoints WHERE family='row_effect'"
        ).fetchone()
        assert checkpoint["state"] == "returned"
        payload = json.loads(checkpoint["payload"])["entities"]
        assert payload[_NER_EVIDENCE_KEY]["schema_version"].endswith(
            "ner_input_capture.v1"
        )
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
        )
        monkeypatch.setattr(ner_module, "_write_ner_result_evidence", original)
        before = tuple(project.db.iterdump())
        replay = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert replay.model_dump() == result.model_dump()
        assert len(adapter.requests) == 1
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


def test_map_ner_llm_engine_drops_malformed_model_entities(tmp_path: Path) -> None:
    """postprocess_value's canonicalization runs on the llm path too: a
    malformed model entity (missing start/end) is dropped rather than
    corrupting the cell, same as spacy/gliner (canonicalize_entities)."""
    project_path = tmp_path / "ner-llm-dispatch-malformed.frisket"
    sheet_id = _seed_project(project_path)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:malformed"
    )
    router, _adapter = _stub_router(
        {
            "entities": [
                {"text": "Ada Lovelace", "type": "person", "start": 0, "end": 12},
                {"type": "person"},  # missing text/start/end -- dropped
            ]
        }
    )

    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert result.status == "completed", result.errors
        column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
        (entities,) = project.get_values(sheet_id, column["id"]).values()
        assert entities == [
            {
                "text": "Ada Lovelace",
                "type": "person",
                "start": 0,
                "end": 12,
                "score": None,
                "fingerprint": "ada lovelace",
            }
        ]
    finally:
        project.close()


def test_map_ner_llm_highlights_every_exact_repeated_mention(tmp_path: Path) -> None:
    project_path = tmp_path / "ner-llm-repeated-mention.frisket"
    sheet_id = _seed_project(project_path, body="Ada met Ada.")
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:repeated"
    )
    # The model reports one mention and an unhelpful offset. Alignment is owned
    # by the server, which expands the exact quote to both occurrences.
    router, _adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada",
                    "type": "person",
                    "start": 8,
                    "end": 11,
                }
            ]
        }
    )

    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert result.status == "completed", result.errors
        column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
        values = project.get_values(sheet_id, column["id"])
        row_id, entities = next(iter(values.items()))
        assert [(entity["start"], entity["end"]) for entity in entities] == [
            (0, 3),
            (8, 11),
        ]
        link = list_cell_evidence(
            project, sheet_id=sheet_id, row_id=row_id, column_id=column["id"]
        )["links"][0]
        viewer = resolve_evidence_viewer(project, link["id"])
        spans = viewer["artifacts"][0]["spans"]
        assert [
            (span["selector"]["char_start"], span["selector"]["char_end"])
            for span in spans
        ] == [(0, 3), (8, 11)]
    finally:
        project.close()
