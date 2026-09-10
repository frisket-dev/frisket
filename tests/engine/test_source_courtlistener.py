from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import (
    SqliteJobQueue,
    Worker,
    default_registry,
    enqueue_due_source_polls,
    register_source_poll_handler,
)
from frisket.server.sources.courtlistener import (
    COURTLISTENER_DOCKET_KIND,
    CourtListenerDocketListing,
    CourtListenerDocketPoller,
    CourtListenerProviderError,
    fetch_courtlistener_docket,
)
from frisket.server.sources.runtime import (
    SourcePollContext,
    get_source_poller,
    register_source_poller,
    unregister_source_poller,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


PROJECT_ID = "project-source-courtlistener"
DOCKET_ID = "123456"
DOCKET_URL = "https://www.courtlistener.com/docket/123456/sec-v-acme/"


class FakeCourtListenerProvider:
    def __init__(self, listings: list[CourtListenerDocketListing | Exception]) -> None:
        self._listings = list(listings)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        docket_id: str,
        docket_url: str,
        include_parties: bool,
        include_documents: bool,
        max_entries: int | None,
    ) -> CourtListenerDocketListing:
        self.calls.append(
            {
                "docket_id": docket_id,
                "docket_url": docket_url,
                "include_parties": include_parties,
                "include_documents": include_documents,
                "max_entries": max_entries,
            }
        )
        if not self._listings:
            raise AssertionError("fake CourtListener provider exhausted")
        listing = self._listings.pop(0)
        if isinstance(listing, Exception):
            raise listing
        return listing


@pytest.fixture
def restore_courtlistener_poller() -> Iterator[None]:
    previous = get_source_poller(COURTLISTENER_DOCKET_KIND)
    try:
        yield
    finally:
        if previous is None:
            unregister_source_poller(COURTLISTENER_DOCKET_KIND)
        else:
            register_source_poller(previous, replace=True)


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "source_courtlistener@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["source"] = source_id
    if source is not None:
        params["source"] = source
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _courtlistener_source(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": COURTLISTENER_DOCKET_KIND,
        "name": "SEC v. Acme docket",
        "url": DOCKET_URL,
        "config": {
            "schema_version": "frisket.source.courtlistener_docket.v1",
            "docket_url": DOCKET_URL,
            "keywords": ["injunction", "SETTLEMENT"],
            "include_parties": True,
            "include_documents": True,
            "recap_pdf_policy": "link_only",
            "max_entries_per_poll": 10,
            **(config or {}),
        },
    }


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])


def _columns(project: Project, sheet_id: int) -> list[str]:
    return [
        str(column["name"]) for column in project.columns(sheet_id, include_hidden=True)
    ]


def _rows(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    columns = {
        int(column["id"]): column["name"]
        for column in project.columns(sheet_id, include_hidden=True)
    }
    out: list[dict[str, Any]] = []
    for row in project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
        (sheet_id,),
    ).fetchall():
        values = project.db.execute(
            "SELECT column_id, value FROM cells WHERE row_id=?",
            (row["id"],),
        ).fetchall()
        out.append(
            {
                columns[int(value["column_id"])]: json.loads(value["value"])
                for value in values
            }
        )
    return out


def _table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _docket() -> dict[str, Any]:
    return {
        "id": DOCKET_ID,
        "case_name": "SEC v. Acme Holdings",
        "court_id": "nysd",
        "court_name": "Southern District of New York",
        "docket_number": "1:26-cv-00001",
        "date_filed": "2026-06-20",
        "absolute_url": "/docket/123456/sec-v-acme/",
    }


def _parties() -> list[dict[str, Any]]:
    return [
        {"id": 10, "name": "SEC", "type": "plaintiff"},
        {"id": 11, "name": "Acme Holdings LLC", "type": "defendant"},
    ]


def _entry(
    entry_id: int,
    number: str,
    description: str,
    *,
    date_filed: str = "2026-06-21",
    recap_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "entry_number": number,
        "date_filed": date_filed,
        "description": description,
        "absolute_url": f"/docket/123456/sec-v-acme/#entry-{entry_id}",
        "recap_documents": recap_documents or [],
    }


def _document(
    document_id: int,
    number: str,
    description: str,
) -> dict[str, Any]:
    return {
        "id": document_id,
        "document_number": number,
        "description": description,
        "absolute_url": f"/docket/{DOCKET_ID}/sec-v-acme/{number}/",
        "is_available": True,
    }


def _listing(
    *,
    entries: list[dict[str, Any]],
    documents: list[dict[str, Any]] | None = None,
    parties: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    **facts: Any,
) -> CourtListenerDocketListing:
    return CourtListenerDocketListing(
        docket=_docket(),
        parties=parties if parties is not None else _parties(),
        docket_entries=entries,
        recap_documents=documents if documents is not None else [],
        provider_facts={"service": "fake-courtlistener", **facts},
        warnings=warnings or [],
    )


@pytest.mark.parametrize(
    (
        "max_entries",
        "available_count",
        "has_next",
        "expected_count",
        "expected_entry_calls",
        "expected_truncated",
    ),
    [
        (None, 501, True, 501, 2, False),
        (500, 501, True, 500, 1, True),
        (500, 500, False, 500, 1, False),
    ],
)
def test_courtlistener_provider_separates_total_limit_from_page_size(
    monkeypatch: pytest.MonkeyPatch,
    max_entries: int | None,
    available_count: int,
    has_next: bool,
    expected_count: int,
    expected_entry_calls: int,
    expected_truncated: bool,
) -> None:
    entries = [
        _entry(10_000 + index, str(index + 1), f"Entry {index + 1}")
        for index in range(available_count)
    ]
    second_page_url = "https://www.courtlistener.com/api/rest/v4/docket-entries/?page=2"

    class FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeClient:
        calls: list[tuple[str, dict[str, Any] | None]] = []

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get(
            self, url: str, *, params: dict[str, Any] | None = None
        ) -> FakeResponse:
            self.calls.append((url, params))
            if "/dockets/" in url:
                return FakeResponse(_docket())
            if url == second_page_url:
                return FakeResponse({"results": entries[500:], "next": None})
            if "/docket-entries/" in url:
                return FakeResponse(
                    {
                        "results": entries[:500],
                        "next": second_page_url if has_next else None,
                    }
                )
            raise AssertionError(f"unexpected CourtListener URL: {url}")

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    listing = fetch_courtlistener_docket(
        docket_id=DOCKET_ID,
        docket_url=DOCKET_URL,
        include_parties=False,
        include_documents=False,
        max_entries=max_entries,
    )

    ids = [int(entry["id"]) for entry in listing.docket_entries]
    assert len(ids) == expected_count
    assert len(set(ids)) == expected_count
    assert listing.entries_truncated is expected_truncated
    entry_calls = [url for url, _params in FakeClient.calls if "docket-entries" in url]
    assert len(entry_calls) == expected_entry_calls
    assert entry_calls[0] == (
        "https://www.courtlistener.com/api/rest/v4/docket-entries/"
    )
    if expected_entry_calls == 2:
        assert entry_calls[1] == second_page_url
    first_page_params = next(
        params for url, params in FakeClient.calls if url == entry_calls[0]
    )
    assert first_page_params is not None
    assert 0 < int(first_page_params["page_size"]) <= 500

    source = _courtlistener_source(
        {
            "include_parties": False,
            "include_documents": False,
            "max_entries_per_poll": max_entries,
        }
    )
    if max_entries is None:
        source["config"].pop("max_entries_per_poll")
    polled = CourtListenerDocketPoller(provider=lambda **_kwargs: listing).poll(
        SourcePollContext(source=source, cursor_before=None)
    )
    assert polled.cursor_after["truncated"] is expected_truncated
    cap_warning = (
        f"courtlistener_docket entry cap reached; limited to {max_entries} entries"
    )
    assert (cap_warning in polled.warnings) is expected_truncated


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "https://attacker.example/collect",
        "https://www.courtlistener.com/accounts/login/",
        "https://www.courtlistener.com/api/rest/v4/docket-entries/",
    ],
)
def test_courtlistener_provider_never_follows_unsafe_or_cyclic_next_links(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_next: str,
) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeClient:
        calls: list[str] = []
        headers: dict[str, str] = {}

        def __init__(self, **kwargs: Any) -> None:
            type(self).headers = kwargs.get("headers", {})

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get(
            self, url: str, *, params: dict[str, Any] | None = None
        ) -> FakeResponse:
            self.calls.append(url)
            if "/dockets/" in url:
                return FakeResponse(_docket())
            if "/docket-entries/" in url:
                return FakeResponse(
                    {
                        "results": [_entry(10_000, "1", "First entry")],
                        "next": unsafe_next,
                    }
                )
            raise AssertionError(f"unsafe pagination request: {url}")

    import httpx

    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "test-secret-token")
    monkeypatch.setattr(httpx, "Client", FakeClient)

    try:
        fetch_courtlistener_docket(
            docket_id=DOCKET_ID,
            docket_url=DOCKET_URL,
            include_parties=False,
            include_documents=False,
            max_entries=None,
        )
    except CourtListenerProviderError:
        pass

    assert FakeClient.headers["Authorization"] == "Token test-secret-token"
    assert [url for url in FakeClient.calls if "docket-entries" in url] == [
        "https://www.courtlistener.com/api/rest/v4/docket-entries/"
    ]


def test_courtlistener_solo_poll_materializes_legacy_cap_plus_one(
    tmp_path: Path,
    restore_courtlistener_poller: None,
) -> None:
    entries = [
        _entry(20_000 + index, str(index + 1), f"Entry {index + 1}")
        for index in range(501)
    ]
    provider = FakeCourtListenerProvider(
        [_listing(entries=entries, parties=[], pages_fetched=2)]
    )
    register_source_poller(CourtListenerDocketPoller(provider=provider), replace=True)
    source = _courtlistener_source(
        {"include_parties": False, "include_documents": False}
    )
    source["config"].pop("max_entries_per_poll")

    project = Project.create(tmp_path / "courtlistener-unbounded.frisket", name="Court")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=source,
                idempotency_key="source_courtlistener@sha256:legacy-cap-plus-one",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        assert provider.calls[0]["max_entries"] is None
        sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        item_ids = [
            str(row["source_item_id"])
            for row in _rows(project, sheet_id)
            if row["item_type"] == "docket_entry"
        ]
        assert len(item_ids) == 501
        assert len(set(item_ids)) == 501
        run_ref = next(
            output.ref for output in result.outputs if output.kind == "source_run"
        )
        source_run = project.db.execute(
            "SELECT cursor_after FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        assert source_run is not None
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["entry_count"] == 501
        assert cursor["truncated"] is False
    finally:
        project.close()


def test_courtlistener_docket_source_poll_materializes_rows_and_dedupes(
    tmp_path: Path,
    restore_courtlistener_poller: None,
) -> None:
    first_documents = [_document(7001, "1", "Complaint PDF with injunction exhibit")]
    first_entries = [
        _entry(
            9001,
            "1",
            "Complaint seeking emergency injunction",
            recap_documents=first_documents,
        ),
        _entry(9002, "2", "Notice of appearance"),
    ]
    second_documents = [
        *first_documents,
        _document(7002, "3", "Settlement conference notice PDF"),
    ]
    second_entries = [
        *first_entries,
        _entry(
            9003,
            "3",
            "Settlement conference scheduled",
            date_filed="2026-06-22",
            recap_documents=second_documents[1:],
        ),
    ]
    third_entries = [
        first_entries[0],
        _entry(9002, "2", "Notice of appearance corrected"),
        second_entries[2],
    ]
    provider = FakeCourtListenerProvider(
        [
            _listing(entries=first_entries),
            _listing(entries=second_entries),
            _listing(entries=third_entries),
        ]
    )
    register_source_poller(CourtListenerDocketPoller(provider=provider), replace=True)

    project = Project.create(tmp_path / "courtlistener.frisket", name="CourtListener")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(source=_courtlistener_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        assert first.receipt_id is not None
        assert provider.calls == [
            {
                "docket_id": DOCKET_ID,
                "docket_url": DOCKET_URL,
                "include_parties": True,
                "include_documents": True,
                "max_entries": 10,
            }
        ]
        source_id = next(
            output.ref["source_id"]
            for output in first.outputs
            if output.kind == "source"
        )
        source_run_id = next(
            output.ref["source_run_id"]
            for output in first.outputs
            if output.kind == "source_run"
        )
        sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None

        rows = _rows(project, sheet_id)
        assert [row["item_type"] for row in rows] == [
            "docket_snapshot",
            "party",
            "party",
            "docket_entry",
            "docket_entry",
            "recap_document",
        ]
        snapshot = rows[0]
        assert snapshot["source_id"] == source_id
        assert snapshot["source_run_id"] == source_run_id
        assert snapshot["source_item_id"] == f"courtlistener:docket:{DOCKET_ID}"
        assert snapshot["docket_id"] == int(DOCKET_ID)
        assert snapshot["case_name"] == "SEC v. Acme Holdings"
        assert snapshot["court_id"] == "nysd"
        assert snapshot["source_url"] == DOCKET_URL

        assert rows[1]["source_item_id"] == f"courtlistener:party:{DOCKET_ID}:10"
        assert rows[1]["party_name"] == "SEC"
        assert rows[1]["party_type"] == "plaintiff"
        assert rows[2]["source_item_id"] == f"courtlistener:party:{DOCKET_ID}:11"
        assert rows[3]["entry_number"] == "1"
        assert rows[3]["matched_keywords"] == ["injunction"]
        assert rows[4]["matched_keywords"] == []
        document = rows[5]
        assert document["entry_number"] == "1"
        assert document["document_url"].endswith(f"/{DOCKET_ID}/sec-v-acme/1/")
        assert document["recap_available"] is True
        assert document["matched_keywords"] == ["injunction"]
        assert "media" not in document
        assert _table_count(project, "source_items") == 6
        assert _table_count(project, "blobs") == 0
        assert "agency" not in _columns(project, sheet_id)

        first_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (source_run_id,),
        ).fetchone()
        assert first_run is not None
        assert first_run["status"] == "ok"
        assert first_run["new_rows"] == 6
        assert first_run["warning_count"] == 0
        first_cursor = json.loads(first_run["cursor_after"])
        assert first_cursor["schema_version"] == "frisket.courtlistener_cursor.v1"
        assert first_cursor["docket_id"] == DOCKET_ID
        assert first_cursor["entry_count"] == 2
        assert "courtlistener:docket_entry:9001" in first_cursor["item_hashes"]

        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "source.poll"
        assert receipt["status"] == "completed"
        assert receipt["warnings"] == []
        assert receipt["provider_use"][0]["provider"] == "courtlistener"
        assert receipt["provider_use"][0]["service"] == "fake-courtlistener"
        assert receipt["provider_use"][0]["docket_url_hash"].startswith("sha256:")
        assert DOCKET_URL not in json.dumps(receipt["provider_use"])

        second = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_courtlistener@sha256:second",
            ),
            project_id=PROJECT_ID,
        )
        assert second.status == "completed"
        second_run_id = next(
            output.ref["source_run_id"]
            for output in second.outputs
            if output.kind == "source_run"
        )
        second_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (second_run_id,),
        ).fetchone()
        assert second_run["new_rows"] == 2
        assert second_run["skipped_rows"] == 6
        assert second_run["changed_rows"] == 0
        assert second_run["revisions"] == 0
        rows = _rows(project, sheet_id)
        assert [row["source_item_id"] for row in rows[-2:]] == [
            "courtlistener:docket_entry:9003",
            f"courtlistener:recap_document:{DOCKET_ID}:7002",
        ]
        assert rows[-2]["matched_keywords"] == ["settlement"]
        assert _table_count(project, "source_items") == 8
        assert _table_count(project, "blobs") == 0

        third = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_courtlistener@sha256:third",
            ),
            project_id=PROJECT_ID,
        )
        assert third.status == "completed"
        third_run_id = next(
            output.ref["source_run_id"]
            for output in third.outputs
            if output.kind == "source_run"
        )
        third_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (third_run_id,),
        ).fetchone()
        assert third_run["new_rows"] == 0
        assert third_run["skipped_rows"] == 7
        assert third_run["changed_rows"] == 1
        assert third_run["revisions"] == 1
        third_receipt = _receipt(project, third.receipt_id)
        assert third_receipt["warnings"] == [
            "courtlistener_docket existing item changed: "
            "courtlistener:docket_entry:9002"
        ]
        rows = _rows(project, sheet_id)
        assert rows[-1]["source_item_id"] == "courtlistener:docket_entry:9002"
        assert rows[-1]["_revises"] == "courtlistener:docket_entry:9002"
        assert rows[-1]["_revision"] == 1
        assert rows[-1]["description"] == "Notice of appearance corrected"
    finally:
        project.close()


def test_courtlistener_entry_cap_retains_all_documents_from_kept_entry(
    tmp_path: Path,
    restore_courtlistener_poller: None,
) -> None:
    retained_documents = [
        _document(7001, "1", "First retained PDF"),
        _document(7002, "1-1", "Second retained PDF"),
    ]
    excluded_document = _document(7003, "2", "Excluded PDF")
    provider = FakeCourtListenerProvider(
        [
            _listing(
                entries=[
                    _entry(
                        9001,
                        "1",
                        "Retained entry",
                        recap_documents=retained_documents,
                    ),
                    _entry(
                        9002,
                        "2",
                        "Excluded entry",
                        recap_documents=[excluded_document],
                    ),
                ],
                parties=[],
            )
        ]
    )
    register_source_poller(CourtListenerDocketPoller(provider=provider), replace=True)

    project = Project.create(tmp_path / "courtlistener-entry-cap.frisket", name="Court")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=_courtlistener_source(
                    {
                        "include_parties": False,
                        "max_entries_per_poll": 1,
                    }
                ),
                idempotency_key="source_courtlistener@sha256:entry-cap-documents",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        rows = _rows(project, sheet_id)
        assert [row["source_item_id"] for row in rows] == [
            f"courtlistener:docket:{DOCKET_ID}",
            "courtlistener:docket_entry:9001",
            f"courtlistener:recap_document:{DOCKET_ID}:7001",
            f"courtlistener:recap_document:{DOCKET_ID}:7002",
        ]
        assert "courtlistener:docket_entry:9002" not in {
            row["source_item_id"] for row in rows
        }
        assert f"courtlistener:recap_document:{DOCKET_ID}:7003" not in {
            row["source_item_id"] for row in rows
        }
    finally:
        project.close()


def test_courtlistener_validation_caps_and_provider_failure_redaction(
    tmp_path: Path,
    restore_courtlistener_poller: None,
) -> None:
    assert get_source_poller(COURTLISTENER_DOCKET_KIND) is not None

    invalid_project = Project.create(tmp_path / "invalid.frisket", name="Invalid")
    try:
        invalid = run_action_spec(
            invalid_project,
            _source_poll_action(
                source=_courtlistener_source({"api_token": "SECRET"}),
                idempotency_key="source_courtlistener@sha256:invalid",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid.status == "failed"
        assert invalid.errors[0].code == "unsupported_source_config"
        assert "token" in invalid.errors[0].message
        assert "SECRET" not in invalid.errors[0].message
        assert _table_count(invalid_project, "sources") == 0
        assert _table_count(invalid_project, "source_runs") == 0

        bad_schema = run_action_spec(
            invalid_project,
            _source_poll_action(
                source=_courtlistener_source({"schema_version": "frisket.old"}),
                idempotency_key="source_courtlistener@sha256:bad-schema",
            ),
            project_id=PROJECT_ID,
        )
        assert bad_schema.status == "failed"
        assert bad_schema.errors[0].code == "unsupported_source_config"
        assert "schema_version" in bad_schema.errors[0].message
        assert _table_count(invalid_project, "sources") == 0
        assert _table_count(invalid_project, "source_runs") == 0
    finally:
        invalid_project.close()

    capped_provider = FakeCourtListenerProvider(
        [
            _listing(
                entries=[
                    _entry(9001, "1", "First"),
                    _entry(9002, "2", "Second"),
                ],
                documents=[
                    {
                        "id": 7001,
                        "document_number": None,
                        "description": "First PDF",
                        "filepath_local": "https://files.example/leak.pdf",
                        "absolute_url": "",
                        "is_available": True,
                    },
                    _document(7002, "2", "Second PDF"),
                ],
                parties=[],
            )
        ]
    )
    register_source_poller(
        CourtListenerDocketPoller(provider=capped_provider), replace=True
    )
    capped_project = Project.create(tmp_path / "capped.frisket", name="Capped")
    try:
        capped = run_action_spec(
            capped_project,
            _source_poll_action(
                source=_courtlistener_source(
                    {
                        "docket_url": None,
                        "docket_id": DOCKET_ID,
                        "include_parties": False,
                        "max_entries_per_poll": 1,
                    }
                ),
                idempotency_key="source_courtlistener@sha256:capped",
            ),
            project_id=PROJECT_ID,
        )
        assert capped.status == "completed"
        assert capped_provider.calls[0]["docket_id"] == DOCKET_ID
        assert capped_provider.calls[0]["max_entries"] == 1
        sheet_id = next(
            output.sheet_id for output in capped.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        rows = _rows(capped_project, sheet_id)
        assert [row["item_type"] for row in rows] == [
            "docket_snapshot",
            "docket_entry",
            "recap_document",
        ]
        assert rows[2]["recap_available"] is False
        assert "document_url" not in rows[2]
        assert "media" not in rows[2]
        run_ref = next(
            output.ref for output in capped.outputs if output.kind == "source_run"
        )
        source_run = capped_project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["max_entries_per_poll"] == 1
        assert cursor["truncated"] is True
        assert _receipt(capped_project, capped.receipt_id)["warnings"] == [
            "courtlistener_docket entry cap reached; limited to 1 entries"
        ]
    finally:
        capped_project.close()

    failing_provider = FakeCourtListenerProvider(
        [
            RuntimeError(
                "quota failed api_key=SECRET token=SECRET authorization: Bearer abc123"
            )
        ]
    )
    register_source_poller(
        CourtListenerDocketPoller(provider=failing_provider), replace=True
    )
    failed_project = Project.create(tmp_path / "failed.frisket", name="Failed")
    try:
        source_id = SourceStore(failed_project).add_source(
            name="Failing docket",
            kind=COURTLISTENER_DOCKET_KIND,
            url=DOCKET_URL,
            config={"docket_id": DOCKET_ID},
        )
        failed = run_action_spec(
            failed_project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_courtlistener@sha256:provider-failed",
            ),
            project_id=PROJECT_ID,
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "source_poll_failed"
        assert "SECRET" not in failed.errors[0].message
        assert "abc123" not in failed.errors[0].message
        source_run = SourceStore(failed_project).source_runs(source_id, limit=1)[0]
        assert source_run["status"] == "error"
        assert "SECRET" not in source_run["error"]
        assert "abc123" not in source_run["error"]
        receipt = _receipt(failed_project, failed.receipt_id)
        assert receipt["status"] == "failed"
        assert "SECRET" not in receipt["errors"][0]["message"]
        assert "abc123" not in receipt["errors"][0]["message"]
    finally:
        failed_project.close()


def test_courtlistener_missing_token_returns_disabled_source_state(
    tmp_path: Path,
    restore_courtlistener_poller: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    register_source_poller(CourtListenerDocketPoller(), replace=True)

    project = Project.create(tmp_path / "missing-token.frisket", name="Missing Token")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=_courtlistener_source(),
                idempotency_key="source_courtlistener@sha256:missing-token",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "failed"
        assert result.receipt_id is not None
        assert result.errors[0].code == "source_poll_failed"
        assert "disabled" in result.errors[0].message
        assert "COURTLISTENER_API_TOKEN" in result.errors[0].message

        source = project.db.execute(
            "SELECT * FROM sources WHERE kind=?", (COURTLISTENER_DOCKET_KIND,)
        ).fetchone()
        assert source is not None
        source_run = SourceStore(project).source_runs(int(source["id"]), limit=1)[0]
        assert source_run["status"] == "error"
        assert "disabled" in source_run["error"]
        assert "COURTLISTENER_API_TOKEN" in source_run["error"]
        assert "token=" not in source_run["error"].lower()
    finally:
        project.close()


def test_courtlistener_scheduled_source_dispatches_generic_source_poll(
    tmp_path: Path,
    restore_courtlistener_poller: None,
) -> None:
    provider = FakeCourtListenerProvider(
        [_listing(entries=[_entry(9001, "1", "Scheduled docket entry")])]
    )
    register_source_poller(CourtListenerDocketPoller(provider=provider), replace=True)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = Project.create(workspace / "news.frisket", name="news")
    try:
        courtlistener_source_id = SourceStore(project).add_source(
            name="CourtListener",
            kind=COURTLISTENER_DOCKET_KIND,
            url=DOCKET_URL,
            schedule="@hourly",
            config={"docket_id": DOCKET_ID, "max_entries_per_poll": 1},
        )
        SourceStore(project).add_source(
            name="Unsupported API",
            kind="api",
            url="https://example.com/api",
            schedule="@hourly",
        )
    finally:
        project.close()

    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        jobs = enqueue_due_source_polls(workspace_root=workspace, queue=queue)
        assert len(jobs) == 1
        assert jobs[0]["source_id"] == courtlistener_source_id
        job_id = int(jobs[0]["job_id"])

        def unexpected_rss_fetch(_url: str) -> str:
            raise AssertionError("courtlistener_docket must not use RSS fetch")

        registry = default_registry()
        register_source_poll_handler(
            registry,
            workspace_root=workspace,
            queue=queue,
            fetch=unexpected_rss_fetch,
        )
        assert Worker(queue, registry).run_once()
        job = queue.get(job_id)
        assert job.status == "done"
        assert job.result["action_kind"] == "source.poll"
        assert job.result["source_id"] == courtlistener_source_id
        assert job.result["new_rows"] == 4
        assert provider.calls[0]["max_entries"] == 1

        reopened = Project(workspace / "news.frisket")
        try:
            runs = [
                dict(row)
                for row in SourceStore(reopened).source_runs(courtlistener_source_id)
            ]
            assert len(runs) == 1
            assert runs[0]["status"] == "ok"
            receipt = _receipt(reopened, job.result["receipt_id"])
            assert receipt["action_kind"] == "source.poll"
            assert receipt["provider_use"][0]["provider"] == "courtlistener"
        finally:
            reopened.close()
    finally:
        queue.close()
