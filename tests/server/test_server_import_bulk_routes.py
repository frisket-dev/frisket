from __future__ import annotations

import asyncio
import hashlib
import errno
import io
import json
import os
import stat
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from frisket.actions.types import EmailInput
from frisket.server.app import create_app
from frisket.server.services import import_bulk as import_bulk_service
from frisket.server.services import (
    import_bulk_execute,
    import_bulk_plan,
    import_bulk_sources,
)
from frisket.server.services.import_bulk import BulkImportLimits
from frisket.server.services.import_files import ImportFilesUploadResponse


def _project(client: TestClient, name: str = "Bulk import") -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _bulk_plan(
    client: TestClient,
    pid: str,
    uploads: list[tuple[str, bytes, str, str]],
    *,
    expand_archive: bool,
):
    parts: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]] = []
    for filename, content, content_type, logical_path in uploads:
        parts.extend(
            [
                ("files", (filename, content, content_type)),
                ("logical_paths", (None, logical_path)),
            ]
        )
    parts.append(("expand_archive", (None, str(expand_archive).lower())))
    return client.post(f"/api/projects/{pid}/import/bulk/plan", files=parts)


def _multipart_bulk_plan_body(
    *, boundary: str, logical_path: str, content: bytes = b"x"
) -> bytes:
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="files"; filename="tiny.txt"\r\n',
            b"Content-Type: text/plain\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="logical_paths"\r\n\r\n',
            logical_path.encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="expand_archive"\r\n\r\n',
            b"false\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )


def _raw_chunked_bulk_plan(
    app: Any, *, pid: str, body: bytes, boundary: str
) -> tuple[int, int]:
    """Send a multipart request in real ASGI chunks without Content-Length."""

    async def invoke() -> tuple[int, int]:
        chunks = [body[:256], body[256:800], body[800:]]
        chunks = [chunk for chunk in chunks if chunk]
        body_reads = 0
        status = 0

        async def receive() -> dict[str, Any]:
            nonlocal body_reads
            if not chunks:
                return {"type": "http.disconnect"}
            body_reads += 1
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])

        path = f"/api/projects/{pid}/import/bulk/plan"
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (
                        b"content-type",
                        f"multipart/form-data; boundary={boundary}".encode(),
                    ),
                    (b"transfer-encoding", b"chunked"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )
        return status, body_reads

    return asyncio.run(invoke())


def _zip_bytes(members: dict[str, bytes] | list[tuple[str, bytes]]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        items = members.items() if isinstance(members, dict) else members
        for path, content in items:
            archive.writestr(path, content)
    return payload.getvalue()


def _zip_with_unix_member(name: str, mode: int, content: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        member = zipfile.ZipInfo(name)
        member.create_system = 3
        member.external_attr = mode << 16
        archive.writestr(member, content)
    return payload.getvalue()


def _rewrite_zip_member_name(
    payload: bytes, original: bytes, replacement: bytes
) -> bytes:
    assert len(original) == len(replacement)
    assert payload.count(original) == 2  # local header and central directory
    return payload.replace(original, replacement)


def _mark_zip_encrypted(payload: bytes) -> bytes:
    encrypted = bytearray(payload)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        assert encrypted.count(signature) == 1
        header = encrypted.index(signature)
        flags = struct.unpack_from("<H", encrypted, header + flag_offset)[0]
        struct.pack_into("<H", encrypted, header + flag_offset, flags | 1)
    return bytes(encrypted)


def _bulk_staging_directories(client: TestClient, pid: str) -> list[Path]:
    root = client.app.state.workspace.get(pid).path / ".bulk_import_staging"
    return [path for path in root.iterdir() if path.is_dir()]


def _sheet_data(client: TestClient, pid: str, sheet_id: int) -> dict:
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data")
    assert data.status_code == 200, data.text
    return data.json()


def _sheet_rows(client: TestClient, pid: str, sheet_id: int) -> list[dict[str, object]]:
    data = _sheet_data(client, pid, sheet_id)
    names = {str(column["id"]): column["name"] for column in data["columns"]}
    return [
        {names[str(column_id)]: value for column_id, value in row["cells"].items()}
        for row in data["rows"]
    ]


def _compatible_csv_uploads() -> list[tuple[str, bytes, str, str]]:
    # Deliberately send b before a: the normalized logical path determines both the
    # plan order and the combined sheet's primary column order.
    return [
        ("b.csv", b"score,name\n2,Bob\n", "text/csv", "folder/b.csv"),
        ("a.csv", b"name,score\nAda,1\n", "text/csv", "folder/a.csv"),
    ]


def _csv_question(plan: dict) -> dict:
    assert len(plan["questions"]) == 1
    question = plan["questions"][0]
    assert question["kind"] == "csv_combine"
    assert question["default"] == "combine"
    assert question["logical_paths"] == ["folder/a.csv", "folder/b.csv"]
    return question


def _execute_bulk(
    client: TestClient,
    pid: str,
    plan: dict,
    decisions: dict[str, str] | None = None,
):
    return client.post(
        f"/api/projects/{pid}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": decisions or {}},
    )


def test_solo_bulk_csv_exceeds_legacy_file_and_row_caps_without_truncation(
    tmp_path: Path,
) -> None:
    row_count = 10_001
    padding = "x" * 540
    raw = (
        "id,payload\n"
        + "".join(f"{index},{padding}{index}\n" for index in range(row_count))
    ).encode()
    assert len(raw) > 5 * 1024 * 1024

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Large CSV")
    planned = _bulk_plan(
        client,
        pid,
        [("records.csv", raw, "text/csv", "records.csv")],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text

    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["failed"] == []
    assert result["created"][0]["rows"] == row_count
    data = _sheet_data(client, pid, result["created"][0]["sheet_id"])
    assert data["total"] == row_count
    assert {column["name"]: column["type"] for column in data["columns"]} == {
        "id": "integer",
        "payload": "text",
    }


def test_bulk_plan_reads_uploads_in_bounded_chunks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = b"name,notes\n" + b"".join(
        f"person-{index},".encode() + (b"x" * 12_000) + b"\n" for index in range(200)
    )
    assert len(raw) > 2 * 1024 * 1024
    original_read = UploadFile.read
    requested_sizes: list[int] = []

    async def recording_read(self: UploadFile, size: int = -1) -> bytes:
        requested_sizes.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Chunked bulk staging")

    planned = _bulk_plan(
        client,
        pid,
        [("records.csv", raw, "text/csv", "records.csv")],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    assert len(requested_sizes) >= 3
    assert all(0 < size <= 1024 * 1024 for size in requested_sizes)


def test_bulk_request_limit_counts_multipart_metadata_before_staging(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "workspace",
        bulk_import_limits=BulkImportLimits(max_request_bytes=512),
    )
    client = TestClient(app)
    pid = _project(client, "Request envelope quota")

    response = _bulk_plan(
        client,
        pid,
        [("tiny.txt", b"x", "text/plain", f"folder/{'m' * 1_024}.txt")],
        expand_archive=False,
    )

    assert response.status_code == 413
    assert not (app.state.workspace.get(pid).path / ".bulk_import_staging").exists()


def test_bulk_request_limit_counts_chunked_body_without_content_length(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "workspace",
        bulk_import_limits=BulkImportLimits(max_request_bytes=512),
    )
    client = TestClient(app)
    pid = _project(client, "Chunked request envelope quota")
    boundary = "frisket-bulk-envelope-boundary"
    body = _multipart_bulk_plan_body(
        boundary=boundary,
        logical_path=f"folder/{'m' * 1_024}.txt",
    )
    assert len(body) > 800

    status, body_reads = _raw_chunked_bulk_plan(
        app, pid=pid, body=body, boundary=boundary
    )

    assert status == 413
    assert body_reads == 2, "request must stop reading as soon as the limit is crossed"
    assert not (app.state.workspace.get(pid).path / ".bulk_import_staging").exists()


def test_bulk_request_limit_defaults_to_unlimited_for_legacy_requests(
    tmp_path: Path,
) -> None:
    limits = BulkImportLimits()
    assert limits.max_request_bytes is None
    app = create_app(tmp_path / "workspace", bulk_import_limits=limits)
    client = TestClient(app)
    pid = _project(client, "Unlimited request envelope")

    response = _bulk_plan(
        client,
        pid,
        [("tiny.txt", b"x", "text/plain", f"folder/{'m' * 1_024}.txt")],
        expand_archive=False,
    )

    assert response.status_code == 200, response.text


def test_bulk_upload_byte_limit_counts_aggregate_file_content(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            bulk_import_limits=BulkImportLimits(max_upload_bytes=10),
        )
    )

    accepted_pid = _project(client, "Exact content quota")
    accepted = _bulk_plan(
        client,
        accepted_pid,
        [("exact.txt", b"1234567890", "text/plain", "exact.txt")],
        expand_archive=False,
    )
    assert accepted.status_code == 200, accepted.text

    pid = _project(client, "Cumulative quota")
    response = _bulk_plan(
        client,
        pid,
        [
            ("a.txt", b"123456", "text/plain", "a.txt"),
            ("b.txt", b"abcdef", "text/plain", "b.txt"),
        ],
        expand_archive=False,
    )
    assert response.status_code == 413
    root = client.app.state.workspace.get(pid).path / ".bulk_import_staging"
    assert not [path for path in root.iterdir() if path.is_dir()]


def test_bulk_draft_mkdir_failures_leave_no_partial_draft(
    tmp_path: Path, monkeypatch
) -> None:
    original_mkdir = Path.mkdir
    for seam in ("draft", "files"):
        app = create_app(tmp_path / seam / "workspace")
        client = TestClient(app, raise_server_exceptions=False)
        pid = _project(client, f"mkdir {seam}")

        def failing_mkdir(path: Path, *args, **kwargs):
            should_fail = (
                path.name.startswith(".draft-")
                if seam == "draft"
                else path.name == "files" and path.parent.name.startswith(".draft-")
            )
            if should_fail:
                raise OSError(errno.ENOSPC, "injected no space")
            return original_mkdir(path, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "mkdir", failing_mkdir)
            response = _bulk_plan(
                client,
                pid,
                [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
                expand_archive=False,
            )
        assert response.status_code == 500
        root = app.state.workspace.get(pid).path / ".bulk_import_staging"
        assert not [path for path in root.iterdir() if path.name.startswith(".draft-")]


def test_post_claim_database_failure_cleans_claimed_plan(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app, raise_server_exceptions=False)
    pid = _project(client, "Claim cleanup")
    planned = _bulk_plan(
        client,
        pid,
        [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
        expand_archive=False,
    ).json()
    project = app.state.workspace.get(pid)

    def fail_insert(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected database read failure")

    monkeypatch.setattr(import_bulk_execute, "run_scanned_csv_import", fail_insert)
    response = _execute_bulk(client, pid, planned)
    assert response.status_code == 500
    assert not (project.path / ".bulk_import_staging" / planned["plan_id"]).exists()


def test_bulk_service_uses_its_injected_clock_for_plan_expiry(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Injected bulk clock")
    now = [1_000.0]
    service = import_bulk_service.ImportBulkService(
        client.app.state.workspace, clock=lambda: now[0]
    )

    ready = asyncio.run(service.plan(pid, uploads=[], expand_archive=False))
    assert service.execute(pid, ready["plan_id"], decisions={})["message"] == (
        "No importable files found."
    )

    expired = asyncio.run(service.plan(pid, uploads=[], expand_archive=False))
    now[0] += 86_400
    with pytest.raises(import_bulk_service.ImportBulkRouteError) as exc:
        service.execute(pid, expired["plan_id"], decisions={})
    assert exc.value.status_code == 404
    plan = (
        client.app.state.workspace.get(pid).path
        / ".bulk_import_staging"
        / expired["plan_id"]
    )
    assert not plan.exists()


def test_bulk_purge_does_not_follow_symlink_children(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Safe purge")
    project = client.app.state.workspace.get(pid)
    root = project.path / ".bulk_import_staging"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep")
    (root / ".draft-attacker").symlink_to(victim, target_is_directory=True)
    response = _bulk_plan(
        client,
        pid,
        [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
        expand_archive=False,
    )
    assert response.status_code == 200
    assert marker.read_text() == "keep"


def test_bulk_import_works_without_dir_fd_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows verifies staged files without POSIX relative-open support."""

    monkeypatch.setattr(import_bulk_sources.os, "supports_dir_fd", set())
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Portable staged source")
    planned = _bulk_plan(
        client,
        pid,
        [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    assert executed.json()["failed"] == []


def test_bulk_combined_csv_aligns_reordered_columns_and_widens_numbers(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "CSV widening")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("b.csv", b"amount,name\n2.5,Bob\n", "text/csv", "folder/b.csv"),
            ("a.csv", b"name,amount\nAda,1\n", "text/csv", "folder/a.csv"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = _csv_question(plan)

    executed = _execute_bulk(client, pid, plan, {question["id"]: "combine"})
    assert executed.status_code == 200, executed.text
    created = executed.json()["created"][0]
    assert _sheet_rows(client, pid, created["sheet_id"]) == [
        {"name": "Ada", "amount": 1.0, "source_file": "folder/a.csv"},
        {"name": "Bob", "amount": 2.5, "source_file": "folder/b.csv"},
    ]
    data = _sheet_data(client, pid, created["sheet_id"])
    assert {column["name"]: column["type"] for column in data["columns"]} == {
        "name": "text",
        "amount": "number",
        "source_file": "text",
    }


@pytest.mark.parametrize(
    "malformed",
    [
        b"name,name\nSECRET-ROW-CONTENT,Lovelace\n",
        b"name,\nSECRET-ROW-CONTENT,Lovelace\n",
        b'"unterminated\nSECRET-ROW-CONTENT\n',
    ],
    ids=["duplicate", "empty", "unparseable"],
)
def test_bulk_plan_keeps_valid_sibling_when_csv_header_is_unusable(
    tmp_path: Path,
    malformed: bytes,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Unusable CSV headers")

    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", malformed, "text/csv", "folder/a.csv"),
            ("b.csv", b"name\nGrace\n", "text/csv", "folder/b.csv"),
        ],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["questions"] == []
    assert [output["logical_paths"] for output in plan["proposed_outputs"]] == [
        ["folder/a.csv"],
        ["folder/b.csv"],
    ]
    assert all(
        set(output) == {"id", "kind", "sheet_name", "logical_paths"}
        for output in plan["proposed_outputs"]
    )
    assert "SECRET-ROW-CONTENT" not in planned.text

    executed = _execute_bulk(client, pid, plan)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert [(item["sheet_name"], item["rows"]) for item in result["created"]] == [
        ("b", 1)
    ]
    assert _sheet_rows(client, pid, result["created"][0]["sheet_id"]) == [
        {"name": "Grace"}
    ]
    assert [failure["logical_paths"] for failure in result["failed"]] == [
        ["folder/a.csv"]
    ]
    assert "SECRET-ROW-CONTENT" not in executed.text
    assert _bulk_staging_directories(client, pid) == []


def test_bulk_execute_reports_deterministic_inert_csv_failure_details(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Deterministic CSV failures")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("later.csv", b"name,name\nAda,Lovelace\n", "text/csv", "inbox/later.csv"),
            ("valid.csv", b"name\nGrace\n", "text/csv", "inbox/valid.csv"),
            (
                "first.csv",
                b"name,name\nLinus,Torvalds\n",
                "text/csv",
                "inbox/first.csv",
            ),
        ],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert [output["logical_paths"] for output in plan["proposed_outputs"]] == [
        ["inbox/first.csv"],
        ["inbox/later.csv"],
        ["inbox/valid.csv"],
    ]

    executed = _execute_bulk(client, pid, plan)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert [
        {
            "kind": failure["kind"],
            "logical_paths": failure["logical_paths"],
            "error": failure["error"],
        }
        for failure in result["failed"]
    ] == [
        {
            "kind": "csv_group",
            "logical_paths": ["inbox/first.csv"],
            "error": "CSV header is invalid",
        },
        {
            "kind": "csv_group",
            "logical_paths": ["inbox/later.csv"],
            "error": "CSV header is invalid",
        },
    ]
    assert result["created"][0]["logical_paths"] == ["inbox/valid.csv"]
    assert _bulk_staging_directories(client, pid) == []


def test_bulk_execute_bounds_failure_details_without_losing_aggregate_accounting(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Bounded CSV failures")
    malformed = [
        (
            f"broken-{index:02}.csv",
            b"name,name\ninvalid,header\n",
            "text/csv",
            f"inbox/broken-{index:02}.csv",
        )
        for index in reversed(range(23))
    ]
    planned = _bulk_plan(
        client,
        pid,
        [
            *malformed,
            ("valid.csv", b"name\nGrace\n", "text/csv", "inbox/valid.csv"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()

    executed = _execute_bulk(client, pid, plan)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    expected_paths = [f"inbox/broken-{index:02}.csv" for index in range(23)]
    assert len(result["failed"]) <= 20
    assert result["failed_omitted"] == 3
    assert [failure["logical_paths"] for failure in result["failed"]] == [
        [path] for path in expected_paths[:20]
    ]
    assert all(failure["kind"] == "csv_group" for failure in result["failed"])
    assert all(
        failure["error"] == "CSV header is invalid" for failure in result["failed"]
    )
    assert result["created"][0]["logical_paths"] == ["inbox/valid.csv"]
    assert result["message"] == "Imported 1 of 24 files."
    assert _bulk_staging_directories(client, pid) == []


def test_bulk_nonmatching_csvs_create_independent_outputs(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Nonmatching CSVs")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("b.csv", b"score\n2\n", "text/csv", "folder/b.csv"),
            ("a.csv", b"name\nAda\n", "text/csv", "folder/a.csv"),
        ],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["questions"] == []
    assert [output["logical_paths"] for output in plan["proposed_outputs"]] == [
        ["folder/a.csv"],
        ["folder/b.csv"],
    ]

    executed = _execute_bulk(client, pid, plan)
    assert executed.status_code == 200, executed.text
    created = executed.json()["created"]
    assert [(item["sheet_name"], item["rows"]) for item in created] == [
        ("a", 1),
        ("b", 1),
    ]
    assert _sheet_rows(client, pid, created[0]["sheet_id"]) == [{"name": "Ada"}]
    assert _sheet_rows(client, pid, created[1]["sheet_id"]) == [{"score": 2}]


def test_bulk_csv_publications_keep_one_reversible_op_and_receipt_per_output(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Bulk CSV publication")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", b"name\nAda\n", "text/csv", "folder/a.csv"),
            ("b.csv", b"score\n2\n", "text/csv", "folder/b.csv"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text

    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    created = executed.json()["created"]
    assert len(created) == 2

    project = client.app.state.workspace.get(pid)
    ops = project.history()
    receipts = project.db.execute(
        "SELECT action_kind, status, body FROM receipts ORDER BY id"
    ).fetchall()
    assert len(ops) == len(receipts) == len(created)
    created_sheet_ids = {item["sheet_id"] for item in created}
    op_ids = {int(op["id"]) for op in ops}
    for op in ops:
        assert op["kind"] == "import.csv"
        assert op["barrier"] == 0
        spec = json.loads(op["spec"])
        assert spec["action_id"] == "import.csv"
        assert spec["scope"] == {"kind": "project"}
        assert spec["reads"][0]["kind"] == "local_file_read"
        undo = json.loads(op["undo_info"])
        assert set(undo) == {"created_sheets"}
        assert len(undo["created_sheets"]) == 1
        assert undo["created_sheets"][0] in created_sheet_ids

    receipt_op_ids = set()
    for row in receipts:
        assert row["action_kind"] == "import.csv"
        assert row["status"] == "completed"
        receipt = json.loads(row["body"])
        assert receipt["action_kind"] == "import.csv"
        assert receipt["status"] == "completed"
        assert len(receipt["op_ids"]) == 1
        assert receipt["op_ids"][0] in op_ids
        receipt_op_ids.add(receipt["op_ids"][0])
    assert receipt_op_ids == op_ids

    last_op_id = int(ops[-1]["id"])
    assert project.undo() == last_op_id
    assert len(client.get(f"/api/projects/{pid}/sheets").json()) == 1
    assert project.redo() == last_op_id
    assert {
        sheet["id"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    } == (created_sheet_ids)


def test_bulk_combined_csv_uses_noncolliding_source_file_provenance(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Provenance collision")
    planned = _bulk_plan(
        client,
        pid,
        [
            (
                "b.csv",
                b"name,source_file\nBob,original-b\n",
                "text/csv",
                "folder/b.csv",
            ),
            (
                "a.csv",
                b"source_file,name\noriginal-a,Ada\n",
                "text/csv",
                "folder/a.csv",
            ),
        ],
        expand_archive=False,
    )

    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = _csv_question(plan)
    executed = _execute_bulk(client, pid, plan, {question["id"]: "combine"})

    assert executed.status_code == 200, executed.text
    created = executed.json()["created"][0]
    assert _sheet_rows(client, pid, created["sheet_id"]) == [
        {
            "source_file": "original-a",
            "name": "Ada",
            "source_file_2": "folder/a.csv",
        },
        {
            "source_file": "original-b",
            "name": "Bob",
            "source_file_2": "folder/b.csv",
        },
    ]


def _csv_with_late_malformed_record() -> bytes:
    valid = "".join(f"person-{index},{index}\n" for index in range(80))
    return f'name,amount\n{valid}"unterminated,81\n'.encode()


def test_bulk_combined_csv_late_parse_failure_publishes_no_sheet(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Atomic combined CSV")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", b"name,amount\nAda,1\n", "text/csv", "folder/a.csv"),
            (
                "b.csv",
                _csv_with_late_malformed_record(),
                "text/csv",
                "folder/b.csv",
            ),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = _csv_question(plan)

    executed = _execute_bulk(client, pid, plan, {question["id"]: "combine"})
    assert executed.status_code == 200, executed.text
    assert executed.json()["created"] == []
    assert len(executed.json()["failed"]) == 1
    assert client.get(f"/api/projects/{pid}/sheets").json() == []


def test_bulk_reports_committed_sheet_after_later_fatal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Visible partial import")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", b"name\nAda\n", "text/csv", "a.csv"),
            ("b.txt", b"attachment", "text/plain", "b.txt"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text

    def fail_files(*args, **kwargs):
        raise import_bulk_service.ImportBulkRouteError(500, "storage unavailable")

    monkeypatch.setattr(import_bulk_execute.BulkExecutor, "_files", fail_files)
    response = _execute_bulk(client, pid, planned.json(), {})
    assert response.status_code == 200, response.text
    result = response.json()
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert len(sheets) == 1
    assert result["first_sheet_id"] == sheets[0]["id"]
    assert result["created"][0]["logical_paths"] == ["a.csv"]
    assert result["failed"][0]["logical_paths"] == ["b.txt"]
    assert _sheet_rows(client, pid, sheets[0]["id"]) == [{"name": "Ada"}]
    assert _bulk_staging_directories(client, pid) == []


def test_bulk_separate_csv_keeps_earlier_success_when_later_output_fails(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Per-output CSV atomicity")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", b"name,amount\nAda,1\n", "text/csv", "folder/a.csv"),
            (
                "b.csv",
                _csv_with_late_malformed_record(),
                "text/csv",
                "folder/b.csv",
            ),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = _csv_question(plan)

    executed = _execute_bulk(client, pid, plan, {question["id"]: "separate"})
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert [(item["sheet_name"], item["rows"]) for item in result["created"]] == [
        ("a", 1)
    ]
    assert len(result["failed"]) == 1
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert [sheet["name"] for sheet in sheets] == ["a"]
    assert _sheet_rows(client, pid, sheets[0]["id"]) == [{"name": "Ada", "amount": "1"}]


def test_bulk_separate_csv_reports_committed_child_after_later_fatal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Visible partial separate CSV")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("a.csv", b"name\nAda\n", "text/csv", "folder/a.csv"),
            ("b.csv", b"name\nBea\n", "text/csv", "folder/b.csv"),
            ("c.csv", b"name\nCam\n", "text/csv", "folder/c.csv"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = next(
        question for question in plan["questions"] if question["kind"] == "csv_combine"
    )
    original = import_bulk_execute.run_scanned_csv_import
    attempted: list[str] = []

    def fail_second_csv(*args, **kwargs):
        attempted.append(kwargs["sheet_name"])
        if len(attempted) == 2:
            raise import_bulk_service.ImportBulkRouteError(500, "storage unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(import_bulk_execute, "run_scanned_csv_import", fail_second_csv)
    response = _execute_bulk(client, pid, plan, {question["id"]: "separate"})

    assert response.status_code == 200, response.text
    result = response.json()
    assert attempted == ["a", "b"]
    assert [item["logical_paths"] for item in result["created"]] == [["folder/a.csv"]]
    assert [item["logical_paths"] for item in result["failed"]] == [
        ["folder/b.csv"],
        ["folder/c.csv"],
    ]
    assert result["failed"][0]["error"] == "storage unavailable"
    assert result["failed"][1]["error"] == (
        "Not attempted because bulk execution stopped after a server error."
    )
    assert result["message"] == (
        "Imported 1 of 3 files; execution stopped after a server error."
    )
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert [sheet["name"] for sheet in sheets] == ["a"]
    assert _bulk_staging_directories(client, pid) == []
    replay = _execute_bulk(client, pid, plan, {question["id"]: "separate"})
    assert replay.status_code == 404


def test_bulk_plan_is_non_mutating_deterministic_and_zip_equivalent(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)

    direct = _bulk_plan(client, pid, _compatible_csv_uploads(), expand_archive=False)
    assert direct.status_code == 200, direct.text
    direct_plan = direct.json()
    assert isinstance(direct_plan["plan_id"], str)
    assert client.get(f"/api/projects/{pid}/sheets").json() == []
    assert direct_plan["proposed_outputs"] == [
        {
            "id": direct_plan["proposed_outputs"][0]["id"],
            "kind": "csv_group",
            "sheet_name": "a",
            "logical_paths": ["folder/a.csv", "folder/b.csv"],
        }
    ]
    _csv_question(direct_plan)

    archive = _bulk_plan(
        client,
        pid,
        [
            (
                "selected.zip",
                _zip_bytes(
                    {
                        "folder/b.csv": b"score,name\n2,Bob\n",
                        "folder/a.csv": b"name,score\nAda,1\n",
                    }
                ),
                "application/zip",
                "selected.zip",
            )
        ],
        expand_archive=True,
    )
    assert archive.status_code == 200, archive.text
    archive_plan = archive.json()
    assert archive_plan["proposed_outputs"] == direct_plan["proposed_outputs"]
    assert {
        key: value for key, value in _csv_question(archive_plan).items() if key != "id"
    } == {
        key: value for key, value in _csv_question(direct_plan).items() if key != "id"
    }
    assert client.get(f"/api/projects/{pid}/sheets").json() == []


def test_bulk_execute_requires_exact_project_bound_decisions_and_combines_csvs(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    plan_response = _bulk_plan(
        client, pid, _compatible_csv_uploads(), expand_archive=False
    )
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    question = _csv_question(plan)
    execute_path = f"/api/projects/{pid}/import/bulk/{plan['plan_id']}/execute"

    missing = client.post(execute_path, json={"decisions": {}})
    assert missing.status_code == 400
    unknown = client.post(
        execute_path,
        json={"decisions": {question["id"]: "combine", "nope": "combine"}},
    )
    assert unknown.status_code == 400

    # The write-time name resolution must not trust the earlier proposed name.
    client.app.state.workspace.get(pid).add_sheet(
        plan["proposed_outputs"][0]["sheet_name"]
    )
    executed = client.post(
        execute_path, json={"decisions": {question["id"]: "combine"}}
    )
    assert executed.status_code == 200, executed.text
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    created = [sheet for sheet in sheets if sheet["name"] != "a"]
    assert len(created) == 1
    assert created[0]["name"] == "a-2"
    assert executed.json()["created"]
    assert executed.json()["first_sheet_id"] == created[0]["id"]
    assert _sheet_rows(client, pid, created[0]["id"]) == [
        {"name": "Ada", "score": "1", "source_file": "folder/a.csv"},
        {"name": "Bob", "score": "2", "source_file": "folder/b.csv"},
    ]

    consumed = client.post(
        execute_path, json={"decisions": {question["id"]: "combine"}}
    )
    assert consumed.status_code == 404
    other_pid = _project(client, "Other project")
    cross_project = client.post(
        f"/api/projects/{other_pid}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {question["id"]: "combine"}},
    )
    assert cross_project.status_code == 404


def test_bulk_manifest_is_bound_to_storage_identity_and_staged_inode(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Manifest identity")
    planned = _bulk_plan(
        client,
        pid,
        [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
        expand_archive=False,
    ).json()
    project = client.app.state.workspace.get(pid)
    project.set_meta("storage_id", "frisket.bundle.v1:replacement")
    response = _execute_bulk(client, pid, planned)
    assert response.status_code == 404
    assert client.get(f"/api/projects/{pid}/sheets").json() == []

    # A fresh plan cannot be redirected through a symlink after publication.
    planned = _bulk_plan(
        client,
        pid,
        [("rows.csv", b"name\nAda\n", "text/csv", "rows.csv")],
        expand_archive=False,
    ).json()
    root = project.path / ".bulk_import_staging" / planned["plan_id"]
    manifest = json.loads((root / "manifest.json").read_text())
    staged = root / manifest["files"][0]["path"]
    replacement = tmp_path / "replacement.csv"
    replacement.write_text("name\nMallory\n")
    staged.unlink()
    staged.symlink_to(replacement)
    response = _execute_bulk(client, pid, planned)
    assert response.status_code == 200
    assert response.json()["created"] == []
    assert response.json()["failed"]
    assert client.get(f"/api/projects/{pid}/sheets").json() == []


def test_concurrent_same_name_bulk_plans_allocate_inside_transaction(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Concurrent names")
    plans = [
        _bulk_plan(
            client,
            pid,
            [("rows.csv", f"name\nvalue-{index}\n".encode(), "text/csv", "rows.csv")],
            expand_archive=False,
        ).json()
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda plan: _execute_bulk(client, pid, plan), plans))
    assert all(response.status_code == 200 for response in responses)
    assert all(not response.json()["failed"] for response in responses)
    assert sorted(
        sheet["name"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    ) == [
        "rows",
        "rows-2",
    ]


def test_concurrent_generic_file_plans_create_distinct_actual_outputs(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Concurrent files")
    plans = [
        _bulk_plan(
            client,
            pid,
            [
                (
                    f"note-{index}.txt",
                    f"note {index}".encode(),
                    "text/plain",
                    f"note-{index}.txt",
                )
            ],
            expand_archive=False,
        ).json()
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda plan: _execute_bulk(client, pid, plan), plans))
    assert all(response.status_code == 200 for response in responses)
    assert all(not response.json()["failed"] for response in responses)
    assert sorted(
        response.json()["created"][0]["sheet_name"] for response in responses
    ) == [
        "files",
        "files-2",
    ]
    assert sorted(
        sheet["name"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    ) == [
        "files",
        "files-2",
    ]


def test_generic_import_cannot_report_success_for_a_missing_sheet(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app, raise_server_exceptions=False)
    pid = _project(client, "Missing generic output")
    planned = _bulk_plan(
        client,
        pid,
        [("note.txt", b"hello", "text/plain", "note.txt")],
        expand_archive=False,
    ).json()

    def missing_output(*_args, **_kwargs):
        return ImportFilesUploadResponse(
            status_code=200, payload={"sheet_id": 999_999, "rows": 1}
        )

    monkeypatch.setattr(
        import_bulk_execute.ImportFilesUploadService,
        "upload_files",
        missing_output,
    )
    response = _execute_bulk(client, pid, planned)
    assert response.status_code == 500
    assert client.get(f"/api/projects/{pid}/sheets").json() == []
    project = app.state.workspace.get(pid)
    assert not (project.path / ".bulk_import_staging" / planned["plan_id"]).exists()


def test_bulk_files_keep_verified_handles_and_pdf_attachment_occurrences(
    tmp_path: Path, monkeypatch
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Held file group")
    content = b"same attachment bytes"
    planned = _bulk_plan(
        client,
        pid,
        [
            ("first.pdf", content, "application/pdf", "cases/first.pdf"),
            ("second.pdf", content, "application/pdf", "cases/second.pdf"),
        ],
        expand_archive=False,
    ).json()
    held = []
    upload = import_bulk_execute.ImportFilesUploadService.upload_files

    def capture(service, project_id, *, files, **kwargs):
        assert [file.filename for file in files] == [
            "cases/first.pdf",
            "cases/second.pdf",
        ]
        assert all(not file.source.closed for file in files)
        assert all(file.sha256 == hashlib.sha256(content).hexdigest() for file in files)
        held.extend(file.source for file in files)
        return upload(service, project_id, files=files, **kwargs)

    monkeypatch.setattr(
        import_bulk_execute.ImportFilesUploadService, "upload_files", capture
    )
    response = _execute_bulk(client, pid, planned)
    assert response.status_code == 200, response.text
    assert len(held) == 2 and all(source.closed for source in held)
    project = client.app.state.workspace.get(pid)
    (sheet,) = project.sheets()
    columns = {column["name"]: column["id"] for column in project.columns(sheet["id"])}
    assert set(columns) == {"filename", "media", "size"}
    assert list(project.get_values(sheet["id"], columns["filename"]).values()) == [
        "cases/first.pdf",
        "cases/second.pdf",
    ]
    media = list(project.get_values(sheet["id"], columns["media"]).values())
    assert len(media) == 2
    assert media[0]["blob"] == media[1]["blob"] == hashlib.sha256(content).hexdigest()
    assert project.read_blob(media[0]["blob"]) == content


def test_bulk_xlsx_retry_reuses_receipt_before_allocating_suffix(
    tmp_path: Path,
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["name"])
    workbook.active.append(["Ada"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    raw = buffer.getvalue()
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Retry workbook")
    results = []
    for _ in range(2):
        plan_response = _bulk_plan(
            client,
            pid,
            [("book.xlsx", raw, "application/octet-stream", "book.xlsx")],
            expand_archive=False,
        )
        assert plan_response.status_code == 200, plan_response.text
        plan = plan_response.json()
        response = _execute_bulk(client, pid, plan)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["failed"] == []
        assert result["created"][0]["rows"] == 1
        results.append(result["created"][0])
        project = client.app.state.workspace.get(pid)
        assert not (project.path / ".bulk_import_staging" / plan["plan_id"]).exists()

    assert results[0]["sheet_id"] == results[1]["sheet_id"]
    assert results[0]["sheet_name"] == results[1]["sheet_name"] == "book"
    assert len(project.sheets()) == 1
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_concurrent_xlsx_plans_create_distinct_actual_outputs(tmp_path: Path) -> None:
    from openpyxl import Workbook

    def workbook_bytes(value: str) -> bytes:
        target = io.BytesIO()
        workbook = Workbook()
        workbook.active.append(["value"])
        workbook.active.append([value])
        workbook.save(target)
        return target.getvalue()

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Concurrent workbooks")
    plans = [
        _bulk_plan(
            client,
            pid,
            [
                (
                    "book.xlsx",
                    workbook_bytes(f"value-{index}"),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "book.xlsx",
                )
            ],
            expand_archive=False,
        ).json()
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda plan: _execute_bulk(client, pid, plan), plans))
    assert all(response.status_code == 200 for response in responses)
    assert all(not response.json()["failed"] for response in responses)
    assert sorted(
        response.json()["created"][0]["sheet_name"] for response in responses
    ) == [
        "book",
        "book-2",
    ]
    assert sorted(
        sheet["name"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    ) == [
        "book",
        "book-2",
    ]


def test_bulk_execute_separate_creates_one_sheet_per_compatible_csv(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    planned = _bulk_plan(client, pid, _compatible_csv_uploads(), expand_archive=False)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    question = _csv_question(plan)

    executed = client.post(
        f"/api/projects/{pid}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {question["id"]: "separate"}},
    )
    assert executed.status_code == 200, executed.text
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert [sheet["name"] for sheet in sheets] == ["a", "b"]
    assert _sheet_rows(client, pid, sheets[0]["id"]) == [{"name": "Ada", "score": "1"}]
    assert _sheet_rows(client, pid, sheets[1]["id"]) == [{"score": "2", "name": "Bob"}]


def test_bulk_plan_ignores_junk_and_refuses_unsafe_or_corrupt_archives(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)

    junk = _bulk_plan(
        client,
        pid,
        [
            (
                "junk.zip",
                _zip_bytes(
                    {
                        ".DS_Store": b"metadata",
                        "nested/.DS_Store": b"metadata",
                        "__MACOSX/rows.csv": b"name\nnot data\n",
                        "__MACOSX/nested/metadata": b"metadata",
                        "._rows.csv": b"name\nnot data\n",
                        "nested/._rows.csv": b"name\nnot data\n",
                        "~$budget.csv": b"name\nnot data\n",
                        "nested/~$budget.csv": b"name\nnot data\n",
                        ".~lock.rows.csv#": b"name\nnot data\n",
                        "nested/.~lock.rows.csv#": b"name\nnot data\n",
                        "Thumbs.db": b"metadata",
                        "nested/Thumbs.db": b"metadata",
                        "desktop.ini": b"metadata",
                        "nested/desktop.ini": b"metadata",
                    }
                ),
                "application/zip",
                "junk.zip",
            )
        ],
        expand_archive=True,
    )
    assert junk.status_code == 200, junk.text
    assert junk.json()["proposed_outputs"] == []
    assert junk.json()["questions"] == []
    assert junk.json()["message"] == "No importable files found."
    assert client.get(f"/api/projects/{pid}/sheets").json() == []

    unsafe = _bulk_plan(
        client,
        pid,
        [
            (
                "unsafe.zip",
                _zip_bytes({"../escape.csv": b"name\nAda\n"}),
                "application/zip",
                "unsafe.zip",
            )
        ],
        expand_archive=True,
    )
    assert unsafe.status_code == 400

    not_sole = _bulk_plan(
        client,
        pid,
        [
            (
                "selected.zip",
                _zip_bytes({"rows.csv": b"name\nAda\n"}),
                "application/zip",
                "selected.zip",
            ),
            ("notes.txt", b"notes", "text/plain", "notes.txt"),
        ],
        expand_archive=True,
    )
    assert not_sole.status_code == 400

    duplicate = _bulk_plan(
        client,
        pid,
        [
            (
                "duplicate.zip",
                _zip_bytes(
                    [
                        ("folder/rows.csv", b"name\nAda\n"),
                        ("folder/./rows.csv", b"name\nBob\n"),
                    ]
                ),
                "application/zip",
                "duplicate.zip",
            )
        ],
        expand_archive=True,
    )
    assert duplicate.status_code == 400

    symlink = _bulk_plan(
        client,
        pid,
        [
            (
                "symlink.zip",
                _zip_with_unix_member(
                    "linked.csv",
                    stat.S_IFLNK | 0o777,
                    "elsewhere.csv",
                ),
                "application/zip",
                "symlink.zip",
            )
        ],
        expand_archive=True,
    )
    assert symlink.status_code == 400

    fifo = _bulk_plan(
        client,
        pid,
        [
            (
                "fifo.zip",
                _zip_with_unix_member(
                    "stream.csv",
                    stat.S_IFIFO | 0o644,
                    "name\nAda\n",
                ),
                "application/zip",
                "fifo.zip",
            )
        ],
        expand_archive=True,
    )
    assert fifo.status_code == 400

    corrupt = _bulk_plan(
        client,
        pid,
        [("broken.zip", b"not a zip", "application/zip", "broken.zip")],
        expand_archive=True,
    )
    assert corrupt.status_code == 400


@pytest.mark.parametrize(
    ("case", "member_name"),
    [
        ("absolute-posix", "/escape.csv"),
        ("windows-drive", "C:\\escape.csv"),
        ("windows-unc", "\\\\server\\share\\escape.csv"),
        ("encrypted", None),
    ],
)
def test_bulk_plan_rejects_unsafe_or_encrypted_zip_atomically(
    tmp_path: Path,
    case: str,
    member_name: str | None,
) -> None:
    archive = _zip_bytes({member_name or "safe.csv": b"name\nAda\n"})
    if case == "encrypted":
        archive = _mark_zip_encrypted(archive)
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, f"Reject {case} ZIP")

    response = _bulk_plan(
        client,
        pid,
        [("unsafe.zip", archive, "application/zip", "unsafe.zip")],
        expand_archive=True,
    )

    assert response.status_code == 400
    assert _bulk_staging_directories(client, pid) == []
    assert client.get(f"/api/projects/{pid}/sheets").json() == []


def test_bulk_plan_rejects_nul_in_raw_zip_member_name_atomically(
    tmp_path: Path,
) -> None:
    archive = _rewrite_zip_member_name(
        _zip_bytes({"safe.csv": b"name\nAda\n"}),
        b"safe.csv",
        b"bad\0.csv",
    )
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, "NUL member name")

    response = _bulk_plan(
        client,
        pid,
        [("invalid-name.zip", archive, "application/zip", "invalid-name.zip")],
        expand_archive=True,
    )

    assert (response.status_code, _bulk_staging_directories(client, pid)) == (400, [])


def test_bulk_plan_rejects_invalid_utf8_zip_member_name_atomically(
    tmp_path: Path,
) -> None:
    utf8_name = "é.csv".encode()
    archive = _rewrite_zip_member_name(
        _zip_bytes({"é.csv": b"name\nAda\n"}),
        utf8_name,
        b"\xff" + utf8_name[1:],
    )
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, "Invalid UTF-8 member name")

    response = _bulk_plan(
        client,
        pid,
        [("invalid-name.zip", archive, "application/zip", "invalid-name.zip")],
        expand_archive=True,
    )

    assert (response.status_code, _bulk_staging_directories(client, pid)) == (400, [])


def test_bulk_plan_rejects_late_corrupt_deflate_member_atomically(
    tmp_path: Path,
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.csv", b"name\nAda\n")
        archive.writestr("z.csv", b"abcdefgh" * 10_000)
    corrupted = bytearray(payload.getvalue())
    with zipfile.ZipFile(io.BytesIO(corrupted)) as archive:
        late_member = archive.getinfo("z.csv")
    name_length, extra_length = struct.unpack_from(
        "<HH", corrupted, late_member.header_offset + 26
    )
    compressed_data = late_member.header_offset + 30 + name_length + extra_length
    assert corrupted[compressed_data] != 0
    corrupted[compressed_data] = 0

    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, "Late corrupt member")
    response = _bulk_plan(
        client,
        pid,
        [
            (
                "corrupt.zip",
                bytes(corrupted),
                "application/zip",
                "corrupt.zip",
            )
        ],
        expand_archive=True,
    )

    assert (response.status_code, _bulk_staging_directories(client, pid)) == (400, [])


def test_bulk_zip_member_publication_failure_is_server_error_and_cleans_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, "ZIP publication failure")

    def fail_publish(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "injected no space")

    monkeypatch.setattr(import_bulk_sources, "publish_staged_file", fail_publish)
    response = _bulk_plan(
        client,
        pid,
        [
            (
                "valid.zip",
                _zip_bytes({"rows.csv": b"name\nAda\n"}),
                "application/zip",
                "valid.zip",
            )
        ],
        expand_archive=True,
    )

    assert (response.status_code, _bulk_staging_directories(client, pid)) == (500, [])


def test_bulk_bzip2_backing_read_failure_is_server_error_and_cleans_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("rows.csv", b"name\nAda\n")

    def fail_bzip2_backing_read(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected archive read failure")

    monkeypatch.setattr(zipfile.ZipExtFile, "read", fail_bzip2_backing_read)
    client = TestClient(
        create_app(tmp_path / "workspace"), raise_server_exceptions=False
    )
    pid = _project(client, "BZIP2 backing read failure")

    response = _bulk_plan(
        client,
        pid,
        [("valid.zip", payload.getvalue(), "application/zip", "valid.zip")],
        expand_archive=True,
    )

    assert (response.status_code, _bulk_staging_directories(client, pid)) == (500, [])


def test_bulk_plan_executes_staged_email_with_canonical_attachment_storage(
    tmp_path: Path,
) -> None:
    attachment = b"\x00binary invoice\xff"
    message = EmailMessage()
    message["Date"] = "Tue, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>"
    message["Subject"] = "Bulk email"
    message.set_content("Plain message body")
    message.add_alternative("<p>HTML message body</p>", subtype="html")
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="octet-stream",
        filename="invoice.bin",
    )

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    planned = _bulk_plan(
        client,
        pid,
        [
            (
                "inbox.eml",
                message.as_bytes(),
                "message/rfc822",
                "mail/inbox.eml",
            )
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["questions"] == []
    assert plan["proposed_outputs"] == [
        {
            "id": plan["proposed_outputs"][0]["id"],
            "kind": "email",
            "sheet_name": "Emails",
            "logical_paths": ["mail/inbox.eml"],
        }
    ]

    executed = client.post(
        f"/api/projects/{pid}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {}},
    )
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["failed"] == []
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["kind"] == "sheet"
    assert created["sheet_name"] == "Emails"
    assert created["logical_paths"] == ["mail/inbox.eml"]
    assert result["first_sheet_id"] == created["sheet_id"]

    project = client.app.state.workspace.get(pid)
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (created["sheet_id"],),
        ).fetchall()
    }
    values = {
        name: list(project.get_values(created["sheet_id"], int(column["id"])).values())
        for name, column in columns.items()
    }
    assert values["subject"] == ["Bulk email"]
    assert values["body"] == ["Plain message body"]
    assert values["html"] == ["<p>HTML message body</p>"]
    assert values["source_file"] == ["mail/inbox.eml"]
    digest = hashlib.sha256(attachment).hexdigest()
    assert values["attachments"] == [
        [
            {
                "blob": digest,
                "mime": "application/octet-stream",
                "filename": "invoice.bin",
            }
        ]
    ]
    with project.blob_store.materialize(digest) as stored:
        assert stored.read_bytes() == attachment
    downloaded = client.get(f"/api/projects/{pid}/blobs/{digest}")
    assert downloaded.status_code == 200
    assert downloaded.content == attachment


def test_bulk_email_consumes_the_verified_open_inode_if_staged_path_is_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = EmailMessage()
    original["From"] = "reporter@example.test"
    original["Subject"] = "Held original"
    original.set_content("original bytes")
    replacement = EmailMessage()
    replacement["From"] = "attacker@example.test"
    replacement["Subject"] = "Path replacement"
    replacement.set_content("replacement bytes")

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Pinned email inode")
    planned = _bulk_plan(
        client,
        pid,
        [("message.eml", original.as_bytes(), "message/rfc822", "message.eml")],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan_id = planned.json()["plan_id"]
    project = client.app.state.workspace.get(pid)
    plan_root = project.path / ".bulk_import_staging" / plan_id
    manifest = json.loads((plan_root / "manifest.json").read_text())
    staged = plan_root / manifest["files"][0]["path"]
    real_run_action_spec = import_bulk_execute.run_action_spec
    held_sources = []

    def replace_path_after_verification(project, action, **kwargs):
        source_ref = import_bulk_plan.stable_identifier(
            "bulk-email-source",
            planned.json()["proposed_outputs"][0]["id"],
            [plan_id, "0", "message.eml", manifest["files"][0]["sha256"]],
        )
        assert action == {
            "action_id": "import.email",
            "scope": {"kind": "project"},
            "sheet_name": "Emails",
            "params": {
                "sources": [
                    {
                        "source_ref": source_ref,
                        "logical_path": "message.eml",
                        "format": "eml",
                    }
                ]
            },
            "output_names": {},
            "idempotency_key": (
                f"bulk:{plan_id}:{planned.json()['proposed_outputs'][0]['id']}"
            ),
        }
        admitted = kwargs["deps"].email_sources
        assert set(admitted) == {source_ref}
        source = admitted[source_ref]
        assert isinstance(source, EmailInput)
        assert (source.logical_path, source.format) == ("message.eml", "eml")
        assert not source.stream.closed
        assert source.stream.tell() == 0
        held_sources.append(source.stream)
        swap = tmp_path / "replacement.eml"
        swap.write_bytes(replacement.as_bytes())
        try:
            swap.replace(staged)
        except PermissionError:
            # Windows pins the verified file by denying replacement while its
            # handle is open; POSIX permits replacement but retains the inode.
            if os.name != "nt":
                raise
        result = real_run_action_spec(project, action, **kwargs)
        assert not source.stream.closed, "the typed reader only borrows ingress streams"
        return result

    monkeypatch.setattr(
        import_bulk_execute, "run_action_spec", replace_path_after_verification
    )
    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["failed"] == []
    rows = _sheet_rows(client, pid, result["created"][0]["sheet_id"])
    assert [row["subject"] for row in rows] == ["Held original"]
    assert len(held_sources) == 1
    assert held_sources[0].closed, "bulk ingress owns final stream cleanup"
    assert not plan_root.exists()


def test_bulk_email_keeps_valid_messages_and_returns_bounded_parse_warnings(
    tmp_path: Path,
) -> None:
    """A bad message is a warning, not a failed bulk-email import."""

    valid = EmailMessage()
    valid["From"] = "reporter@example.test"
    valid["Subject"] = "Still usable"
    valid.set_content("The valid message must remain importable.")

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Email warnings")
    planned = _bulk_plan(
        client,
        pid,
        [
            ("good.eml", valid.as_bytes(), "message/rfc822", "mail/good.eml"),
            # The parser deliberately recognizes this as a malformed message,
            # while the neighboring EML is still a valid row.
            ("bad.eml", b"not an RFC 822 message\n", "message/rfc822", "mail/bad.eml"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text

    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["failed"] == []
    assert len(result["created"]) == 1
    assert result["first_sheet_id"] == result["created"][0]["sheet_id"]
    assert len(result["warnings"]) == 1
    assert "mail/bad.eml" in result["warnings"][0]
    assert "recognizable headers" in result["warnings"][0]
    rows = _sheet_rows(client, pid, result["created"][0]["sheet_id"])
    assert len(rows) == 1
    assert rows[0]["subject"] == "Still usable"
    assert rows[0]["source_file"] == "mail/good.eml"


def test_bulk_email_warning_response_is_bounded_with_a_compact_remainder(
    tmp_path: Path,
) -> None:
    valid = EmailMessage()
    valid["From"] = "reporter@example.test"
    valid["Subject"] = "Usable despite bad neighbors"
    valid.set_content("Keep this row.")
    malformed = [
        (
            f"bad-{index:02}.eml",
            b"not an RFC 822 message\n",
            "message/rfc822",
            f"mail/bad-{index:02}.eml",
        )
        for index in range(21)
    ]
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Bounded email warnings")
    planned = _bulk_plan(
        client,
        pid,
        [("good.eml", valid.as_bytes(), "message/rfc822", "mail/good.eml"), *malformed],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text

    executed = _execute_bulk(client, pid, planned.json())
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["failed"] == []
    assert len(result["created"]) == 1
    assert len(result["warnings"]) == 20
    assert any(
        "mail/bad-00.eml" in warning and "recognizable headers" in warning
        for warning in result["warnings"]
    )
    summaries = [
        warning
        for warning in result["warnings"]
        if "21" in warning and "omit" in warning.lower()
    ]
    assert len(summaries) == 1


def test_bulk_email_failure_is_compact_and_does_not_discard_successful_sibling(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client, "Partial email import")
    planned = _bulk_plan(
        client,
        pid,
        [
            (
                "bad.eml",
                b"not an RFC 822 message\n",
                "message/rfc822",
                "a/bad.eml",
            ),
            ("records.csv", b"name\nAda\n", "text/csv", "z/records.csv"),
        ],
        expand_archive=False,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()

    executed = _execute_bulk(client, pid, plan)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert len(result["created"]) == 1
    assert result["created"][0]["logical_paths"] == ["z/records.csv"]
    assert result["created"][0]["rows"] == 1
    assert result["failed"] == [
        {
            "id": result["failed"][0]["id"],
            "kind": "email",
            "logical_paths": ["a/bad.eml"],
            "error": "no valid email messages were found",
        }
    ]
    assert result["first_sheet_id"] == result["created"][0]["sheet_id"]
    project = client.app.state.workspace.get(pid)
    assert not (project.path / ".bulk_import_staging" / plan["plan_id"]).exists()
