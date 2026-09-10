from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    UndoRerun,
    case_env,
)
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimless_test_model_calls

PROJECT_ID = "project-media-transcribe"
WORKFLOW_ID = "rss-transcribe-sample-extract-export"

# Reset by _patch_transcriber at the start of every harness test for this
# case; holds the blob hash each fake ASR call was handed.
_ASR_CALLS: list[str] = []


def _patch_transcriber(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-transcription-key")
    _ASR_CALLS.clear()

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        # The materialized blob path is named by its hash — echo it so
        # check_state can pin per-row routing without shared state.
        digest = Path(path).name
        _ASR_CALLS.append(digest)
        return {
            "text": f"transcript {digest}",
            "segments": [{"start": 0.0, "end": 0.5, "text": f"segment {digest}"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_faster_whisper
    )


def _transcribe_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    engine: str = "faster_whisper",
    output_name: str = "transcript",
    idempotency_key: str = "media_transcribe@sha256:first",
) -> dict[str, Any]:
    columns = input_columns if input_columns is not None else ["media"]
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.transcribe",
        "scope": scope,
        "params": {
            "source": columns[0] if len(columns) == 1 else columns,
            "engine": engine,
        },
        "output_names": {"text": output_name, "segments": f"{output_name}_segments"},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blobs = [
        project.add_blob(
            b"RIFF0000WAVEfmt " + label.encode("ascii"),
            filename=f"{label}.wav",
            mime="audio/wav",
            source_url=f"https://cdn.example/{label}.wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for label in ("ep1", "ep2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": f"Episode {index + 1}",
                "media": media_cell(
                    blob,
                    mime="audio/wav",
                    filename=f"ep{index + 1}.wav",
                ),
            }
            for index, blob in enumerate(blobs)
        ],
        cols,
    )
    raw_sheet = project.add_sheet("Raw URLs")
    raw_row = project.add_rows(
        raw_sheet,
        [{"media": "https://cdn.example/raw.mp3"}],
        {"media": project.add_column(raw_sheet, "media", type="audio")},
    )[0]
    return {
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "blobs": blobs,
        "raw_sheet": raw_sheet,
        "raw_row": raw_row,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["missing"],
        idempotency_key="media_transcribe@sha256:missing-cap",
    )


def _multi_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["media", "title"],
        idempotency_key="media_transcribe@sha256:multi-input",
    )


def _unpriced_engine_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        engine="openai/not-priced-transcribe",
        output_name="gate_transcript",
        idempotency_key="media_transcribe@sha256:cost-gate",
    )


def _source_collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        output_name="title",
        idempotency_key="media_transcribe@sha256:collision",
    )


def _raw_url_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["raw_sheet"],
        row_ids=[seeded["raw_row"]],
        idempotency_key="media_transcribe@sha256:raw-url",
    )


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.executor.temporal_transcripts import (
        resolve_timestamped_transcript,
    )
    from frisket.engine.store.evidence import (
        TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    )

    sheet_id = seeded["sheet_id"]
    row_ids = seeded["row_ids"]
    blobs = seeded["blobs"]
    assert _ASR_CALLS == blobs
    assert {output.name for output in result.outputs} == {
        "transcript",
        "transcript_segments",
        "detected_language",
    }

    columns = _columns(project, sheet_id)
    assert columns["transcript"]["type"] == "timestamped_transcript"
    assert columns["transcript_segments"]["type"] == "json"
    assert columns["detected_language"]["type"] == "category"
    assert columns["transcript"]["current_run_id"] == result.run_id
    transcript_values = project.get_values(
        sheet_id, int(columns["transcript"]["id"]), row_ids=row_ids
    )
    assert transcript_values == {
        row_ids[0]: f"transcript {blobs[0]}",
        row_ids[1]: f"transcript {blobs[1]}",
    }
    for row_id in row_ids:
        resolved = resolve_timestamped_transcript(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=int(columns["transcript"]["id"]),
        )
        assert resolved is not None
        assert resolved.transcript_run_id == result.run_id
        assert resolved.language == "en"
        link_metadata = json.loads(
            project.db.execute(
                "SELECT metadata FROM evidence_links WHERE id=?",
                (resolved.evidence_link_id,),
            ).fetchone()["metadata"]
        )
        assert link_metadata == {
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
            "language": "en",
        }
    segment_values = project.get_values(
        sheet_id, int(columns["transcript_segments"]["id"]), row_ids=[row_ids[0]]
    )
    assert segment_values[row_ids[0]] == [
        {
            "start": 0.0,
            "end": 0.5,
            "text": f"segment {blobs[0]}",
            "segment_index": 0,
        }
    ]

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 2
    assert {call["capability"] for call in model_calls} == {"transcribe"}
    assert {call["engine"] for call in model_calls} == {"faster_whisper"}
    assert {call["provider_kind"] for call in model_calls} == {"local_process"}
    # Local execution is a zero-provider-cost fact with local credential
    # provenance; billability is a private settlement verdict, not an open fact.
    assert {call["provider_cost_usd"] for call in model_calls} == {0.0}
    assert {call["cost_source"] for call in model_calls} == {"free_local"}
    assert {call["credential_source"] for call in model_calls} == {"local"}

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.status == "completed"
    assert receipt.provider_use[0]["model_call_count"] == 2
    assert receipt.provider_use[0]["provider"] == "local"
    assert receipt.provider_use[0]["engine"] == "faster_whisper"
    assert receipt.provider_use[0]["external_api"] is False

    refs = [item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence]
    ref_kinds = {ref["kind"] for ref in refs}
    assert {
        "source_column",
        "map_result_column",
        "typed_action_request",
        "map_rows_run_counts",
        "media_transcribe_temporal_evidence_link",
    } <= ref_kinds
    source_ref = next(ref for ref in refs if ref["kind"] == "source_column")
    assert source_ref["name"] == "media"
    assert source_ref["row_ids"] == row_ids
    artifacts = project.db.execute(
        "SELECT blob_hash, source_row_id FROM source_artifacts WHERE source_sheet_id=?",
        (sheet_id,),
    ).fetchall()
    assert {row["blob_hash"] for row in artifacts} == set(blobs)
    assert {row["source_row_id"] for row in artifacts} == set(row_ids)
    request = next(ref for ref in refs if ref["kind"] == "typed_action_request")
    assert request["params"]["engine"] == "faster_whisper"
    counts = next(ref for ref in refs if ref["kind"] == "map_rows_run_counts")
    assert counts["model_call_count"] == 2
    output_refs = [ref for ref in refs if ref["kind"] == "map_result_column"]
    assert all(ref["value_hash"].startswith("sha256:") for ref in output_refs)
    assert {ref["name"] for ref in output_refs} >= {"transcript", "transcript_segments"}
    named_refs = [ref for ref in refs if ref["kind"] == "named_result"]
    assert len(named_refs) == 1
    assert named_refs[0]["source_action_kind"] == "media.transcribe"
    assert named_refs[0]["route"] == "transcript_segments"
    assert named_refs[0]["schema"] == "transcript_segments"
    assert (
        named_refs[0]["item_schema"]["properties"]["segment_index"]["type"] == "integer"
    )
    assert named_refs[0]["may_feed"] == ["derive.table_from_list"]


def _conflicting_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary action, different output name.
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        output_name="different_transcript",
    )


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='transcript'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert column is not None
    column_id = int(column["id"])
    project.db.execute("DELETE FROM cell_result_heads WHERE column_id=?", (column_id,))
    project.db.execute(
        "UPDATE ops SET status='discarded' WHERE id IN ("
        "SELECT run.op_id FROM runs run JOIN run_output_generations generation "
        "ON generation.run_id=run.id WHERE generation.column_id=?)",
        (column_id,),
    )
    project.db.execute(
        "DELETE FROM run_output_generations WHERE column_id=?", (column_id,)
    )
    project.db.execute("DELETE FROM columns WHERE id=?", (column_id,))
    project.db.commit()


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    transcript = project.db.execute(
        "SELECT id, hidden FROM columns WHERE sheet_id=? AND name='transcript'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert transcript is not None
    assert transcript["hidden"] == 1
    seeded["transcript_column_id"] = int(transcript["id"])


def _rerun_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _transcribe_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        idempotency_key="media_transcribe@sha256:undo-rerun",
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first
    revived = project.db.execute(
        "SELECT id, hidden, current_run_id FROM columns "
        "WHERE sheet_id=? AND name='transcript'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert int(revived["id"]) == seeded["transcript_column_id"]
    assert revived["hidden"] == 0
    assert revived["current_run_id"] is None
    values = project.get_values(
        seeded["sheet_id"], int(revived["id"]), row_ids=seeded["row_ids"]
    )
    assert values == {
        seeded["row_ids"][0]: f"transcript {seeded['blobs'][0]}",
        seeded["row_ids"][1]: f"transcript {seeded['blobs'][1]}",
    }


CASES = [
    ExecutorCase(
        kind="media.transcribe",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:transcribe"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_external_provider",
                    "create_generated_columns",
                    "write_run_results",
                    "write_model_calls",
                    "write_trace",
                    "write_map_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "output_column_exists",
                    "external_rows_failed",
                    "stale_replay",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                }
            ),
            cost_policy_kind="external_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_transcriber,
        gates=(
            Gate(
                "missing_source",
                _missing_source_action,
                "invalid_input_ref",
            ),
            Gate("invalid_input_ref", _multi_input_action, "invalid_action_request"),
            Gate(
                "model_cost_requires_confirmation",
                _unpriced_engine_action,
                "external_cost_requires_confirmation",
                expected_status="needs_confirmation",
            ),
            Gate(
                "output_column_exists",
                _source_collision_action,
                "output_column_exists",
            ),
        ),
        expect_counts={
            "columns": 3,
            "runs": 1,
            "results": 6,
            "model_calls": 2,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            make_conflict=_conflicting_output_action,
            go_stale=_go_stale,
        ),
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_rerun_action,
            check_rerun=_check_rerun,
        ),
    )
]


def _mutate_transcript_output_for_replay(project: Project, receipt_id: str) -> None:
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    body = json.loads(receipt_row["body"])
    ref = next(
        item["ref"]
        for item in body["outputs"]
        if item["ref"].get("name") == "transcript"
        and item["ref"]["kind"] == "map_result_column"
    )
    assert ref["value_hash"].startswith("sha256:")
    project.db.execute(
        "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
        (
            json.dumps("structurally drifted transcript"),
            ref["run_id"],
            ref["row_ids"][0],
            ref["column_id"],
        ),
    )
    project.db.commit()


def test_media_transcribe_published_output_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        with pytest.raises(sqlite3.IntegrityError, match="semantics are immutable"):
            _mutate_transcript_output_for_replay(env.project, first.receipt_id)

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert _ASR_CALLS == env.seeded["blobs"]


def test_media_transcribe_replay_preserves_identity_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_replay_error

    defects = (
        "invalid_ref",
        "missing",
        "renamed",
        "type_changed",
        "run_changed",
        "no_output",
    )
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        base = json.loads(receipt_row["body"])
        bound = typed_action_for_request(_make_action(env.seeded))

        for defect in defects:
            body = json.loads(json.dumps(base))
            if defect == "no_output":
                body["outputs"] = [
                    item
                    for item in body["outputs"]
                    if item["ref"]["kind"] != "map_result_column"
                ]
            else:
                ref = next(
                    item["ref"]
                    for item in body["outputs"]
                    if item["ref"]["kind"] == "map_result_column"
                )
                if defect == "invalid_ref":
                    ref["column_id"] = "not-an-integer"
                elif defect == "missing":
                    ref["column_id"] = 2_147_483_647
                elif defect == "renamed":
                    ref["name"] = "renamed_transcript"
                elif defect == "type_changed":
                    ref["type"] = "json"
                else:
                    assert defect == "run_changed"
                    ref["run_id"] += 1

            error = _typed_replay_error(env.project, bound)(
                Receipt.model_validate(body)
            )
            assert error is not None, defect
            assert error.code == "stale_replay", defect
            assert error.message, defect


def test_a_remote_transcription_meters_settles_and_accrues_against_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BYOK remote path, end to end: fact -> settlement -> cap accrual.

    The local-engine case above already pins the fact writer, but only for a
    free in-process engine, where every money column is legitimately zero —
    so it could not have noticed a remote engine that egressed on the user's
    own key and recorded nothing. This is the same wire with real money on
    it: one `openai/whisper-1` call must produce a metering row carrying the
    observed credential and the provider's own price, that row must settle to
    a non-zero charge under the attempt's pinned card, `borne_by` must name
    the key that was billed, and the spend must land on the capped project
    key. Cut any one of those four links and this goes red.
    """
    import httpx

    from frisket.ai.llm import ModelRouter
    from frisket.engine.jobs.runs import _router_for
    from frisket.execution.attempt import run_attempt_receipts
    from frisket.team.security.secrets import encrypt_secret

    audio_seconds = 10.64

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        assert request.headers.get("authorization") == "Bearer sk-project-openai"
        return httpx.Response(
            200,
            json={
                "text": "hello world",
                "language": "en",
                "duration": audio_seconds,
                "segments": [{"start": 0.0, "end": audio_seconds, "text": "hello"}],
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: mock_client))

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project = env.project
        # A capped project key, exactly as Settings > AI Providers writes it.
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint="sk-p***",
            spend_cap_micro=5_000_000,
        )
        # The composition the queued run handler performs, so the credential
        # provenance on the fact is the production one and not a test literal.
        router = _router_for(project, None)
        assert router.credential_source_for("openai") == "project_key"

        # One row only: the metered quantity has to be traceable to one call.
        action = _transcribe_action(
            sheet_id=env.seeded["sheet_id"],
            row_ids=[env.seeded["row_ids"][0]],
            engine="openai/whisper-1",
            output_name="remote_transcript",
            idempotency_key="media_transcribe@sha256:byok-remote",
        )

        result = env.run(action)
        assert result.status == "completed", result.errors

        calls = RunResultStore(project).model_calls(result.run_id)
        assert len(calls) == 1
        (call,) = calls
        assert call["capability"] == "transcribe"
        assert call["engine"] == "openai/whisper-1"
        assert call["provider"] == "openai"
        assert call["credential_source"] == "project_key"
        assert call["cost_source"] == "pricing_data"
        assert call["provider_cost_usd"] == pytest.approx(audio_seconds * 0.0001)
        assert json.loads(call["units"])["audio_seconds"] == audio_seconds
        assert call["attempt_id"] is not None

        (receipt,) = run_attempt_receipts(project, result.run_id)
        settlement = receipt["settlement"]
        assert settlement["pricing_key"] == "openai/whisper-1.audio_second"
        assert settlement["rated_calls"] == 1
        assert settlement["metered_quantity"] == "10.64"
        assert "unsettleable" not in settlement
        assert float(settlement["charge_usd"]) > 0
        # The Charges panel's "this ran on your own key" sentence.
        assert receipt["borne_by"] == {"credentialed_providers": ["openai"]}

        # the structured completer's cap: a remote transcription is spend against the key that bore
        # it, so the cap it was launched under must see it.
        spend = project.provider_spend_state("openai")
        assert spend is not None
        assert spend.spent_micro == round(audio_seconds * 0.0001 * 1_000_000)
        assert spend.unmetered_calls == 0
        assert spend.cap_enforceable

        # Nothing local ran: the remote engine is the only dispatch that
        # happened, so the local stub the case patches stayed untouched.
        assert _ASR_CALLS == []


def test_over_cap_remote_transcription_is_refused_before_provider_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-LLM recipe can still spend through a project provider key.

    The launch cap is a property of the credential that will be charged, not
    of ``recipe.is_llm``.  Once that key is exhausted, a confirmed remote ASR
    request must fail before the HTTP adapter is called.
    """
    import httpx

    from frisket.ai.llm import ModelRouter
    from frisket.team.security.secrets import encrypt_secret

    provider_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_requests.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "text": "this call should never happen",
                "language": "en",
                "duration": 1.0,
                "segments": [],
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: mock_client))

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project = env.project
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint="sk-p***",
            spend_cap_micro=500,
        )

        # Exhaust the key through the same durable accrual path production
        # metering uses; do not mutate the spend counter directly.
        sheet_id = env.seeded["sheet_id"]
        row_id = env.seeded["row_ids"][0]
        column_id = int(_columns(project, sheet_id)["media"]["id"])
        op_id = project.append_op(
            "map", {"recipe": "prior_provider_spend"}, label="prior spend"
        )
        prior_run_id = RunResultStore(project).start_run(
            op_id, sheet_id, "test.prior_provider_spend"
        )
        write_claimless_test_model_calls(
            project,
            prior_run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "model_calls": [
                        {
                            "fact_version": "frisket.model-call-fact.v1",
                            "capability": "transcribe",
                            "engine": "openai/whisper-1",
                            "provider": "openai",
                            "provider_kind": "platform_api",
                            "credential_source": "project_key",
                            "provider_reported_cost_usd": 0.001,
                            "provider_cost_usd": 0.001,
                            "cost_source": "pricing_data",
                            "units": {"audio_seconds": 10.0},
                        }
                    ],
                }
            ],
        )
        project.db.commit()
        assert project.provider_spend_state("openai").over_cap

        action = _transcribe_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            engine="openai/whisper-1",
            output_name="blocked_remote_transcript",
            idempotency_key="media_transcribe@sha256:over-cap",
        )

        result = env.run(action)

        assert result.status == "failed"
        assert result.run_id is None
        assert result.errors[0].code == "provider_spend_cap_exceeded", (
            result.errors,
            provider_requests,
        )
        assert provider_requests == []


@pytest.mark.parametrize(
    ("response_kind", "expected_response_count", "expected_fact_count"),
    [
        ("http_error", 1, 1),
        ("malformed_200", 1, 1),
        ("transport_error", 0, 0),
    ],
)
def test_remote_transcription_records_only_response_proven_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
    expected_response_count: int,
    expected_fact_count: int,
) -> None:
    """A returned HTTP response is provider use even when the row fails.

    Conversely, a connection failure before any response is not proof that
    the provider accepted or billed the request.  The durable fact, spend-cap
    lower bound, and receipt must all preserve that distinction.
    """
    import httpx

    from frisket.ai.llm import ModelRouter
    from frisket.team.security.secrets import encrypt_secret

    responses = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal responses
        if response_kind == "transport_error":
            raise httpx.ConnectError("provider unavailable", request=request)
        responses += 1
        if response_kind == "http_error":
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(
            200,
            json={"text": "unusable", "segments": "not-a-list"},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: mock_client))

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project = env.project
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint="sk-p***",
            spend_cap_micro=5_000_000,
        )
        action = _transcribe_action(
            sheet_id=env.seeded["sheet_id"],
            row_ids=[env.seeded["row_ids"][0]],
            engine="openai/whisper-1",
            output_name=f"remote_failure_{response_kind}",
            idempotency_key=f"media_transcribe@sha256:{response_kind}",
        )

        result = env.run(action)

        assert result.status == "failed"
        assert responses == expected_response_count
        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == expected_fact_count
        spend = project.provider_spend_state("openai")
        assert spend is not None
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == expected_fact_count

        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = json.loads(receipt_row["body"])
        if not facts:
            assert receipt["provider_use"] == []
            return
        assert receipt["provider_use"][0]["model_call_count"] == expected_fact_count
        assert receipt["provider_use"][0]["cost_actual"] is None
        (fact,) = facts
        assert fact["capability"] == "transcribe"
        assert fact["engine"] == "openai/whisper-1"
        assert fact["provider"] == "openai"
        assert fact["credential_source"] == "project_key"
        assert fact["provider_cost_usd"] is None
        assert fact["cost_source"] == "unknown"
        assert json.loads(fact["units"]) == {"requests": 1}
        assert fact["attempt_id"] is not None


def test_media_transcribe_rerun_replaces_prior_outputs_and_undo_restores_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second visible run onto the same output columns replaces the values;
    undoing it restores the FIRST run's values, segments, and language — the
    temporal layer, not just column visibility."""
    from executor_harness import operation_action

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        generation = {"value": 1}
        blobs = env.seeded["blobs"]

        async def generational_whisper(
            self: transcribe_engines.FasterWhisperAdapter,
            path: str,
            spec: dict[str, Any],
            *,
            should_cancel: Any = None,
        ) -> dict[str, Any]:
            del self, spec
            digest = Path(path).name
            label = "one" if digest == blobs[0] else "two"
            run_label = generation["value"]
            return {
                "text": f"run {run_label} episode {label} transcript",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 0.5,
                        "text": f"run {run_label} episode {label}",
                    }
                ],
                "language": "en" if run_label == 1 else "es",
                "duration": 0.5,
            }

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter, "transcribe", generational_whisper
        )

        sheet_id = env.seeded["sheet_id"]
        row_ids = env.seeded["row_ids"]
        first = env.run(
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="media_transcribe@sha256:visible-rerun-first",
            )
        )
        assert first.status == "completed", first.errors
        columns = _columns(env.project, sheet_id)
        transcript_column_id = int(columns["transcript"]["id"])
        segments_column_id = int(columns["transcript_segments"]["id"])
        language_column_id = int(columns["detected_language"]["id"])

        generation["value"] = 2
        second = env.run(
            _transcribe_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="media_transcribe@sha256:visible-rerun-second",
            )
            | {"replace_existing": True}
        )
        assert second.status == "completed", second.errors
        assert second.run_id != first.run_id
        rerun_columns = _columns(env.project, sheet_id)
        assert int(rerun_columns["transcript"]["id"]) == transcript_column_id
        assert int(rerun_columns["transcript_segments"]["id"]) == segments_column_id
        assert int(rerun_columns["detected_language"]["id"]) == language_column_id
        assert rerun_columns["transcript"]["current_run_id"] == first.run_id

        transcript_values = env.project.get_values(
            sheet_id, transcript_column_id, row_ids=row_ids
        )
        assert transcript_values == {
            row_ids[0]: "run 2 episode one transcript",
            row_ids[1]: "run 2 episode two transcript",
        }
        language_values = env.project.get_values(
            sheet_id, language_column_id, row_ids=row_ids
        )
        assert language_values == {row_ids[0]: "es", row_ids[1]: "es"}

        undo = env.run(
            operation_action(
                "operation.undo",
                key="operation_undo_media_transcribe@sha256:visible-rerun",
                expected_op_id=second.op_ids[0],
            )
        )
        assert undo.status == "completed", undo.errors
        restored = env.project.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?",
            (transcript_column_id,),
        ).fetchone()
        assert restored["current_run_id"] == first.run_id
        restored_values = env.project.get_values(
            sheet_id, transcript_column_id, row_ids=row_ids
        )
        assert restored_values == {
            row_ids[0]: "run 1 episode one transcript",
            row_ids[1]: "run 1 episode two transcript",
        }
        restored_segments = env.project.get_values(
            sheet_id, segments_column_id, row_ids=row_ids
        )
        assert restored_segments == {
            row_ids[0]: [
                {
                    "start": 0.0,
                    "end": 0.5,
                    "text": "run 1 episode one",
                    "segment_index": 0,
                }
            ],
            row_ids[1]: [
                {
                    "start": 0.0,
                    "end": 0.5,
                    "text": "run 1 episode two",
                    "segment_index": 0,
                }
            ],
        }
        restored_languages = env.project.get_values(
            sheet_id, language_column_id, row_ids=row_ids
        )
        assert restored_languages == {row_ids[0]: "en", row_ids[1]: "en"}


def test_unexpected_runner_failure_leaves_no_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:

        class FailingMapRunner:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def run(
                self,
                spec: dict[str, Any],
                *,
                confirmed: bool = False,
                resume_run_id: int | None = None,
            ):
                raise RuntimeError("scheduler exploded")

        # The deps override is per-call, so this bypasses env.run.
        from frisket.engine.executor import run_action_spec

        unexpected_key = "media_transcribe@sha256:unexpected"
        before = env.counts()
        result = run_action_spec(
            env.project,
            _transcribe_action(
                sheet_id=env.seeded["sheet_id"],
                row_ids=[env.seeded["row_ids"][0]],
                output_name="unexpected_transcript",
                idempotency_key=unexpected_key,
            ),
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                map_runner_factory=lambda project, router: FailingMapRunner(
                    project, router
                )
            ),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "external_rows_failed"
        assert env.counts() == before
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                (unexpected_key,),
            ).fetchone()[0]
            == 0
        )


def test_provider_failure_persists_failed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    with case_env(CASES[0], tmp_path, monkeypatch) as env:

        async def fail_faster_whisper(
            self: transcribe_engines.FasterWhisperAdapter,
            path: str,
            spec: dict[str, Any],
            *,
            should_cancel: Any = None,
        ) -> dict[str, Any]:
            raise RuntimeError("ASR provider unavailable")

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter, "transcribe", fail_faster_whisper
        )
        failed = env.run(
            _transcribe_action(
                sheet_id=env.seeded["sheet_id"],
                row_ids=[env.seeded["row_ids"][0]],
                idempotency_key="media_transcribe@sha256:provider-fail",
            )
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "external_rows_failed"
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (failed.receipt_id,)
        ).fetchone()
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "external_rows_failed"


def test_raw_url_cell_never_reaches_transcriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run(_raw_url_action(env.seeded))
        assert result.status == "failed"
        assert result.errors[0].code == "external_rows_failed"
        assert _ASR_CALLS == []
        assert RunResultStore(env.project).model_calls(result.run_id) == []
        for column in _columns(env.project, env.seeded["raw_sheet"]).values():
            if column["name"] == "media":
                continue
            assert column["hidden"] == 1
            values = env.project.get_values(
                env.seeded["raw_sheet"],
                int(column["id"]),
                row_ids=[env.seeded["raw_row"]],
            )
            assert not any(value is not None for value in values.values())


def test_media_transcribe_params_probes() -> None:
    from frisket.actions.types import ActionRequest
    from frisket.sdk.media import (
        media_action_status,
    )

    with pytest.raises(ValueError):
        ActionRequest.model_validate(
            {
                "action_id": "media.transcribe",
                "scope": {"kind": "sheet_rows", "sheet_id": "1"},
                "params": {"source": "media"},
            }
        )
    assert (
        media_action_status({"status": "failed", "total_rows": 0, "failed_rows": 1})
        == "failed"
    )
