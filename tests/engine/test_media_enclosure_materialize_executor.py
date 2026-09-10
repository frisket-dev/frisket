from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from blob_store_helpers import local_blob_path
from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
)
from frisket.engine.store import Project

PROJECT_ID = "project-media-enclosure-materialize"
WORKFLOW_ID = "rss-transcribe-sample-extract-export"

# Reset by _patch_fetcher at the start of every harness test for this case;
# holds the URLs the stub fetcher was asked to download.
_FETCH_CALLS: list[str] = []


def _fetch(url: str) -> tuple[bytes, str, str, str | None]:
    _FETCH_CALLS.append(url)
    name = url.rsplit("/", 1)[-1]
    return (f"ID3-{name}".encode(), "audio/mpeg", name, None)


def _patch_fetcher(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _FETCH_CALLS.clear()
    monkeypatch.setattr(
        "frisket.ops.enclosures.download_url", lambda url, **kwargs: _fetch(url)
    )
    return {"enclosure_fetcher": _fetch}


def _media_enclosure_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    force: bool = False,
    idempotency_key: str = "media_enclosure_materialize@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if force:
        params["force"] = True
    return {
        "action_id": "media.enclosure_materialize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _seed_enclosure_sheet(
    project: Project, *, name: str = "Podcast", urls: list[str] | None = None
) -> tuple[int, list[int]]:
    sheet_id = project.add_sheet(name)
    cols = {
        "guid": project.add_column(sheet_id, "guid", type="text"),
        "title": project.add_column(sheet_id, "title", type="text"),
        "enclosure_url": project.add_column(sheet_id, "enclosure_url", type="link"),
        "enclosure_mime": project.add_column(sheet_id, "enclosure_mime", type="text"),
        "media_status": project.add_column(sheet_id, "media_status", type="category"),
    }
    urls = (
        urls
        if urls is not None
        else [
            "https://cdn.example/ep1.mp3",
            "https://cdn.example/ep2.mp3",
        ]
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "guid": f"ep{index + 1}",
                "title": f"Episode {index + 1}",
                "enclosure_url": url,
                "enclosure_mime": "audio/mpeg",
                "media_status": "remote",
            }
            for index, url in enumerate(urls)
        ],
        cols,
    )
    return sheet_id, row_ids


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id, row_ids = _seed_enclosure_sheet(project)
    no_enclosure_sheet = project.add_sheet("No Enclosure")
    no_enclosure_row = project.add_rows(
        no_enclosure_sheet,
        [{"title": "No media"}],
        {"title": project.add_column(no_enclosure_sheet, "title", type="text")},
    )[0]
    return {
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "no_enclosure_sheet": no_enclosure_sheet,
        "no_enclosure_row": no_enclosure_row,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _media_enclosure_action(
        sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]
    )


def _invalid_scope_action(seeded: dict[str, Any]) -> dict[str, Any]:
    request = _media_enclosure_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
    )
    request["scope"] = {"kind": "project"}
    return request


def _missing_row_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _media_enclosure_action(
        sheet_id=seeded["sheet_id"],
        row_ids=[999_999],
        idempotency_key="media_enclosure_materialize@sha256:missing-row",
    )


def _no_enclosure_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _media_enclosure_action(
        sheet_id=seeded["no_enclosure_sheet"],
        row_ids=[seeded["no_enclosure_row"]],
        idempotency_key="media_enclosure_materialize@sha256:no-enclosure",
    )


def _columns(project: Project, sheet_id: int) -> dict[str, int]:
    return {
        str(column["name"]): int(column["id"])
        for column in project.columns(sheet_id, include_hidden=True)
    }


def _cell(project: Project, sheet_id: int, row_id: int, column: str) -> Any:
    column_id = _columns(project, sheet_id)[column]
    return project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)


def _receipt(project: Project, receipt_id: str):
    from frisket.contracts.action import Receipt

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _receipt_refs(project: Project, receipt_id: str) -> tuple[dict[str, Any], ...]:
    receipt = _receipt(project, receipt_id)
    return tuple(
        [item.ref for item in receipt.inputs]
        + [item.ref for item in receipt.outputs]
        + [item.ref for item in receipt.evidence]
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    sheet_id = seeded["sheet_id"]
    row_ids = seeded["row_ids"]
    assert len(result.op_ids) == 2
    assert _FETCH_CALLS == [
        "https://cdn.example/ep1.mp3",
        "https://cdn.example/ep2.mp3",
    ]

    media_1 = _cell(project, sheet_id, row_ids[0], "media")
    media_2 = _cell(project, sheet_id, row_ids[1], "media")
    assert media_1["blob"] != media_2["blob"]
    assert media_1["mime"] == "audio/mpeg"
    assert media_1["filename"] == "ep1.mp3"
    assert _cell(project, sheet_id, row_ids[0], "media_status") == "downloaded"
    blob = project.db.execute(
        "SELECT source_url, filename, mime FROM blobs WHERE hash=?",
        (media_1["blob"],),
    ).fetchone()
    assert dict(blob) == {
        "source_url": "https://cdn.example/ep1.mp3",
        "filename": "ep1.mp3",
        "mime": "audio/mpeg",
    }

    refs = _receipt_refs(project, result.receipt_id)
    ref_kinds = {ref["kind"] for ref in refs}
    assert {
        "enclosure_rows",
        "enclosure_download_request",
        "media_blob",
        "media_cell",
        "enclosure_download_network",
    } <= ref_kinds
    rows_ref = next(ref for ref in refs if ref["kind"] == "enclosure_rows")
    assert rows_ref["sheet_id"] == sheet_id
    assert rows_ref["row_ids"] == row_ids
    media_column_id = _columns(project, sheet_id)["media"]
    blob_refs = [ref for ref in refs if ref["kind"] == "media_blob"]
    assert {ref["blob_hash"] for ref in blob_refs} == {
        media_1["blob"],
        media_2["blob"],
    }
    assert {ref["column_id"] for ref in blob_refs} == {media_column_id}
    assert all(ref["may_feed"] == ["media.transcribe"] for ref in blob_refs)
    cell_refs = [ref for ref in refs if ref["kind"] == "media_cell"]
    assert {ref["row_id"] for ref in cell_refs} == set(row_ids)
    assert {ref["status"] for ref in cell_refs} == {"downloaded"}
    assert {ref["column_id"] for ref in cell_refs} == {media_column_id}
    assert all(ref["may_feed"] == ["media.transcribe"] for ref in cell_refs)


def _conflicting_force_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary action, force added.
    return _media_enclosure_action(
        sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"], force=True
    )


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    media_column_id = _columns(project, seeded["sheet_id"])["media"]
    project.apply_edits(
        [
            {
                "row_id": seeded["row_ids"][0],
                "column_id": media_column_id,
                "value": None,
            }
        ],
        label="tamper media replay",
    )


CASES = [
    ExecutorCase(
        kind="media.enclosure_materialize",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "external:media_download"),
            side_effects=frozenset(
                {
                    "fetch_external_media_url",
                    "write_project_blob",
                    "write_media_cell",
                    "write_media_status",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "media_download_failed",
                    "stale_replay",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                }
            ),
            cost_policy_kind="external_metered",
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_fetcher,
        gates=(
            Gate(
                "invalid_scope",
                _invalid_scope_action,
                "invalid_action_request",
            ),
            Gate("missing_row", _missing_row_action, "invalid_input_ref"),
            Gate("no_enclosure_column", _no_enclosure_action, "invalid_input_ref"),
        ),
        expect_counts={"blobs": 2, "columns": 1, "ops": 2, "receipts": 1, "rows": 0},
        check_state=_check_state,
        # Replay reconstructs generic receipt-io names (media_blob.N, rows)
        # rather than the live run's output names; identity is asserted via
        # receipt/op ids and the replay keeper below.
        replay_output_names=False,
        replay_output_kinds=False,
        reservation=Reservation(
            make_conflict=_conflicting_force_action,
            go_stale=_go_stale,
        ),
    )
]


def _run(
    project: Project,
    action: dict[str, Any],
    fetcher: Any,
) -> Any:
    from frisket.engine.executor import run_action_spec

    return run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
        enclosure_fetcher=fetcher,
    )


def _refusing_fetcher(reason: str) -> Any:
    def fetcher(url: str) -> tuple[bytes, str, str, str | None]:
        raise AssertionError(f"{reason} downloaded media: {url}")

    return fetcher


def test_fixed_enclosure_column_type_is_validated_before_fetch(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "wrong-enclosure-type.frisket", name="Media v1")
    try:
        sheet_id = project.add_sheet("Malformed RSS")
        enclosure_col = project.add_column(sheet_id, "enclosure_url", type="integer")
        (row_id,) = project.add_rows(
            sheet_id,
            [{"enclosure_url": 42}],
            {"enclosure_url": enclosure_col},
        )

        result = _run(
            project,
            _media_enclosure_action(sheet_id=sheet_id, row_ids=[row_id]),
            _refusing_fetcher("invalid fixed enclosure column"),
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert result.errors[0].field == "scope.sheet_id"
        assert result.errors[0].details == {
            "column": "enclosure_url",
            "type": "integer",
        }
    finally:
        project.close()


def test_shared_reservation_recheck_executes_enclosure_callback_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_request_hash
    from frisket.engine.executor import enclosure_action as enclosure_materialize
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "reservation-recheck.frisket", name="Media v1")
    try:
        seeded = _seed(project, tmp_path)
        action_payload = _make_action(seeded)
        bound = typed_action_for_request(action_payload)
        action = bound.request
        params_hash = typed_request_hash(bound)
        running = Receipt(
            receipt_id="receipt_enclosure_concurrent",
            project_id=PROJECT_ID,
            action_id="act_enclosure_concurrent",
            action_kind=action.action_id,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="running",
            inputs=[
                ReceiptIO(
                    name="idempotency",
                    ref={
                        "kind": "media_enclosure_idempotency_reservation",
                        "params_hash": params_hash,
                    },
                )
            ],
        )

        def insert_between_reservation_checks(
            checked_project: Project, key: str | None
        ) -> None:
            assert checked_project is project
            assert key == action.idempotency_key
            ReceiptStore(project).insert_running(running)
            return None

        monkeypatch.setattr(
            enclosure_materialize,
            "_receipt_for_idempotency",
            insert_between_reservation_checks,
        )

        result = _run(
            project,
            action_payload,
            _refusing_fetcher("concurrent reservation recheck"),
        )

        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_in_progress"
        assert result.errors[0].details == {"receipt_id": running.receipt_id}
    finally:
        project.close()


def test_replay_and_already_downloaded_rows_never_refetch(tmp_path: Path) -> None:
    """Replaying the receipt reuses stored blobs, and a NEW key over rows whose
    media is already materialized no-ops per row (already_downloaded, zero
    ops) — neither path may touch the network."""
    project = Project.create(tmp_path / "enclosure.frisket", name="Media v1")
    try:
        seeded = _seed(project, tmp_path)
        action = _make_action(seeded)
        first = _run(project, action, _fetch)
        assert first.status == "completed", first.errors

        replay = _run(project, action, _refusing_fetcher("replay"))
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        replay_rows = next(o for o in replay.outputs if o.kind == "rows")
        assert replay_rows.row_ids == seeded["row_ids"]

        already = _run(
            project,
            _media_enclosure_action(
                sheet_id=seeded["sheet_id"],
                row_ids=seeded["row_ids"],
                idempotency_key="media_enclosure_materialize@sha256:already",
            ),
            _refusing_fetcher("already-downloaded row"),
        )
        assert already.status == "completed"
        assert already.op_ids == []
        already_cells = [
            ref
            for ref in _receipt_refs(project, already.receipt_id)
            if ref["kind"] == "media_cell"
        ]
        assert {ref["status"] for ref in already_cells} == {"already_downloaded"}
    finally:
        project.close()


def test_missing_local_blob_is_repaired_by_a_new_run(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "repair.frisket", name="Media v1")
    try:
        seeded = _seed(project, tmp_path)
        first = _run(project, _make_action(seeded), _fetch)
        assert first.status == "completed", first.errors
        media_2 = _cell(project, seeded["sheet_id"], seeded["row_ids"][1], "media")

        repair_calls: list[str] = []

        def repair_fetch(url: str) -> tuple[bytes, str, str, str | None]:
            repair_calls.append(url)
            name = url.rsplit("/", 1)[-1]
            return (f"ID3-{name}".encode(), "audio/mpeg", name, None)

        local_blob_path(project, media_2["blob"]).unlink()
        repaired = _run(
            project,
            _media_enclosure_action(
                sheet_id=seeded["sheet_id"],
                row_ids=[seeded["row_ids"][1]],
                idempotency_key=(
                    "media_enclosure_materialize@sha256:repair-stale-existing"
                ),
            ),
            repair_fetch,
        )
        assert repaired.status == "completed"
        assert repair_calls == ["https://cdn.example/ep2.mp3"]
        with project.materialize_blob(media_2["blob"]) as path:
            assert path.exists()
        repaired_cell = next(
            ref
            for ref in _receipt_refs(project, repaired.receipt_id)
            if ref["kind"] == "media_cell"
        )
        assert repaired_cell["status"] == "downloaded"
        assert repaired_cell["may_feed"] == ["media.transcribe"]
    finally:
        project.close()


def test_existing_foreign_blob_is_replaced_with_current_enclosure(
    tmp_path: Path,
) -> None:
    """A media cell already holding an unrelated imported blob is re-downloaded
    from the row's current enclosure_url — provenance wins over presence."""
    from frisket.engine.store.media_blobs import media_cell

    project = Project.create(tmp_path / "provenance.frisket", name="Media v1")
    try:
        sheet_id = project.add_sheet("Foreign Media")
        cols = {
            "enclosure_url": project.add_column(sheet_id, "enclosure_url", type="link"),
            "enclosure_mime": project.add_column(
                sheet_id, "enclosure_mime", type="text"
            ),
            "media": project.add_column(sheet_id, "media", type="audio"),
        }
        foreign_blob = project.add_blob(
            b"unrelated imported media",
            filename="imported.mp3",
            mime="audio/mpeg",
            source_url="https://uploads.example/imported.mp3",
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "enclosure_url": "https://cdn.example/current.mp3",
                    "enclosure_mime": "audio/mpeg",
                    "media": media_cell(
                        foreign_blob, mime="audio/mpeg", filename="imported.mp3"
                    ),
                }
            ],
            cols,
        )[0]
        calls: list[str] = []

        def provenance_fetch(url: str) -> tuple[bytes, str, str, str | None]:
            calls.append(url)
            return (b"current enclosure bytes", "audio/mpeg", "current.mp3", None)

        result = _run(
            project,
            _media_enclosure_action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                idempotency_key="media_enclosure_materialize@sha256:provenance",
            ),
            provenance_fetch,
        )
        assert result.status == "completed"
        assert calls == ["https://cdn.example/current.mp3"]
        current_media = _cell(project, sheet_id, row_id, "media")
        assert current_media["blob"] != foreign_blob
        provenance_cell = next(
            ref
            for ref in _receipt_refs(project, result.receipt_id)
            if ref["kind"] == "media_cell"
        )
        assert provenance_cell["status"] == "downloaded"
        assert provenance_cell["blob_hash"] == current_media["blob"]
        assert provenance_cell["may_feed"] == ["media.transcribe"]
    finally:
        project.close()


def test_mixed_download_outcomes_produce_partial_status(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "mixed.frisket", name="Media v1")
    try:
        sheet_id, row_ids = _seed_enclosure_sheet(
            project,
            name="Mixed Downloads",
            urls=[
                "https://cdn.example/mixed-ok.mp3",
                "https://cdn.example/mixed-error.mp3",
            ],
        )

        def mixed_fetch(url: str) -> tuple[bytes, str, str, str | None]:
            if url.endswith("mixed-error.mp3"):
                return b"", "", "", "download denied"
            return b"mixed ok", "audio/mpeg", "mixed-ok.mp3", None

        mixed = _run(
            project,
            _media_enclosure_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="media_enclosure_materialize@sha256:mixed",
            ),
            mixed_fetch,
        )
        assert mixed.status == "partial"
        assert mixed.errors[0].code == "media_download_failed"
        mixed_receipt = _receipt(project, mixed.receipt_id)
        assert mixed_receipt.status == "partial"
        feedable_cells = [
            output.ref
            for output in mixed_receipt.outputs
            if output.ref["kind"] == "media_cell"
        ]
        assert [ref["row_id"] for ref in feedable_cells] == [row_ids[0]]
        assert feedable_cells[0]["may_feed"] == ["media.transcribe"]
        failed_cell = next(
            ref
            for ref in _receipt_refs(project, mixed.receipt_id)
            if ref["kind"] == "media_cell" and ref["row_id"] == row_ids[1]
        )
        assert failed_cell["status"] == "error"
        assert failed_cell["may_feed"] == []
    finally:
        project.close()


def test_all_rows_failing_yields_failed_receipt_with_error_trail(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "errors.frisket", name="Media v1")
    try:
        sheet_id, row_ids = _seed_enclosure_sheet(
            project, name="Download Errors", urls=["https://cdn.example/error.mp3"]
        )
        failed = _run(
            project,
            _media_enclosure_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="media_enclosure_materialize@sha256:download-error",
            ),
            lambda url: (b"", "", "", "blocked URL"),
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "media_download_failed"
        assert _cell(project, sheet_id, row_ids[0], "media_status") == "error"
        assert "blocked URL" in _cell(project, sheet_id, row_ids[0], "media_error")
        failed_receipt = _receipt(project, failed.receipt_id)
        assert failed_receipt.status == "failed"
        assert all(
            output.ref["kind"] not in {"media_cell", "media_blob"}
            for output in failed_receipt.outputs
        )
        failed_cell = next(
            ref
            for ref in _receipt_refs(project, failed.receipt_id)
            if ref["kind"] == "media_cell"
        )
        assert failed_cell["status"] == "error"
        assert failed_cell["error"] == "blocked URL"
        assert failed_cell["column_id"] == _columns(project, sheet_id)["media"]
        assert "media.transcribe" not in failed_cell["may_feed"]
    finally:
        project.close()


def test_media_enclosure_receipt_write_failure_clears_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.contracts.action import ActionError, ActionIdentity, ActionResult
    from frisket.engine.executor import (
        enclosure_action as media_enclosure_materialize,
    )

    def failed_write(*args: Any, **kwargs: Any) -> ActionResult:
        return ActionResult(
            action=ActionIdentity(
                kind="media.enclosure_materialize",
                action_id=kwargs["action_id"],
            ),
            status="failed",
            project_id=PROJECT_ID,
            errors=[
                ActionError(
                    code="project_write_failed",
                    message="simulated receipt finalization failure",
                    action_kind="media.enclosure_materialize",
                )
            ],
        )

    project = Project.create(tmp_path / "write-failure.frisket", name="Media v1")
    try:
        seeded = _seed(project, tmp_path)
        monkeypatch.setattr(
            media_enclosure_materialize,
            "_write_media_enclosure_receipt",
            failed_write,
        )
        action = _media_enclosure_action(
            sheet_id=seeded["sheet_id"],
            row_ids=[seeded["row_ids"][0]],
            idempotency_key=(
                "media_enclosure_materialize@sha256:write-failure-after-run"
            ),
        )
        result = _run(
            project,
            action,
            lambda url: (b"write failure media", "audio/mpeg", "wf.mp3", None),
        )
        assert result.status == "failed"
        assert result.receipt_id is None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                (action["idempotency_key"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            _cell(project, seeded["sheet_id"], seeded["row_ids"][0], "media_status")
            == "downloaded"
        )
        assert isinstance(
            _cell(project, seeded["sheet_id"], seeded["row_ids"][0], "media"), dict
        )
    finally:
        project.close()
