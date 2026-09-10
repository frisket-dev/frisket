from __future__ import annotations

import itertools
import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    create_sheet,
)
from frisket.actions.collection_expand import CollectionOutput
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    CollectionReader,
    RowSource,
    TableResult,
    TableRow,
)
from frisket.contracts.action import Receipt
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store.receipts import ReceiptStore

from frisket.engine.executor import actions as executor_actions
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.sources.youtube import YouTubeChannelListing
from frisket.engine.store import Project

PROJECT_ID = "project-collection-expand"
CHANNEL_URL = "https://www.youtube.com/@investigative"
OTHER_CHANNEL_URL = "https://www.youtube.com/@same-looking-channel"
_KEYGEN = itertools.count(1)


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"vid{index:07d}",
            "title": f"Video {index}",
            "url": f"https://www.youtube.com/watch?v=vid{index:07d}",
            "channel": "@investigative",
            "channel_id": "UCinvestigative123456",
        }
        for index in range(count)
    ]


def _fake_channel_provider(
    entry_count: int,
    preview_count: int,
    calls: list[dict[str, int | None]] | None = None,
):
    observed_calls = [] if calls is None else calls

    def provider(
        channel_url: str,
        *,
        channel_id: str | None,
        channel_handle: str | None,
        max_pages: int | None,
        item_cap: int | None,
    ) -> YouTubeChannelListing:
        observed_calls.append({"max_pages": max_pages, "item_cap": item_cap})
        entries = _entries(entry_count)
        limits = [limit for limit in (item_cap, max_pages and max_pages * 100) if limit]
        if limits:
            entries = entries[: min(limits)]
        return YouTubeChannelListing(
            entries=entries,
            channel_id=channel_id,
            channel_handle=channel_handle,
            channel_title="Investigative",
            provider_facts={"service": "yt-dlp", "playlist_count": preview_count},
        )

    return provider


@pytest.fixture
def inject_channel_provider(monkeypatch: pytest.MonkeyPatch):
    from frisket.engine.executor import collection_read as family

    def _inject(entry_count: int, preview_count: int) -> list[dict[str, int | None]]:
        calls: list[dict[str, int | None]] = []
        monkeypatch.setattr(
            family,
            "_ENUMERATOR_PROVIDER",
            _fake_channel_provider(entry_count, preview_count, calls),
            raising=False,
        )
        return calls

    return _inject


def _seed_source_cell(project_path: Path, url: str = CHANNEL_URL) -> dict[str, int]:
    project = Project.create(project_path, name="Channels")
    try:
        sheet_id = project.add_sheet("Channels")
        col_id = project.add_column(sheet_id, "channel_url", type="link")
        row_ids = project.add_rows(
            sheet_id, [{"channel_url": url}], {"channel_url": col_id}
        )
        return {
            "sheet_id": sheet_id,
            "column_id": col_id,
            "row_id": int(row_ids[0]) if isinstance(row_ids, list) else int(row_ids),
        }
    finally:
        project.close()


def _expand_action(
    *,
    source_sheet_id: int,
    source_column_id: int,
    source_row_id: int,
    target_sheet_name: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = f"collection_expand@sha256:{next(_KEYGEN)}"
    params: dict[str, Any] = {
        "source_sheet_id": source_sheet_id,
        "source_column_id": source_column_id,
        "source_row_id": source_row_id,
    }
    return {
        "action_id": "derive.collection_expand",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _run(project: Project, action: dict[str, Any]):
    return executor_actions.run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
    )


# ---------------------------------------------------------------------------
# Happy path: child sheet + one row per item + lineage to the source row
# ---------------------------------------------------------------------------


def test_channel_expands_to_child_sheet_with_lineage(
    tmp_path: Path, inject_channel_provider
) -> None:
    inject_channel_provider(entry_count=3, preview_count=3)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="Investigative Videos",
            ),
        )
        assert result.status == "completed", result.errors

        child = project.db.execute(
            "SELECT id, parent_sheet_id FROM sheets "
            "WHERE name='Investigative Videos' AND hidden=0"
        ).fetchone()
        assert child is not None
        # Child sheet's parent is the source sheet (lineage).
        assert int(child["parent_sheet_id"]) == seed["sheet_id"]

        rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows WHERE sheet_id=?",
            (int(child["id"]),),
        ).fetchall()
        assert len(rows) == 3
        # Every child row's parent is the single source cell's row (lineage).
        assert all(int(r["parent_row_id"]) == seed["row_id"] for r in rows)
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Cost gate on the unified 402 envelope
# ---------------------------------------------------------------------------


def test_over_default_cap_without_confirmed_gates_at_402(
    tmp_path: Path, inject_channel_provider
) -> None:
    # preview_count 150 exceeds default_cap (100); Solo has no hard cap.
    inject_channel_provider(entry_count=150, preview_count=150)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        gated = _run(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="Gated",
            ),
        )
        assert gated.status == "needs_confirmation", gated.status
        assert v1_action_result_http_status(gated) == 402
        err = gated.errors[0]
        assert err.needs_confirmation is True
        assert err.field == "confirmation"
        assert err.details.get("preview_count") == 150
        assert err.details.get("default_cap") == 100
        assert err.details.get("hard_cap") is None
        assert err.details.get("source_url") == CHANNEL_URL
        assert err.details.get("collection_identity")
        assert str(err.details.get("enumerated_items_hash", "")).startswith("sha256:")
        assert len(str(err.details.get("promise_set_hash", ""))) == 64
        # Nothing materialized on the gate.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Gated'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_confirmed_completes_over_default_cap(
    tmp_path: Path, inject_channel_provider
) -> None:
    inject_channel_provider(entry_count=150, preview_count=150)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        action = _expand_action(
            source_sheet_id=seed["sheet_id"],
            source_column_id=seed["column_id"],
            source_row_id=seed["row_id"],
            target_sheet_name="Confirmed",
        )
        gated = _run(project, action)
        assert gated.status == "needs_confirmation"
        promise_set_hash = gated.errors[0].details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str)

        action["confirmation"] = promise_set_hash
        confirmed = _run(
            project,
            action,
        )
        assert confirmed.status == "completed", confirmed.errors
        child = project.db.execute(
            "SELECT id FROM sheets WHERE name='Confirmed' AND hidden=0"
        ).fetchone()
        assert child is not None
        count = project.db.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (int(child["id"]),)
        ).fetchone()[0]
        assert count == 150
    finally:
        project.close()


def test_confirmation_hash_rejects_same_count_after_source_collection_changes(
    tmp_path: Path, inject_channel_provider
) -> None:
    """A same-size/same-title collection is still a different approved scope."""

    inject_channel_provider(entry_count=150, preview_count=150)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        action = _expand_action(
            source_sheet_id=seed["sheet_id"],
            source_column_id=seed["column_id"],
            source_row_id=seed["row_id"],
            target_sheet_name="Scope-bound",
        )
        first = _run(project, action)
        assert first.status == "needs_confirmation"
        first_hash = first.errors[0].details.get("promise_set_hash")
        assert isinstance(first_hash, str)

        project.apply_edits(
            [
                {
                    "row_id": seed["row_id"],
                    "column_id": seed["column_id"],
                    "value": OTHER_CHANNEL_URL,
                }
            ]
        )
        action["confirmation"] = first_hash
        changed = _run(project, action)
        assert changed.status == "needs_confirmation"
        assert changed.errors[0].details.get("source_url") == OTHER_CHANNEL_URL
        assert changed.errors[0].details.get("promise_set_hash") != first_hash
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Scope-bound'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_confirmed_solo_expansion_completes_legacy_hard_cap_plus_one(
    tmp_path: Path, inject_channel_provider
) -> None:
    provider_calls = inject_channel_provider(entry_count=1001, preview_count=1001)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        action = _expand_action(
            source_sheet_id=seed["sheet_id"],
            source_column_id=seed["column_id"],
            source_row_id=seed["row_id"],
            target_sheet_name="Large confirmed expansion",
        )
        gated = _run(project, action)
        assert gated.status == "needs_confirmation"
        promise_set_hash = gated.errors[0].details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str)

        action["confirmation"] = promise_set_hash
        result = _run(project, action)

        assert result.status == "completed", result.errors
        assert provider_calls
        assert all(
            call["item_cap"] is None or int(call["item_cap"]) >= 1001
            for call in provider_calls
        )
        assert all(
            call["max_pages"] is None or int(call["max_pages"]) >= 11
            for call in provider_calls
        )
        child = project.db.execute(
            "SELECT id FROM sheets WHERE name='Large confirmed expansion' AND hidden=0"
        ).fetchone()
        assert child is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (int(child["id"]),)
            ).fetchone()[0]
            == 1001
        )
        assert (
            project.db.execute(
                "SELECT COUNT(DISTINCT parent_row_id) FROM rows WHERE sheet_id=?",
                (int(child["id"]),),
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Large confirmed expansion'"
            ).fetchone()[0]
            == 1
        )
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Honest error on a non-collection URL
# ---------------------------------------------------------------------------


def test_non_collection_url_is_an_honest_error(
    tmp_path: Path, inject_channel_provider
) -> None:
    inject_channel_provider(entry_count=1, preview_count=1)
    seed = _seed_source_cell(
        tmp_path / "p.frisket", url="https://www.youtube.com/watch?v=vid0000001"
    )
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="NotACollection",
            ),
        )
        assert result.status == "failed", result.status
        assert result.errors[0].code == "invalid_input_ref"
        assert result.errors[0].needs_confirmation is False
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_the_same_sheet(
    tmp_path: Path, inject_channel_provider
) -> None:
    calls = inject_channel_provider(entry_count=3, preview_count=3)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        key = "collection_expand_replay@sha256:v1"
        first = _run(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="Replay",
                idempotency_key=key,
            ),
        )
        assert first.status == "completed", first.errors
        second = _run(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="Replay",
                idempotency_key=key,
            ),
        )
        assert second.status == "completed", second.errors
        # Replay does NOT create a duplicate sheet.
        sheet_count = project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Replay' AND hidden=0"
        ).fetchone()[0]
        assert sheet_count == 1
        assert first.receipt_id == second.receipt_id
        assert len(calls) == 1
    finally:
        project.close()


def _request_for(seed, *, name="Videos", key="collection"):
    return _expand_action(
        source_sheet_id=seed["sheet_id"],
        source_column_id=seed["column_id"],
        source_row_id=seed["row_id"],
        target_sheet_name=name,
        idempotency_key=key,
    )


def _receipt(project, result):
    return Receipt.model_validate_json(
        ReceiptStore(project).find_by_id(result.receipt_id).body
    )


@pytest.mark.parametrize("change", ["edit", "delete", "undo"])
def test_replay_refuses_changed_or_missing_source_and_undone_publication(
    tmp_path, inject_channel_provider, change
):
    calls = inject_channel_provider(3, 3)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        request = _request_for(seed)
        first = _run(project, request)
        assert first.status == "completed", first.errors
        if change == "undo":
            assert project.undo() == first.op_ids[0]
        else:
            project.apply_edits(
                [
                    {
                        "row_id": seed["row_id"],
                        "column_id": seed["column_id"],
                        "value": OTHER_CHANNEL_URL if change == "edit" else None,
                    }
                ]
            )
        replay = _run(project, request)
        assert replay.errors[0].code == "stale_replay"
        assert len(calls) == 1


def test_empty_collection_keeps_parent_and_replays_without_provider(
    tmp_path, inject_channel_provider
):
    calls = inject_channel_provider(0, 0)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        request = _request_for(seed)
        result = _run(project, request)
        assert result.status == "completed", result.errors
        sheet = project.db.execute(
            "SELECT id,parent_sheet_id FROM sheets WHERE name='Videos'"
        ).fetchone()
        assert sheet["parent_sheet_id"] == seed["sheet_id"]
        assert project.visible_row_ids(sheet["id"]) == []
        assert _run(project, request).receipt_id == result.receipt_id
        assert len(calls) == 1


def test_metadata_has_link_type_no_ai_flag_and_authoritative_provider_facts(
    tmp_path, inject_channel_provider
):
    inject_channel_provider(2, 2)
    seed = _seed_source_cell(tmp_path / "p", url="  " + CHANNEL_URL + "  ")
    with closing(Project(tmp_path / "p")) as project:
        project.db.execute(
            "UPDATE columns SET type='text' WHERE id=?", (seed["column_id"],)
        )
        project.db.commit()
        result = _run(project, _request_for(seed))
        assert result.status == "completed", result.errors
        sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name='Videos'"
        ).fetchone()[0]
        columns = project.db.execute(
            "SELECT name,type,ai_generated FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet,),
        ).fetchall()
        assert [tuple(row) for row in columns] == [
            ("url", "link", 0),
            ("title", "text", 0),
            ("video_id", "text", 0),
            ("channel_title", "text", 0),
            ("published_at", "text", 0),
            ("position", "integer", 0),
        ]
        receipt = _receipt(project, result)
        assert receipt.provider_use == [
            {
                "provider": "youtube",
                "service": "yt-dlp",
                "enumerator": "youtube_channel",
                "items_returned": 2,
                "items_materialized": 2,
                "download": False,
                "external_api": True,
                "cost_actual": 0.0,
            }
        ]
        assert (
            next(
                item.ref
                for item in receipt.inputs
                if item.ref["kind"] == "collection_expand_source_cell"
            )["url"]
            == CHANNEL_URL
        )


def test_same_count_changed_item_identity_requires_new_confirmation(
    tmp_path, monkeypatch
):
    from frisket.engine.executor import collection_read

    seed = _seed_source_cell(tmp_path / "p")
    provider = _fake_channel_provider(150, 150)
    swapped = False

    def changing(*args, **kwargs):
        listing = provider(*args, **kwargs)
        if swapped:
            listing.entries[0]["id"] = "changed00001"
        return listing

    monkeypatch.setattr(collection_read, "_ENUMERATOR_PROVIDER", changing)
    with closing(Project(tmp_path / "p")) as project:
        request = _request_for(seed)
        gated = _run(project, request)
        request["confirmation"] = gated.errors[0].details["promise_set_hash"]
        swapped = True
        changed = _run(project, request)
        assert changed.status == "needs_confirmation"
        assert changed.errors[0].details["promise_set_hash"] != request["confirmation"]


def test_collision_and_changed_request_refuse_before_provider(
    tmp_path, inject_channel_provider
):
    calls = inject_channel_provider(2, 2)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        request = _request_for(seed)
        assert _run(project, request).status == "completed"
        conflict = _run(project, {**request, "sheet_name": "Other"})
        assert conflict.errors[0].code == "idempotency_conflict"
        duplicate = _run(project, {**request, "idempotency_key": "new"})
        assert duplicate.errors[0].code == "duplicate_sheet_name"
        assert len(calls) == 1


def test_receipt_failure_rolls_back_entire_collection_publication(
    tmp_path, inject_channel_provider, monkeypatch
):
    inject_channel_provider(2, 2)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        before = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sheets", "rows", "columns", "ops", "receipts")
        }

        def fail(*args, **kwargs):
            raise RuntimeError("late receipt failure")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail)
        result = _run(project, _request_for(seed))
        assert result.status == "failed"
        assert {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        } == before


class RenamedCollectionParams(ActionParams):
    sheet: int
    column: int
    row: int
    forge: bool = False


def test_supplied_hard_cap_is_absolute(tmp_path, inject_channel_provider, monkeypatch):
    from frisket.engine.executor import collection_read

    calls = inject_channel_provider(5, 5)
    original = collection_read.classify_url

    def classify(*args, **kwargs):
        classification = original(*args, **kwargs)
        return classification.model_copy(
            update={
                "expansion": classification.expansion.model_copy(update={"hard_cap": 2})
            }
        )

    monkeypatch.setattr(collection_read, "classify_url", classify)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        request = {**_request_for(seed), "confirmation": "arbitrary"}
        result = _run(project, request)
        assert result.errors[0].code == "collection_expand_exceeds_hard_cap"
        assert calls == [{"max_pages": None, "item_cap": 2}]
        assert not project.db.execute(
            "SELECT 1 FROM sheets WHERE name='Videos'"
        ).fetchone()


def test_unavailable_entries_keep_original_positions_and_null_metadata(
    tmp_path, monkeypatch
):
    from frisket.engine.executor import collection_read

    original = _fake_channel_provider(3, 3)

    def provider(*args, **kwargs):
        listing = original(*args, **kwargs)
        listing.entries[1]["title"] = "[Private video]"
        return listing

    monkeypatch.setattr(collection_read, "_ENUMERATOR_PROVIDER", provider)
    seed = _seed_source_cell(tmp_path / "p")
    with closing(Project(tmp_path / "p")) as project:
        result = _run(project, _request_for(seed))
        assert result.status == "completed", result.errors
        sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name='Videos'"
        ).fetchone()[0]
        columns = {
            row["name"]: row["id"]
            for row in project.db.execute(
                "SELECT id,name FROM columns WHERE sheet_id=?", (sheet,)
            )
        }
        assert list(project.get_values(sheet, columns["position"]).values()) == [1, 3]
        assert all(
            value is None
            for value in project.get_values(sheet, columns["published_at"]).values()
        )
        enumeration = next(
            item.ref
            for item in _receipt(project, result).inputs
            if item.ref["kind"] == "collection_expand_enumeration"
        )
        assert enumeration["preview_count"] == 3
        assert enumeration["materialized_count"] == 2
        assert enumeration["truncated"] is True


def test_source_change_during_enumeration_refuses_publication(tmp_path, monkeypatch):
    from frisket.engine.executor import collection_read

    seed = _seed_source_cell(tmp_path / "p")
    original = _fake_channel_provider(2, 2)
    with closing(Project(tmp_path / "p")) as project:

        def provider(*args, **kwargs):
            project.apply_edits(
                [
                    {
                        "row_id": seed["row_id"],
                        "column_id": seed["column_id"],
                        "value": OTHER_CHANNEL_URL,
                    }
                ]
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(collection_read, "_ENUMERATOR_PROVIDER", provider)
        result = _run(project, _request_for(seed))
        assert result.errors[0].code == "stale_replay"
        assert not project.db.execute(
            "SELECT 1 FROM sheets WHERE name='Videos'"
        ).fetchone()


def test_custom_params_reader_reuse_ignores_authored_source_claims_and_refuses_forged_lineage(
    tmp_path, inject_channel_provider
):
    calls = inject_channel_provider(2, 2)
    seed = _seed_source_cell(tmp_path / "p")

    def produce(
        params: RenamedCollectionParams, reader: CollectionReader
    ) -> TableResult[CollectionOutput]:
        items = reader.read(
            sheet_id=params.sheet, column_id=params.column, row_id=params.row
        )
        rows = []
        for item in items:
            source = (
                RowSource(sheet_id=params.sheet, row_id=params.row)
                if params.forge
                else item.source
            )
            rows.append(
                TableRow(
                    output=CollectionOutput.model_validate(
                        {
                            key: item.value.get(key)
                            for key in CollectionOutput.model_fields
                        }
                    ),
                    sources=(source,),
                    parent=source,
                )
            )
        return TableResult(
            rows=rows,
            source={
                "kind": "file",
                "path": "forged",
                "provider": "forged",
                "cost_actual": 999,
            },
        )

    registered = ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="collection",
                        title="Collection",
                        description="Custom collection",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(produce),
                    ),
                ),
            ),
        )
    ).get("custom.collection")
    with closing(Project(tmp_path / "p")) as project:
        request = ActionRequest(
            action_id="custom.collection",
            scope={"kind": "project"},
            sheet_name="Custom",
            params={
                "sheet": seed["sheet_id"],
                "column": seed["column_id"],
                "row": seed["row_id"],
            },
            idempotency_key="custom",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        receipt = _receipt(project, result)
        assert "forged" not in json.dumps(receipt.model_dump(mode="json"))
        assert receipt.provider_use[0]["provider"] == "youtube"
        assert (
            run_typed_create_sheet_action(project, "p", bound).receipt_id
            == result.receipt_id
        )
        assert len(calls) == 1
        forged = request.model_copy(
            update={
                "sheet_name": "Forged",
                "params": {**request.params, "forge": True},
                "idempotency_key": "forged",
            }
        )
        failed = run_typed_create_sheet_action(
            project, "p", BoundTypedActionRequest.bind(registered, forged)
        )
        assert failed.status == "failed"
        assert not project.db.execute(
            "SELECT 1 FROM sheets WHERE name='Forged'"
        ).fetchone()
