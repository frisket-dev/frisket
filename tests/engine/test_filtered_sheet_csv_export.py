from __future__ import annotations

import asyncio
import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.contracts.action import ActionOutput, ActionResult, Receipt


def _seed_project(
    client: TestClient,
) -> tuple[str, int, dict[str, int], dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Filtered CSV"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
        "published": project.add_column(sheet_id, "published", type="date"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "A start", "status": "todo", "published": "2026-01-01"},
            {"title": "B done", "status": "done", "published": "2026-02-01"},
            {"title": "C done", "status": "done", "published": "2026-03-01"},
        ],
        columns,
    )
    return (
        pid,
        sheet_id,
        {"a": row_ids[0], "b": row_ids[1], "c": row_ids[2]},
        columns,
    )


def _filter() -> dict:
    return {"status": {"eq": "done"}}


def _sort() -> list[dict]:
    return [{"column": "published", "dir": "desc"}]


def _query(sheet_id: int) -> dict:
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": _filter(),
        "sort": _sort(),
    }


def _export_action(sheet_id: int, path: Path, query: dict, *, key: str) -> dict:
    return {
        "action_id": "export.sheet_csv",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(path)},
            "query": query,
        },
        "idempotency_key": key,
    }


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_filtered_http_sheet_csv_export_matches_grid_rows(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id, rows, _columns = _seed_project(client)
    params = {"filter": json.dumps(_filter()), "sort": json.dumps(_sort())}

    grid = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data", params=params)
    assert grid.status_code == 200, grid.text
    assert [row["id"] for row in grid.json()["rows"]] == [rows["c"], rows["b"]]

    exported = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={**params, "sheet_id": sheet_id, "format": "csv"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    assert _csv_rows(exported.text) == [
        {"title": "C done", "status": "done", "published": "2026-03-01"},
        {"title": "B done", "status": "done", "published": "2026-02-01"},
    ]

    full = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    assert full.status_code == 200, full.text
    assert _csv_rows(full.text) == [
        {"title": "A start", "status": "todo", "published": "2026-01-01"},
        {"title": "B done", "status": "done", "published": "2026-02-01"},
        {"title": "C done", "status": "done", "published": "2026-03-01"},
    ]


def test_filtered_sheet_csv_action_records_query_evidence_and_replays(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, rows, columns = _seed_project(client)
    output_path = tmp_path / "filtered.csv"
    action = _export_action(
        sheet_id,
        output_path,
        _query(sheet_id),
        key="filtered-sheet-csv@sha256:v1",
    )

    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "completed"
    assert result.action.kind == "export.sheet_csv"
    assert result.receipt_id is not None
    export_output = result.outputs[0]
    assert export_output.kind == "export"
    assert export_output.ref["row_count"] == 2
    assert export_output.ref["row_ids"] == [rows["c"], rows["b"]]
    assert export_output.ref["query"]["kind"] == "sheet.filter"
    assert export_output.ref["query_hash"].startswith("sha256:")

    assert _csv_rows(output_path.read_text(encoding="utf-8")) == [
        {"title": "C done", "status": "done", "published": "2026-03-01"},
        {"title": "B done", "status": "done", "published": "2026-02-01"},
    ]

    project = client.app.state.workspace.get(pid)
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "export.sheet_csv"
    ref_kinds = {item.ref["kind"] for item in receipt.evidence}
    assert {"exported_query", "exported_rows"} <= ref_kinds
    query_ref = next(
        item.ref for item in receipt.evidence if item.ref["kind"] == "exported_query"
    )
    assert query_ref["query_hash"] == export_output.ref["query_hash"]
    assert query_ref["row_ids"] == [rows["c"], rows["b"]]

    project.apply_edits(
        [
            {
                "row_id": rows["c"],
                "column_id": columns["status"],
                "value": "archived",
            }
        ],
        label="move exported row out of filter",
    )
    changed = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={
            "sheet_id": sheet_id,
            "format": "csv",
            "filter": json.dumps(_filter()),
            "sort": json.dumps(_sort()),
        },
    )
    assert _csv_rows(changed.text) == [
        {"title": "B done", "status": "done", "published": "2026-02-01"}
    ]

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.outputs[0].ref["row_ids"] == [rows["c"], rows["b"]]
    assert _csv_rows(output_path.read_text(encoding="utf-8")) == [
        {"title": "C done", "status": "done", "published": "2026-03-01"},
        {"title": "B done", "status": "done", "published": "2026-02-01"},
    ]


def _plain_export_action(sheet_id: int, path: Path, *, key: str) -> dict:
    return {
        "action_id": "export.sheet_csv",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(path)},
        },
        "idempotency_key": key,
    }


def _receipt_for(client: TestClient, pid: str, receipt_id: str) -> Receipt:
    project = client.app.state.workspace.get(pid)
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    return Receipt.model_validate(json.loads(row["body"]))


def _seed_many_rows(
    client: TestClient,
    *,
    count: int = 10_001,
) -> tuple[str, int]:
    """Seed an ordered, filterable sheet without going through an upload body.

    This is intentionally just over the retired local 10k guardrail.  The
    contract exercises the public export boundary, while keeping fixture setup
    independent of the CSV-import scale work.
    """
    pid = client.post("/api/projects", json={"name": "Large CSV export"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    columns = {
        "ordinal": project.add_column(sheet_id, "ordinal", type="number"),
        "state": project.add_column(sheet_id, "state"),
    }
    project.add_rows(
        sheet_id,
        [
            # The bookends prove filtering really happened; the requested
            # descending order is deliberately unlike this insertion order.
            {"ordinal": -1, "state": "drop"},
            *[{"ordinal": ordinal, "state": "keep"} for ordinal in range(count)],
            {"ordinal": count, "state": "drop"},
        ],
        columns,
    )
    return pid, sheet_id


def test_large_export_records_bounded_rowset_summary(
    tmp_path: Path, monkeypatch
) -> None:
    import frisket.engine.executor.action_families.exports as export_family

    monkeypatch.setattr(export_family, "MAX_EXPLICIT_EXPORT_ROW_IDS", 1)
    client = _client(tmp_path)
    pid, sheet_id, _rows, _columns = _seed_project(client)  # 3 rows > threshold 1
    out = tmp_path / "all.csv"
    result = ActionResult.model_validate(
        client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_plain_export_action(sheet_id, out, key="rowset-evidence@sha256:v1"),
        ).json()
    )
    assert result.status == "completed"

    receipt = _receipt_for(client, pid, result.receipt_id)
    kinds = {item.ref["kind"] for item in receipt.evidence}
    assert "exported_rowset" in kinds
    assert "exported_rows" not in kinds  # replaced by the bounded summary
    summary = next(
        item.ref for item in receipt.evidence if item.ref["kind"] == "exported_rowset"
    )
    assert summary["row_count"] == 3
    assert summary["rowset_hash"].startswith("sha256:")
    assert "row_ids" not in summary  # no unbounded row-id array
    assert summary["query_hash"] is None
    assert len(summary["column_ids"]) == 3

    # artifact replay still validates path/bytes/sha256 with the summary evidence
    replay = ActionResult.model_validate(
        client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_plain_export_action(sheet_id, out, key="rowset-evidence@sha256:v1"),
        ).json()
    )
    assert replay.receipt_id == result.receipt_id


def test_small_export_keeps_explicit_row_ids(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id, rows, _columns = _seed_project(client)
    out = tmp_path / "small.csv"
    result = ActionResult.model_validate(
        client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_plain_export_action(sheet_id, out, key="small-rowset@sha256:v1"),
        ).json()
    )
    receipt = _receipt_for(client, pid, result.receipt_id)
    rows_ref = next(
        item.ref for item in receipt.evidence if item.ref["kind"] == "exported_rows"
    )
    assert rows_ref["row_ids"] == [rows["a"], rows["b"], rows["c"]]


def test_solo_filtered_csv_export_over_10k_is_complete_and_uses_bounded_fetches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_many_rows(client)

    # The rowset seam is a behavior-level probe: a filtered full-sheet export
    # must keep its SQL fetches bounded and must not first resolve a complete
    # explicit row-id list.  It deliberately says nothing about the
    # implementation used to turn those batches into HTTP chunks.
    from frisket.server.exports import rowset

    original_batches = rowset.iter_row_id_batches
    calls: list[tuple[list[int] | None, list[int]]] = []

    def observed_batches(*args, **kwargs):
        row_ids = kwargs.get("row_ids")
        if row_ids is None and len(args) >= 3:
            row_ids = args[2]
        for batch in original_batches(*args, **kwargs):
            calls.append((row_ids, batch))
            yield batch

    monkeypatch.setattr(rowset, "iter_row_id_batches", observed_batches)

    exported = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={
            "sheet_id": sheet_id,
            "format": "csv",
            "filter": json.dumps({"state": {"eq": "keep"}}),
            "sort": json.dumps([{"column": "ordinal", "dir": "desc"}]),
        },
    )
    assert exported.status_code == 200, exported.text
    rows = _csv_rows(exported.text)
    assert len(rows) == 10_001
    assert rows[0] == {"ordinal": "10000", "state": "keep"}
    assert rows[-1] == {"ordinal": "0", "state": "keep"}

    # A selected/filter rowset needs bounded paging.  An implementation may
    # retain an explicit page of ids, but never the entire 10,001-row result.
    assert len(calls) >= 11
    assert all(row_ids is None or len(row_ids) <= 1000 for row_ids, _batch in calls)
    assert max(len(batch) for _row_ids, batch in calls) <= 1000


def test_direct_csv_starts_streaming_before_complete_render_in_bounded_chunks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The direct HTTP boundary must not turn a complete CSV into ``Response.body``.

    Invoking the registered route gives us the real route/service response object
    without a bespoke ASGI harness.  We observe the established rowset iterator
    only as a runtime scheduling boundary: no fetch before response construction,
    then bounded fetches while its body is consumed.
    """
    client = _client(tmp_path)
    pid, sheet_id = _seed_many_rows(client)
    from frisket.server.exports import rowset

    original_batches = rowset.iter_row_id_batches
    calls: list[list[int]] = []

    def observed_batches(*args, **kwargs):
        for batch in original_batches(*args, **kwargs):
            calls.append(batch)
            yield batch

    monkeypatch.setattr(rowset, "iter_row_id_batches", observed_batches)

    route = next(
        candidate
        for candidate in client.app.routes
        if getattr(candidate, "path", None) == "/api/projects/{pid}/exports/sheets"
    )
    response = route.endpoint(pid, [sheet_id], "csv", None, None, "escape")
    assert isinstance(response, StreamingResponse)
    assert calls == []  # response construction did not render the data set

    async def consume() -> tuple[list[bytes], int | None]:
        chunks: list[bytes] = []
        fetches_after_first_chunk: int | None = None
        async for chunk in response.body_iterator:
            chunks.append(bytes(chunk))
            if fetches_after_first_chunk is None:
                fetches_after_first_chunk = len(calls)
        return chunks, fetches_after_first_chunk

    chunks, fetches_after_first_chunk = asyncio.run(consume())
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= 64 * 1024
    assert fetches_after_first_chunk is not None
    assert fetches_after_first_chunk < len(calls)
    assert len(calls) >= 11
    assert max(len(batch) for batch in calls) <= 1000


def test_streaming_unfiltered_rowset_orders_duplicate_positions_by_id(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, rows, _columns = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "UPDATE rows SET position=1 WHERE id IN (?, ?)",
        (rows["b"], rows["c"]),
    )

    from frisket.server.exports.rowset import iter_row_id_batches

    batches = list(iter_row_id_batches(project, sheet_id, None, batch_size=1))
    assert batches == [[rows["a"]], [rows["b"]], [rows["c"]]]


def test_explicit_value_page_uses_rowid_lookup_and_preserves_scope(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, rows, columns = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    other_sheet = project.add_sheet("other")
    other_column = project.add_column(other_sheet, "title")
    other_row = project.add_rows(
        other_sheet, [{"title": "outside"}], {"title": other_column}
    )[0]
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows["b"],))

    traced: list[str] = []
    project.db.set_trace_callback(traced.append)
    try:
        values = project.get_values(
            sheet_id,
            columns["title"],
            row_ids=[rows["a"], rows["b"], other_row, 999_999],
        )
    finally:
        project.db.set_trace_callback(None)

    assert values == {rows["a"]: "A start"}
    source_query = next(
        query
        for query in traced
        if "FROM rows r NOT INDEXED LEFT JOIN current_cells c" in query
    )
    query_plan = project.db.execute("EXPLAIN QUERY PLAN " + source_query).fetchall()
    assert any("USING INTEGER PRIMARY KEY" in row["detail"] for row in query_plan)


def test_hosted_csv_export_limit_refuses_before_direct_or_action_delivery(
    tmp_path: Path,
) -> None:
    # Cloud composition supplies a limit explicitly.  No limit object is
    # passed by Solo's normal ``create_app`` path, so this must never become a
    # self-hosted default cap.
    from frisket.server.exports.sheet_csv import SheetExportLimits

    client = _client(
        tmp_path,
        sheet_export_limits=SheetExportLimits(max_rows=2),
    )
    pid, sheet_id, _rows, _columns = _seed_project(client)

    exact = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={
            "sheet_id": sheet_id,
            "format": "csv",
            "filter": json.dumps({"status": {"eq": "done"}}),
        },
    )
    assert exact.status_code == 200, exact.text
    assert len(_csv_rows(exact.text)) == 2

    refused = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={"sheet_id": sheet_id, "format": "csv"},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "sheet export exceeds the hosted row limit of 2"
    assert "content-disposition" not in refused.headers
    assert not refused.headers["content-type"].startswith("text/csv")

    destination = tmp_path / "refused.csv"
    action = _plain_export_action(
        sheet_id,
        destination,
        key="hosted-sheet-csv-too-large@sha256:v1",
    )
    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert run.status_code == 400, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "failed"
    assert result.errors[0].code == "export_rowset_too_large"
    assert result.errors[0].details == {"row_count": 3, "max_rows": 2}
    assert not destination.exists()
    assert not list(tmp_path.glob(".refused.csv.*"))


@pytest.mark.parametrize("surface", ["direct", "action"])
def test_hosted_csv_export_keeps_the_admitted_filtered_snapshot(
    tmp_path: Path,
    monkeypatch,
    surface: str,
) -> None:
    """Delivery and receipt facts must describe the admitted two-row snapshot."""
    from frisket.server.exports import rowset
    from frisket.server.exports.sheet_csv import SheetExportLimits

    client = _client(
        tmp_path,
        sheet_export_limits=SheetExportLimits(max_rows=2),
    )
    pid, sheet_id, _rows, columns = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    original_batches = rowset.iter_row_id_batches
    mutated = False

    def mutate_before_first_fetch(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            project.add_rows(
                sheet_id,
                [
                    {
                        "title": "D added after admission",
                        "status": "done",
                        "published": "2026-04-01",
                    }
                ],
                columns,
            )
            mutated = True
        yield from original_batches(*args, **kwargs)

    monkeypatch.setattr(rowset, "iter_row_id_batches", mutate_before_first_fetch)
    if surface == "direct":
        route = next(
            candidate
            for candidate in client.app.routes
            if getattr(candidate, "path", None) == "/api/projects/{pid}/exports/sheets"
        )
        response = route.endpoint(
            pid,
            [sheet_id],
            "csv",
            json.dumps(_filter()),
            json.dumps(_sort()),
            "escape",
        )
        assert isinstance(response, StreamingResponse)
        assert mutated is False

        async def consume() -> bytes:
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(bytes(chunk))
            return b"".join(chunks)

        body = asyncio.run(consume()).decode("utf-8-sig")
    else:
        destination = tmp_path / "admitted-filtered.csv"
        run = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_export_action(
                sheet_id,
                destination,
                _query(sheet_id),
                key="hosted-filtered-snapshot@sha256:v1",
            ),
        )
        assert run.status_code == 200, run.text
        result = ActionResult.model_validate(run.json())
        assert result.status == "completed"
        export_ref = result.outputs[0].ref
        assert export_ref["row_count"] == export_ref["query_total"] == 2

        receipt = _receipt_for(client, pid, result.receipt_id)
        query_ref = next(
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "exported_query"
        )
        assert query_ref["row_count"] == query_ref["total"] == 2
        body = destination.read_text(encoding="utf-8")

    assert mutated is True
    assert _csv_rows(body) == [
        {"title": "C done", "status": "done", "published": "2026-03-01"},
        {"title": "B done", "status": "done", "published": "2026-02-01"},
    ]


@pytest.mark.parametrize("termination", ["complete", "error", "disconnect"])
def test_direct_csv_closes_its_response_owned_snapshot(
    tmp_path: Path,
    monkeypatch,
    termination: str,
) -> None:
    from starlette.requests import ClientDisconnect

    from frisket.server.exports import rowset

    client = _client(tmp_path)
    pid, sheet_id, _rows, _columns = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    snapshots = []
    original_read_snapshot = project.read_snapshot

    def observed_read_snapshot():
        snapshot = original_read_snapshot()
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(project, "read_snapshot", observed_read_snapshot)
    if termination == "error":

        def failed_batches(*_args, **_kwargs):
            raise RuntimeError("injected CSV row failure")
            yield  # pragma: no cover - make this a generator

        monkeypatch.setattr(rowset, "iter_row_id_batches", failed_batches)

    route = next(
        candidate
        for candidate in client.app.routes
        if getattr(candidate, "path", None) == "/api/projects/{pid}/exports/sheets"
    )
    response = route.endpoint(pid, [sheet_id], "csv", None, None, "escape")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.db.in_transaction

    async def consume() -> None:
        async for _chunk in response.body_iterator:
            pass

    async def disconnect() -> None:
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    if termination == "complete":
        asyncio.run(consume())
    elif termination == "error":
        with pytest.raises(RuntimeError, match="injected CSV row failure"):
            asyncio.run(consume())
    else:
        with pytest.raises(ClientDisconnect):
            asyncio.run(disconnect())

    with pytest.raises(RuntimeError, match="snapshot is closed"):
        _ = snapshot.db


def test_large_filtered_solo_csv_action_streams_staging_and_keeps_receipt_compact(
    tmp_path: Path, monkeypatch
) -> None:
    """The artifact can be large without staging or persisting row-id lists."""
    client = _client(tmp_path)
    pid, sheet_id = _seed_many_rows(client)
    destination = tmp_path / "large.csv"
    query = {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"state": {"eq": "keep"}},
        "sort": [{"column": "ordinal", "dir": "desc"}],
    }
    action = _export_action(
        sheet_id,
        destination,
        query,
        key="large-solo-sheet-csv@sha256:v1",
    )

    # Observe the existing runtime boundaries, rather than naming the future
    # CSV encoder.  A staged write must happen while row batches remain to be
    # fetched, which rejects a full in-memory CSV collection without policing
    # its implementation details.
    from frisket.server.exports import rowset

    original_batches = rowset.iter_row_id_batches
    events: list[tuple[str, int]] = []
    staged_writes: list[int] = []
    original_open = Path.open
    original_write_bytes = Path.write_bytes

    def observed_batches(*args, **kwargs):
        for batch in original_batches(*args, **kwargs):
            events.append(("fetch", len(batch)))
            yield batch

    class ObservedWriter:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            staged_writes.append(len(data))
            events.append(("write", len(data)))
            return self._handle.write(data)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def observed_open(path, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "r")
        handle = original_open(path, *args, **kwargs)
        if path.name.startswith(".large.csv.") and "w" in mode:
            return ObservedWriter(handle)
        return handle

    def reject_single_eager_write(path, data):
        if path.name.startswith(".large.csv."):
            raise AssertionError("action CSV staging must use bounded writes")
        return original_write_bytes(path, data)

    monkeypatch.setattr(rowset, "iter_row_id_batches", observed_batches)
    monkeypatch.setattr(Path, "open", observed_open)
    monkeypatch.setattr(Path, "write_bytes", reject_single_eager_write)

    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "completed"
    assert destination.exists()
    assert len(staged_writes) > 1
    assert max(staged_writes) <= 64 * 1024
    assert sum(staged_writes) == destination.stat().st_size
    first_write = next(
        index for index, (kind, _size) in enumerate(events) if kind == "write"
    )
    assert any(kind == "fetch" for kind, _size in events[first_write + 1 :])

    project = client.app.state.workspace.get(pid)
    stored = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert stored is not None
    # This is deliberately generous for ordinary receipt metadata, while
    # excluding a serialized 10,001-item row-id list.
    assert len(str(stored["body"]).encode("utf-8")) < 16_000
    receipt = Receipt.model_validate(json.loads(stored["body"]))
    rowset = next(
        item.ref for item in receipt.evidence if item.ref["kind"] == "exported_rowset"
    )
    assert rowset["row_count"] == 10_001
    assert "row_ids" not in rowset
    exported_query = next(
        item.ref for item in receipt.evidence if item.ref["kind"] == "exported_query"
    )
    assert exported_query["row_count"] == 10_001
    assert "row_ids" not in exported_query
    assert receipt.value["row_ids"] == []
    assert "row_ids" not in json.dumps(
        receipt.model_dump(mode="json", exclude={"value"})
    )
    assert result.value["row_ids"] == []
    assert "row_ids" not in json.dumps(
        result.model_dump(mode="json", exclude={"value"})
    )


def test_csv_byte_iterator_splits_a_single_large_record_without_losing_bytes(
    tmp_path: Path,
) -> None:
    """The shared direct/action encoder never hands a 200KiB record to I/O."""
    from frisket.server.exports.plan import build_sheet_export_plan
    from frisket.server.exports.sheet_csv import (
        iter_export_csv_bytes,
        render_export_csv,
    )

    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Large record"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("records")
    column = project.add_column(sheet_id, "body")
    project.add_rows(sheet_id, [{"body": "x" * 200_000}], {"body": column})
    plan = build_sheet_export_plan(project, sheet_id, streaming_rowset=True)

    chunks = list(iter_export_csv_bytes(project, plan, bom=False))
    _payload, expected = render_export_csv(project, plan)
    assert b"".join(chunks) == expected.encode("utf-8")
    assert max(map(len, chunks)) <= 64 * 1024


def test_empty_output_row_ids_remain_serialized_outside_large_csv_exports() -> None:
    """Large CSV compaction must not alter the v1 ActionOutput wire contract."""
    ordinary = ActionOutput(kind="edit", ref={}).model_dump(mode="json")
    small_export = ActionOutput(
        kind="export",
        ref={"kind": "export_artifact", "export_kind": "sheet_csv", "row_count": 2},
    ).model_dump(mode="json")
    large_export = ActionOutput(
        kind="export",
        ref={
            "kind": "export_artifact",
            "export_kind": "sheet_csv",
            "row_count": 10_001,
        },
    ).model_dump(mode="json")
    assert set(ordinary) == {
        "kind",
        "name",
        "sheet_id",
        "column_id",
        "row_ids",
        "ref",
    }
    assert ordinary["row_ids"] == []
    assert small_export["row_ids"] == []
    assert "row_ids" not in large_export


def test_large_csv_output_compaction_keeps_nonempty_or_loose_row_ids() -> None:
    artifact = {"kind": "export_artifact", "export_kind": "sheet_csv"}
    nonempty = ActionOutput(
        kind="export", row_ids=[7], ref={**artifact, "row_count": 10_001}
    ).model_dump(mode="json")
    string_count = ActionOutput(
        kind="export", ref={**artifact, "row_count": "10001"}
    ).model_dump(mode="json")
    bool_count = ActionOutput(
        kind="export", ref={**artifact, "row_count": True}
    ).model_dump(mode="json")
    missing_kind = ActionOutput(
        kind="export", ref={"export_kind": "sheet_csv", "row_count": 10_001}
    ).model_dump(mode="json")
    edit_shaped_artifact = ActionOutput(
        kind="edit", ref={**artifact, "row_count": 10_001}
    ).model_dump(mode="json")
    assert nonempty["row_ids"] == [7]
    assert string_count["row_ids"] == []
    assert bool_count["row_ids"] == []
    assert missing_kind["row_ids"] == []
    assert edit_shaped_artifact["row_ids"] == []
