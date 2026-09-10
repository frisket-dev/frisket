"""Unit tests for the shared media action-family suite.

The nine media modules used to stamp per-action copies of the run-status
decision, receipt-error extraction, provider mapping, and the hash/replay
helpers. `frisket/sdk/media.py` now owns that policy;
these tests pin its behavior directly so a drift in the shared suite fails
here rather than in nine executor suites at once.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from blob_store_helpers import local_blob_path
from frisket.contracts.action import ActionError, Receipt, ReceiptEvidence, ReceiptIO
from frisket.engine.store import Project
from frisket.sdk.media import (
    media_action_status,
    media_output_column_value_hash,
    media_provider_for_engine,
    media_provider_use,
    media_replay_error,
    media_replay_row_ids,
    media_run_failed_errors,
    media_successful_result_row_ids,
    media_text_hash,
    precheck_media_outputs,
)


def _run(status: str, total: int, failed: int, cost: float = 0.0) -> dict[str, Any]:
    return {
        "status": status,
        "total_rows": total,
        "failed_rows": failed,
        "completed_rows": max(total - failed, 0),
        "cost_actual": cost,
    }


# ---------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_run("completed", 3, 0), "completed"),
        (_run("failed", 3, 0), "failed"),
        (_run("completed", 3, 3), "failed"),  # every row failed
        (_run("completed", 3, 1), "partial"),
        (_run("partial", 3, 0), "partial"),
        (_run("completed", 0, 0), "completed"),  # zero-row run is not a failure
        ({"status": None, "total_rows": None, "failed_rows": None}, "completed"),
    ],
)
def test_media_action_status_decisions(run: dict[str, Any], expected: str) -> None:
    assert media_action_status(run) == expected


def test_media_action_status_zero_total_with_failed_rows_is_not_failed() -> None:
    # failed_rows >= total_rows only collapses to failed when total_rows > 0;
    # this matches every original per-module copy.
    assert media_action_status(_run("completed", 0, 1)) == "partial"


# ---------------------------------------------------------------- receipt errors


def test_media_run_failed_errors_only_stamped_on_failed_status() -> None:
    action = SimpleNamespace(kind="media.ocr")
    run = _run("completed", 3, 1)
    assert media_run_failed_errors(action, run, status="partial", code="x") == []
    assert media_run_failed_errors(action, run, status="completed", code="x") == []


def test_media_run_failed_errors_shape() -> None:
    action = SimpleNamespace(kind="media.transcribe")
    run = _run("failed", 4, 4)
    errors = media_run_failed_errors(
        action, run, status="failed", code="transcribe_run_failed"
    )
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, ActionError)
    assert error.code == "transcribe_run_failed"
    assert error.message == "media.transcribe failed for every target row"
    assert error.action_kind == "media.transcribe"
    assert error.details == {
        "total_rows": 4,
        "completed_rows": 0,
        "failed_rows": 4,
    }


# ---------------------------------------------------------------- provider mapping


def test_media_provider_for_engine_alias_hit_and_fallbacks() -> None:
    aliases = {"rapidocr": "local", "dots.mocr": "frisket-sidecar"}
    assert media_provider_for_engine("rapidocr", aliases) == "local"
    assert media_provider_for_engine("dots.mocr", aliases) == "frisket-sidecar"
    # provider/model ids fall back to the provider prefix
    assert media_provider_for_engine("openai/whisper-1", aliases) == "openai"
    # bare unknown engines map to "unknown"
    assert media_provider_for_engine("mystery", aliases) == "unknown"


def test_media_provider_use_zero_calls_uses_engine_alias_and_fact_cost() -> None:
    run = _run("completed", 2, 0, cost=0.5)
    use = media_provider_use(
        [],
        engine="rapidocr",
        run=run,
        provider_for_engine=lambda engine: {"rapidocr": "local"}.get(engine, "unknown"),
    )
    assert use == [
        {
            "provider": "local",
            "model": "rapidocr",
            "model_call_count": 0,
            # The complete call set is empty; a stale run accumulator is not
            # receipt authority.
            "cost_actual": 0.0,
        }
    ]


def test_media_provider_use_groups_calls_by_provider_and_engine() -> None:
    calls = [
        {"provider": "openai", "engine": "whisper-1", "provider_cost_usd": 0.01},
        {"provider": "openai", "engine": "whisper-1", "provider_cost_usd": 0.02},
        {"provider": "modal", "engine": "parakeet", "provider_cost_usd": None},
    ]
    use = media_provider_use(
        calls,
        engine="remote",
        run=_run("completed", 3, 0),
        provider_for_engine=lambda engine: "unused-for-nonzero-calls",
    )
    by_key = {(item["provider"], item["model"]): item for item in use}
    assert by_key[("openai", "whisper-1")]["model_call_count"] == 2
    assert by_key[("openai", "whisper-1")]["cost_actual"] == pytest.approx(0.03)
    assert by_key[("modal", "parakeet")]["model_call_count"] == 1
    assert by_key[("modal", "parakeet")]["cost_actual"] is None


# ---------------------------------------------------------------- output precheck


@pytest.mark.parametrize(
    ("action_kind", "no_output_fields_code", "output_names"),
    [
        ("media.fetch_url", "url_fetch_failed", ["existing"]),
        (
            "media.ytdlp_download",
            "media_download_failed",
            ["existing"],
        ),
        ("media.fetch_url", "url_fetch_failed", []),
        (
            "media.ytdlp_download",
            "media_download_failed",
            [],
        ),
    ],
    ids=[
        "fetch-url-collision",
        "ytdlp-download-collision",
        "fetch-url-no-output-fields",
        "ytdlp-download-no-output-fields",
    ],
)
def test_precheck_media_outputs_returns_durable_validation_errors(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
    no_output_fields_code: str,
    output_names: list[str],
) -> None:
    """Both URL acquisition ops return their public validation errors."""
    from frisket.ops import builtin

    requested_recipes: list[str] = []

    class _Recipe:
        def output_fields(self, runner_spec: dict[str, Any]) -> list[dict[str, str]]:
            assert runner_spec == {"output_name": "existing"}
            return [{"name": name} for name in output_names]

    def get_recipe(name: str) -> _Recipe:
        requested_recipes.append(name)
        return _Recipe()

    monkeypatch.setattr(builtin, "get_recipe", get_recipe)
    project = Project.create(tmp_path / "project.frisket")
    try:
        sheet_id = project.add_sheet("media")
        project.add_column(sheet_id, "existing", type="file")
        result = precheck_media_outputs(
            project,
            SimpleNamespace(sheet_id=sheet_id),
            {"output_name": "existing"},
            action_kind=action_kind,
            no_output_fields_code=no_output_fields_code,
        )

        if output_names:
            expected = ActionError(
                code="output_column_exists",
                message=f"{action_kind} output would overwrite an existing column",
                action_kind=action_kind,
                field="params.output_name",
                details={"columns": ["existing"]},
            )
        else:
            expected = ActionError(
                code=no_output_fields_code,
                message=f"{action_kind} recipe returned no output fields",
                action_kind=action_kind,
                field="params.output_name",
            )
        assert result == expected
        assert requested_recipes == [action_kind]
    finally:
        project.close()


# ---------------------------------------------------------------- hash/replay


def test_media_text_hash_is_prefixed_sha256() -> None:
    text = "https://example.com/audio.mp3"
    expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert media_text_hash(text) == expected


class _FakeValuesProject:
    def __init__(self, values: dict[int, Any]) -> None:
        self._values = values

    def get_values(
        self, sheet_id: int, column_id: int, *, row_ids: list[int]
    ) -> dict[int, Any]:
        return {row_id: self._values.get(row_id) for row_id in row_ids}


def test_media_output_column_value_hash_deterministic_and_value_sensitive() -> None:
    base = _FakeValuesProject({1: "a", 2: {"blob": "sha256:x"}})
    first = media_output_column_value_hash(
        base, sheet_id=1, column_id=2, row_ids=[1, 2]
    )
    second = media_output_column_value_hash(
        base, sheet_id=1, column_id=2, row_ids=[1, 2]
    )
    assert first == second
    changed = _FakeValuesProject({1: "a", 2: {"blob": "sha256:y"}})
    assert (
        media_output_column_value_hash(changed, sheet_id=1, column_id=2, row_ids=[1, 2])
        != first
    )


@pytest.mark.parametrize(
    "row_ids",
    [
        None,  # missing
        [],  # empty
        [1, "2"],  # non-int
        [1, True],  # bool masquerading as int
        [1, 0],  # non-positive
        [1, -2],  # negative
        [1, 1],  # duplicate
    ],
)
def test_media_replay_row_ids_rejects_invalid_refs(row_ids: Any) -> None:
    receipt = SimpleNamespace(action_kind="media.video_frames", receipt_id="r-1")
    ref: dict[str, Any] = {} if row_ids is None else {"row_ids": row_ids}
    result = media_replay_row_ids(ref, receipt=receipt, output_name="frames")
    assert isinstance(result, ActionError)
    assert result.code == "stale_replay"
    assert result.action_kind == "media.video_frames"
    assert result.details == {"receipt_id": "r-1", "output": "frames"}


def test_media_replay_row_ids_passes_valid_refs_through() -> None:
    receipt = SimpleNamespace(action_kind="media.extract_faces", receipt_id="r-2")
    assert media_replay_row_ids(
        {"row_ids": [3, 1, 2]}, receipt=receipt, output_name="faces"
    ) == [3, 1, 2]


@pytest.mark.parametrize(
    (
        "action_kind",
        "output_ref_kind",
        "blob_ref_kind",
        "decision",
        "expected_message",
        "expected_details",
    ),
    [
        (
            "media.extract_faces",
            "media_extract_faces_output_column",
            None,
            "match",
            None,
            None,
        ),
        (
            "media.video_frames",
            "media_video_frames_output_column",
            None,
            "match",
            None,
            None,
        ),
        (
            "media.fetch_url",
            "media_fetch_url_output_column",
            "media_fetch_url_media_blob",
            "match",
            None,
            None,
        ),
        (
            "media.ytdlp_download",
            "media_ytdlp_download_output_column",
            "media_ytdlp_download_media_blob",
            "match",
            None,
            None,
        ),
        (
            "media.extract_faces",
            "media_extract_faces_output_column",
            None,
            "values_changed",
            "media.extract_faces replay output values changed",
            {
                "receipt_id": "receipt-media.extract_faces",
                "output": "output",
                "column_id": 1,
            },
        ),
        (
            "media.video_frames",
            "media_video_frames_output_column",
            None,
            "malformed_ref",
            "media.video_frames replay receipt has invalid row refs",
            {"receipt_id": "receipt-media.video_frames", "output": "output"},
        ),
        (
            "media.fetch_url",
            "media_fetch_url_output_column",
            "media_fetch_url_media_blob",
            "hidden_column",
            "media.fetch_url replay output column is missing",
            {
                "receipt_id": "receipt-media.fetch_url",
                "output": "output",
                "column_id": 1,
                "sheet_id": 1,
            },
        ),
        (
            "media.ytdlp_download",
            "media_ytdlp_download_output_column",
            "media_ytdlp_download_media_blob",
            "blob_row_missing",
            "media.ytdlp_download replay media blob is missing",
            {
                "receipt_id": "receipt-media.ytdlp_download",
                "blob_hash": "missing-blob",
            },
        ),
        (
            "media.fetch_url",
            "media_fetch_url_output_column",
            "media_fetch_url_media_blob",
            "blob_bytes_missing",
            "media.fetch_url replay media blob is missing",
            {
                "receipt_id": "receipt-media.fetch_url",
                "blob_hash": hashlib.sha256(b"replay-blob").hexdigest(),
            },
        ),
        (
            "media.ytdlp_download",
            "media_ytdlp_download_output_column",
            "media_ytdlp_download_media_blob",
            "kind_mismatch",
            "media.ytdlp_download replay receipt has no output column ref",
            {"receipt_id": "receipt-media.ytdlp_download"},
        ),
        (
            "media.extract_faces",
            "media_extract_faces_output_column",
            None,
            "iteration_order",
            "media.extract_faces replay output column was renamed",
            {
                "receipt_id": "receipt-media.extract_faces",
                "output": "first",
                "column_id": 1,
                "expected_name": "renamed-output",
                "current_name": "output",
            },
        ),
        (
            "media.video_frames",
            "media_video_frames_output_column",
            None,
            "retyped_column",
            "media.video_frames replay output column type changed",
            {
                "receipt_id": "receipt-media.video_frames",
                "output": "output",
                "column_id": 1,
                "expected_type": "audio",
                "current_type": "json",
            },
        ),
        (
            "media.fetch_url",
            "media_fetch_url_output_column",
            "media_fetch_url_media_blob",
            "rerun_column",
            "media.fetch_url replay output column changed runs",
            {
                "receipt_id": "receipt-media.fetch_url",
                "output": "output",
                "column_id": 1,
                "expected_run_id": 7,
                "current_run_id": None,
            },
        ),
        (
            "media.ytdlp_download",
            "media_ytdlp_download_output_column",
            "media_ytdlp_download_media_blob",
            "incomplete_blob_ref",
            "media.ytdlp_download replay blob ref is incomplete",
            {
                "receipt_id": "receipt-media.ytdlp_download",
                "ref": {"kind": "media_ytdlp_download_media_blob"},
            },
        ),
    ],
    ids=[
        "extract-faces-match",
        "video-frames-match",
        "fetch-url-match",
        "ytdlp-download-match",
        "extract-faces-value-mismatch",
        "video-frames-malformed-row-ref",
        "fetch-url-hidden-column",
        "ytdlp-download-blob-row-missing",
        "fetch-url-blob-bytes-missing",
        "ytdlp-download-kind-mismatch",
        "extract-faces-output-iteration-order",
        "video-frames-retyped-column",
        "fetch-url-rerun-column",
        "ytdlp-download-incomplete-blob-ref",
    ],
)
def test_media_replay_error_reports_durable_replay_contract(
    tmp_path: Any,
    action_kind: str,
    output_ref_kind: str,
    blob_ref_kind: str | None,
    decision: str,
    expected_message: str | None,
    expected_details: dict[str, Any] | None,
) -> None:
    """Representative receipts retain the public replay decisions."""
    project = Project.create(tmp_path / "project.frisket")
    try:
        sheet_id = project.add_sheet("media")
        column_type = (
            "json"
            if action_kind in {"media.extract_faces", "media.video_frames"}
            else "file"
        )
        column_id = project.add_column(
            sheet_id, "output", type=column_type, ai_generated=True
        )
        row_ids = project.add_rows(
            sheet_id, [{"output": "value"}], {"output": column_id}
        )
        ref: dict[str, Any] = {
            "kind": output_ref_kind,
            "column_id": column_id,
            "sheet_id": sheet_id,
            "name": "output",
            "type": column_type,
            "row_ids": row_ids,
            "value_hash": media_output_column_value_hash(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            ),
        }
        evidence: list[ReceiptEvidence] = []

        if blob_ref_kind is None:
            # The blob loop must not leak into the blob-analysis pair.
            evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": "media_fetch_url_media_blob",
                        "blob_hash": "missing-but-ignored",
                    }
                )
            )
        else:
            blob_hash = project.add_blob(b"replay-blob")
            evidence.extend(
                [
                    ReceiptEvidence(
                        ref={"kind": "unrelated_blob_kind", "blob_hash": "missing"}
                    ),
                    ReceiptEvidence(
                        ref={"kind": blob_ref_kind, "blob_hash": blob_hash}
                    ),
                ]
            )

        if decision == "values_changed":
            ref["value_hash"] = "changed"
        elif decision == "malformed_ref":
            ref["row_ids"] = "not-a-row-id-list"
        elif decision == "hidden_column":
            project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column_id,))
            project.db.commit()
        elif decision == "blob_row_missing":
            evidence[-1] = ReceiptEvidence(
                ref={"kind": blob_ref_kind, "blob_hash": "missing-blob"}
            )
        elif decision == "blob_bytes_missing":
            local_blob_path(project, evidence[-1].ref["blob_hash"]).unlink()
        elif decision in {"kind_mismatch", "iteration_order"}:
            pass
        elif decision == "retyped_column":
            ref["type"] = "audio"
        elif decision == "rerun_column":
            ref["run_id"] = 7
        elif decision == "incomplete_blob_ref":
            evidence[-1] = ReceiptEvidence(ref={"kind": blob_ref_kind})
        else:
            assert decision == "match"

        if decision == "kind_mismatch":
            outputs = [ReceiptIO(name="ignored", ref={"kind": "unrelated_output_kind"})]
        elif decision == "iteration_order":
            outputs = [
                ReceiptIO(name="first", ref=dict(ref, name="renamed-output")),
                ReceiptIO(name="second", ref=dict(ref, type="audio")),
            ]
        else:
            outputs = [
                ReceiptIO(name="ignored", ref={"kind": "unrelated_output_kind"}),
                ReceiptIO(name="output", ref=ref),
            ]

        receipt = Receipt(
            receipt_id=f"receipt-{action_kind}",
            project_id="project",
            action_id="action",
            action_kind=action_kind,
            status="completed",
            outputs=outputs,
            evidence=evidence,
        )
        result = media_replay_error(
            project,
            receipt,
            action_kind=action_kind,
            output_column_ref_kind=output_ref_kind,
            media_blob_ref_kind=blob_ref_kind,
        )

        if expected_message is None:
            assert result is None
        else:
            assert result == ActionError(
                code="stale_replay",
                message=expected_message,
                action_kind=action_kind,
                details=expected_details or {},
            )
    finally:
        project.close()


# ---------------------------------------------------------------- result rows


class _FakeDbProject:
    """Just enough of Project for media_successful_result_row_ids."""

    def __init__(self, successful_row_ids: list[int]) -> None:
        self._successful = successful_row_ids
        self.db = self
        self.executed: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any]) -> "_FakeDbProject":
        self.executed.append((sql, params))
        return self

    def fetchall(self) -> list[dict[str, int]]:
        return [{"row_id": row_id} for row_id in self._successful]


def test_media_successful_result_row_ids_preserves_input_order() -> None:
    project = _FakeDbProject(successful_row_ids=[2, 5])
    result = media_successful_result_row_ids(
        project, run_id=7, column_id=9, row_ids=[5, 3, 2]
    )
    assert result == [5, 2]
    sql, params = project.executed[0]
    assert "outcome NOT IN ('model_error', 'invalid_output', 'row_error')" in sql
    assert params == [7, 9, 5, 3, 2]


def test_media_successful_result_row_ids_empty_input_short_circuits() -> None:
    project = _FakeDbProject(successful_row_ids=[])
    assert (
        media_successful_result_row_ids(project, run_id=1, column_id=1, row_ids=[])
        == []
    )
    assert project.executed == []
