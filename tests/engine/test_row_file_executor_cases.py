"""Keep typed row-file actions in the shared executor lifecycle contract."""

from copy import deepcopy

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    UndoRerun,
    assert_pre_dispatch_failure_is_durable,
    case_env,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.ops.capture import url as capture_url
from tests.engine.test_capture_screenshot_action import SCREENSHOT_BYTES, _action
from tests.engine.test_media_extract_faces_executor import (
    _extract_faces_action,
    _patch_face_sandbox,
    _seed as seed_faces,
)
from tests.ops.test_media_video_frames_executor import (
    _patch_ffmpeg_sandbox,
    _seed as seed_frames,
    _video_frames_action,
)


def seed_url(project, _tmp_path):
    sheet = project.add_sheet("Links")
    column = project.add_column(sheet, "url", type="link")
    rows = project.add_rows(
        sheet, [{"url": "https://example.test/media"}], {"url": column}
    )
    return {"sheet_id": sheet, "row_ids": rows}


def patch_fetch(monkeypatch):
    monkeypatch.setattr(
        "frisket.ops.enclosures.download_url",
        lambda url, **kwargs: (b"downloaded file", "text/plain", "result.txt", None),
    )


def patch_screenshot(monkeypatch):
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    monkeypatch.setattr(
        capture_url,
        "render_playwright_url",
        lambda url, **kwargs: capture_url.BrowserUrlRenderResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            html="<title>Media</title>",
            screenshot=SCREENSHOT_BYTES,
        ),
    )


def fetch_action(seeded):
    return {
        "action_id": "media.fetch_url",
        "scope": {"kind": "sheet_rows", "sheet_id": seeded["sheet_id"]},
        "params": {"source": "url"},
        "output_names": {"media": "download"},
        "idempotency_key": "fetch-harness",
    }


def _changed(make, seeded, **changes):
    body = deepcopy(make(seeded))
    body.update(changes)
    return body


def _missing_source(make, seeded):
    body = deepcopy(make(seeded))
    body["params"]["source"] = "missing"
    return body


def _output_column(project, seeded, name):
    return project.db.execute(
        "SELECT id, hidden, current_run_id FROM columns WHERE sheet_id=? AND name=?",
        (seeded["sheet_id"], name),
    ).fetchone()


def _case(kind, seed, make, patch, output_key, output_name, row_count, capability=None):
    def check_state(project, seeded, result):
        column = _output_column(project, seeded, output_name)
        assert column is not None and not column["hidden"]
        values = project.get_values(seeded["sheet_id"], column["id"])
        assert set(values) == set(seeded["row_ids"])
        assert all(values.values())
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        occurrences = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "row_file_output"
        ]
        assert {item["row_id"] for item in occurrences} == set(seeded["row_ids"])
        assert all(item["column_id"] == column["id"] for item in occurrences)

    def stale(project, seeded):
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?",
            (_output_column(project, seeded, output_name)["id"],),
        )
        project.db.commit()

    def undone(project, seeded, _first):
        column = _output_column(project, seeded, output_name)
        assert column["hidden"] == 1
        assert column["current_run_id"] is None
        seeded["original_output_id"] = column["id"]

    def rerun(project, seeded, first, second):
        check_state(project, seeded, second)
        assert second.run_id != first.run_id
        assert (
            _output_column(project, seeded, output_name)["id"]
            == seeded["original_output_id"]
        )

    return ExecutorCase(
        kind=kind,
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=(
                "project:write",
                *((capability,) if capability else ()),
            ),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_receipt",
                    *(
                        {"call_external_provider"}
                        if kind == "media.fetch_url"
                        else set()
                    ),
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "external_rows_failed"
                    if kind == "media.fetch_url"
                    else "map_rows_failed",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "stale_replay",
                }
            ),
            cost_policy_kind="unknown" if kind == "media.fetch_url" else "none",
        ),
        seed=seed,
        make_action=make,
        patch=patch,
        gates=(
            Gate(
                "missing_source",
                lambda seeded: _missing_source(make, seeded),
                "invalid_input_ref",
            ),
        ),
        expect_counts={
            "columns": 1,
            "runs": 1,
            "results": row_count,
            "model_calls": 0,
            "ops": 1,
            "receipts": 1,
            "blobs": 1,
        },
        check_state=check_state,
        reservation=Reservation(
            make_conflict=lambda seeded: _changed(
                make, seeded, output_names={output_key: "different"}
            ),
            go_stale=stale,
        ),
        undo=UndoRerun(
            check_undone=undone,
            rerun_action=lambda seeded: _changed(
                make, seeded, idempotency_key="media-harness-rerun"
            ),
            check_rerun=rerun,
        ),
    )


CASES = [
    _case(
        "media.fetch_url",
        seed_url,
        fetch_action,
        patch_fetch,
        "media",
        "download",
        1,
        "external:media_download",
    ),
    _case(
        "web.capture_screenshot",
        seed_url,
        lambda seeded: _action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        patch_screenshot,
        "screenshot",
        "image",
        1,
        "external:browser_render",
    ),
    _case(
        "media.video_frames",
        seed_frames,
        lambda seeded: _video_frames_action(sheet_id=seeded["sheet_id"]),
        _patch_ffmpeg_sandbox,
        "frames",
        "frames",
        2,
    ),
    _case(
        "media.extract_faces",
        seed_faces,
        lambda seeded: _extract_faces_action(sheet_id=seeded["sheet_id"]),
        _patch_face_sandbox,
        "faces",
        "faces",
        2,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
def test_row_file_pre_dispatch_failure_has_durable_halted_prepare(
    case, tmp_path, monkeypatch
):
    from frisket.engine.runner.map_runner import MapRunner

    with case_env(case, tmp_path, monkeypatch) as env:

        async def fail_before_dispatch(self, *_args, **_kwargs):
            raise RuntimeError("injected pre-dispatch refusal")

        monkeypatch.setattr(MapRunner, "run", fail_before_dispatch)
        before = env.counts()
        result = env.run_primary()
        assert result.status == "failed", result.model_dump_json()
        assert result.errors[0].code == (
            "external_rows_failed"
            if case.kind == "media.fetch_url"
            else "map_rows_failed"
        )
        assert_pre_dispatch_failure_is_durable(env.project, before, env.counts())
        assert env.counts()["blobs"] == before["blobs"]
        assert (
            env.project.db.execute(
                "SELECT count(*) FROM effect_checkpoints"
            ).fetchone()[0]
            == 0
        )
        assert (
            env.project.db.execute("SELECT count(*) FROM source_artifacts").fetchone()[
                0
            ]
            == 0
        )
