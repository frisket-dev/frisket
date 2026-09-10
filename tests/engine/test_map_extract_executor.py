from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    UndoRerun,
    _insert_running_reservation,
    case_env,
)
from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project
from tests.engine.extract_typed_chain_helpers import typed_extract_request

REPLY = {
    "people": ["Ada Lovelace"],
    "contact": {"name": "Ada Lovelace", "organization": "Analytical Engine Club"},
}

# Reset by _patch_router at the start of every harness test for this case;
# asserts on it are only meaningful behind that patch.
_REQUESTS: list[LLMRequest] = []


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        _REQUESTS.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=71,
            tokens_out=31,
            cost=0.006,
            model=req.model,
        )


class _FailingAdapter:
    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        _REQUESTS.append(req)
        raise LLMError("provider unavailable", status=503, retryable=False)


def _stub_router(reply: dict[str, Any]) -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _StubAdapter(reply)  # noqa: SLF001
    return router


def _failing_router() -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = _FailingAdapter()  # noqa: SLF001
    return router


def _patch_router(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    del monkeypatch
    _REQUESTS.clear()
    return {"router": _stub_router(REPLY)}


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def _map_extract_action(
    sheet_id: int,
    *,
    idempotency_key: str = "map_extract@sha256:stable",
    model: str = "anthropic/claude-haiku-4-5",
    source: list[str] | dict[str, str] | None = None,
) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=source if source is not None else ["filename", "media"],
        model=model,
        context="Rows are images of contact cards.",
        instruction="Extract the visible people and primary contact record.",
        fields=[
            {
                "name": "people",
                "type": "list",
                "description": "Names visible in the image.",
                "items": {"type": "string"},
            },
            {
                "name": "contact",
                "type": "json",
                "description": "Primary contact details.",
                "properties": {
                    "name": {"type": "string"},
                    "organization": {"type": "string"},
                },
            },
        ],
        idempotency_key=idempotency_key,
    )


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    from frisket.engine.executor import run_action_spec

    image_path = tmp_path / "card.png"
    image_path.write_bytes(_png_header(17, 11))
    import_result = run_action_spec(
        project,
        {
            "action_id": "import.files",
            "scope": {"kind": "project"},
            "sheet_name": "Media",
            "params": {
                "files": [
                    {
                        "path": str(image_path),
                        "filename": image_path.name,
                        "mime": "image/png",
                    }
                ],
            },
            "idempotency_key": "vision/import_files@sha256:card",
        },
        project_id="project-map-extract",
    )
    assert import_result.status == "completed", import_result.errors
    sheet_id = import_result.outputs[0].sheet_id
    assert sheet_id is not None
    return {"sheet_id": sheet_id}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_extract_action(seeded["sheet_id"])


def _client_capability_field_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Capabilities are no longer a client-authored field: a typed request that
    # tries to carry one (even the "right" one) is refused at the boundary.
    action = _map_extract_action(
        seeded["sheet_id"], idempotency_key="map_extract@sha256:missing-capability"
    )
    action["capabilities"] = ["project:write"]
    return action


def _invalid_field_type_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_extract_action(
        seeded["sheet_id"], idempotency_key="map_extract@sha256:bad-field-type"
    )
    action["params"]["fields"][0]["type"] = "unsupported"
    return action


def _duplicate_labels_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_extract_action(
        seeded["sheet_id"], idempotency_key="map_extract@sha256:duplicate-labels"
    )
    action["params"]["fields"] = [
        {"name": "role", "type": "category", "labels": ["staff", "staff"]}
    ]
    return action


def _unconfirmed_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # No ``confirmation`` echo at all: the gate must quote before any model work.
    return _map_extract_action(
        seeded["sheet_id"],
        idempotency_key="map_extract@sha256:unconfirmed",
        model="anthropic/unpriced-test-model",
    )


def _collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_extract_action(
        seeded["sheet_id"], idempotency_key="map_extract@sha256:collision"
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.store.runs import RunResultStore

    sheet_id = seeded["sheet_id"]
    assert result.run_id is not None
    assert {output.name for output in result.outputs} == {"people", "contact"}
    # The stub router saw the media blob as an image content part.
    assert len(_REQUESTS) == 1
    user_content = _REQUESTS[0].messages[1]["content"]
    assert any(part.get("type") == "image" for part in user_content)

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    assert columns["people"]["type"] == "json"
    assert columns["contact"]["type"] == "json"
    assert columns["people"]["current_run_id"] == result.run_id
    people_values = project.get_values(sheet_id, int(columns["people"]["id"]))
    contact_values = project.get_values(sheet_id, int(columns["contact"]["id"]))
    assert next(iter(people_values.values())) == ["Ada Lovelace"]
    assert next(iter(contact_values.values())) == REPLY["contact"]

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 1
    assert model_calls[0]["provider"] == "anthropic"
    # A real provider call records provider cost and its credential
    # provenance; billability is a private settlement verdict, not a fact row.
    assert model_calls[0]["credential_source"] == "local"
    assert model_calls[0]["provider_cost_usd"] is not None

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "map.extract"
    assert receipt.provider_use[0]["model_call_count"] == 1
    refs = [
        item.ref
        for section in (receipt.inputs, receipt.outputs, receipt.evidence)
        for item in section
    ]
    ref_kinds = {ref["kind"] for ref in refs}
    assert {
        "model_rows_input_column",
        "map_result_column",
        "named_result",
        "typed_action_request",
        "model_rows_model_calls",
        "map_rows_run_counts",
    } <= ref_kinds
    # The media column is recorded as an image input over the run's rows.
    media_input = next(
        ref
        for ref in refs
        if ref["kind"] == "model_rows_input_column" and ref["name"] == "media"
    )
    assert media_input["type"] == "image"
    assert media_input["row_ids"] == people_column_row_ids(project, sheet_id)
    # The authored field schema rides the published column ref.
    people_ref = next(
        ref
        for ref in refs
        if ref["kind"] == "map_result_column" and ref["name"] == "people"
    )
    assert people_ref["schema"]["type"] == "array"
    assert refs_of_kind(refs, "model_rows_model_calls")[0]["model_call_count"] == 1
    # The run-counts fact carries the prompt digest for model-backed runs.
    prompt_hash = refs_of_kind(refs, "map_rows_run_counts")[0]["prompt_hash"]
    assert isinstance(prompt_hash, str) and prompt_hash


def people_column_row_ids(project: Project, sheet_id: int) -> list[int]:
    return [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
    ]


def refs_of_kind(refs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [ref for ref in refs if ref["kind"] == kind]


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _make_action(seeded)
    action["params"]["instruction"] = "Extract only organizations."
    return action


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    row = project.db.execute(
        "SELECT id, hidden, current_run_id FROM columns "
        "WHERE sheet_id=? AND name='people'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert row is not None
    assert row["hidden"] == 1
    seeded["people_column_id"] = int(row["id"])


def _rerun_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_extract_action(
        seeded["sheet_id"], idempotency_key="map_extract@sha256:undo-rerun"
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first
    revived = project.db.execute(
        "SELECT id, hidden, current_run_id FROM columns "
        "WHERE sheet_id=? AND name='people'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert revived is not None
    assert int(revived["id"]) == seeded["people_column_id"]
    assert revived["hidden"] == 0
    assert revived["current_run_id"] is None


CASES = [
    ExecutorCase(
        kind="map.extract",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_model_router",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_model_calls",
                    "write_receipt",
                    "write_trace",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "invalid_params",
                    "output_column_exists",
                    "model_cost_requires_confirmation",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_router,
        gates=(
            Gate(
                "client_capability_field",
                _client_capability_field_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_extract_field_type",
                _invalid_field_type_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_extract_field_duplicate_labels",
                _duplicate_labels_action,
                "invalid_action_request",
            ),
            Gate(
                "model_cost_requires_confirmation",
                _unconfirmed_action,
                "model_cost_requires_confirmation",
                expected_status="needs_confirmation",
            ),
            Gate(
                "output_column_exists",
                _collision_action,
                "output_column_exists",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "columns": 2,
            "runs": 1,
            "results": 2,
            "model_calls": 1,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        # No Reservation: map.extract replay serves receipt outputs without a
        # stale_replay validation rung (cleared lineage fails as
        # model_run_failed — see the keeper below); in-progress blocking is
        # covered hand-written to keep the router-not-called assert.
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_rerun_action,
            check_rerun=_check_rerun,
        ),
    )
]


def test_map_extract_blank_instruction_is_valid(tmp_path: Path) -> None:
    """ "Additional prompt instructions" is genuinely optional — fields alone
    are a valid extraction spec (was previously rejected as
    invalid_extract_instruction; matches map.classify's context)."""
    from frisket.actions.system import validate_root_action

    action = _map_extract_action(1)
    action["params"]["instruction"] = "   "
    validation = validate_root_action(action)
    assert validation.ok is True, validation.error
    assert validation.params is not None
    assert validation.params["instruction"] == ""


def test_map_extract_replay_and_refusals_never_reenter_the_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay serves outputs from the stored receipt, and conflict refusals
    fail before any model work — the router transport is never crossed
    again after the first run."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert len(_REQUESTS) == 1

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_REQUESTS) == 1

        conflict = env.run(_conflict_action(env.seeded))
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert len(_REQUESTS) == 1


def test_map_extract_running_reservation_blocks_duplicate_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = env.case.make_action(env.seeded)
        _insert_running_reservation(env, action)
        before = env.counts()
        blocked = env.run(action)
        assert blocked.status == "failed"
        assert blocked.errors[0].code == "idempotency_in_progress"
        assert env.counts() == before
        assert _REQUESTS == []


def test_map_extract_replay_uses_generation_lineage_not_scalar_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compatibility scalar is not lineage authority for a managed output."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        env.project.db.execute(
            "UPDATE columns SET current_run_id=NULL WHERE sheet_id=? AND name='people'",
            (env.seeded["sheet_id"],),
        )
        env.project.db.commit()
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_REQUESTS) == 1


def test_map_extract_input_template_renders_text_alongside_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        # A typed source is either columns or one template; the template must
        # name the media column itself for the image part to ride alongside.
        action = _map_extract_action(
            env.seeded["sheet_id"],
            idempotency_key="map_extract@sha256:template-media",
            source={"text": "File: {{ filename }} {{ media }}"},
        )
        result = env.run(action)
        assert result.status == "completed", result.errors
        assert len(_REQUESTS) == 1
        content = _REQUESTS[0].messages[1]["content"]
        assert any(part.get("type") == "image" for part in content)
        text_parts = [
            part.get("text", "") for part in content if part.get("type") == "text"
        ]
        assert any("File: card.png" in text for text in text_parts)


def test_map_extract_provider_failure_writes_failed_receipt_with_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        from frisket.contracts.action import Receipt

        env.run_kwargs["router"] = _failing_router()
        failed = env.run(
            _map_extract_action(
                env.seeded["sheet_id"],
                idempotency_key="map_extract@sha256:provider-outage",
            )
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "model_run_failed"
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (failed.receipt_id,)
        ).fetchone()
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        assert "model_rows_model_calls" in {
            item.ref["kind"] for item in receipt.evidence
        }
