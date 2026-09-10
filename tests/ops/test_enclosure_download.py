import json

import httpx
from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.engine.jobs import Worker
from frisket.engine.store.sources import SourceStore


def _project(client: TestClient):
    pid = client.post("/api/projects", json={"name": "Enclosures"}).json()["id"]
    return pid, client.app.state.workspace.get(pid)


def _row_with_enclosure(project):
    sheet_id = project.add_sheet("feed")
    cols = {
        "guid": project.add_column(sheet_id, "guid"),
        "link": project.add_column(sheet_id, "link", type="link"),
        "enclosure_url": project.add_column(sheet_id, "enclosure_url", type="link"),
        "enclosure_mime": project.add_column(sheet_id, "enclosure_mime"),
        "media": project.add_column(sheet_id, "media", type="audio"),
        "media_status": project.add_column(sheet_id, "media_status", type="category"),
    }
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "guid": "ep1",
                "link": "https://pod.example/ep1",
                "enclosure_url": "https://cdn.example/ep1.mp3",
                "enclosure_mime": "audio/mpeg",
                "media_status": "remote",
            }
        ],
        cols,
    )[0]
    return sheet_id, row_id


def _cell(client: TestClient, pid: str, sheet_id: int, row_id: int, column: str):
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    col = next(c for c in data["columns"] if c["name"] == column)
    row = next(r for r in data["rows"] if r["id"] == row_id)
    return row["cells"][str(col["id"])]


def _fake_download(data=b"ID3fake", mime="audio/mpeg", filename="ep1.mp3", err=None):
    def go(url, **kwargs):  # noqa: ANN001
        assert url == "https://cdn.example/ep1.mp3"
        return data, mime, filename, err

    return go


def _materialize_enclosure_action(
    sheet_id: int,
    row_id: int,
    *,
    force: bool = False,
    idempotency_key: str = "test-media-enclosure@sha256:first",
) -> dict:
    params: dict[str, object] = {}
    if force:
        params["force"] = True
    return {
        "action_id": "media.enclosure_materialize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _post_materialize_enclosure_action(
    client: TestClient,
    pid: str,
    sheet_id: int,
    row_id: int,
    *,
    force: bool = False,
    idempotency_key: str = "test-media-enclosure@sha256:first",
):
    return client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_materialize_enclosure_action(
            sheet_id,
            row_id,
            force=force,
            idempotency_key=idempotency_key,
        ),
    )


def test_manual_enclosure_download_uses_v1_action_run_and_records_receipt(
    tmp_path,
    monkeypatch,
):
    import frisket.ops.enclosures as enclosures

    monkeypatch.setattr(enclosures, "download_url", _fake_download())
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, row_id = _row_with_enclosure(project)

    response = _post_materialize_enclosure_action(client, pid, sheet_id, row_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "completed"
    assert body["action"]["kind"] == "media.enclosure_materialize"
    assert body["receipt_id"]
    assert [output["kind"] for output in body["outputs"]] == [
        "rows",
        "media_cell",
        "media_blob",
    ]
    assert body["outputs"][0]["row_ids"] == [row_id]

    media = _cell(client, pid, sheet_id, row_id, "media")
    assert media["mime"] == "audio/mpeg"
    assert media["filename"] == "ep1.mp3"
    assert _cell(client, pid, sheet_id, row_id, "media_status") == "downloaded"

    blob = project.db.execute(
        "SELECT source_url, filename, mime FROM blobs WHERE hash=?", (media["blob"],)
    ).fetchone()
    assert dict(blob) == {
        "source_url": "https://cdn.example/ep1.mp3",
        "filename": "ep1.mp3",
        "mime": "audio/mpeg",
    }
    receipt = project.db.execute(
        "SELECT action_kind, status FROM receipts WHERE id=?", (body["receipt_id"],)
    ).fetchone()
    assert dict(receipt) == {
        "action_kind": "media.enclosure_materialize",
        "status": "completed",
    }
    assert project.row_count(sheet_id) == 1


def test_enclosure_download_v1_action_is_row_idempotent_unless_forced(
    tmp_path,
    monkeypatch,
):
    import frisket.ops.enclosures as enclosures

    calls: list[str] = []

    def fake_download(url, **kwargs):  # noqa: ANN001, ARG001
        calls.append(url)
        return f"ID3fake-{len(calls)}".encode(), "audio/mpeg", "ep1.mp3", None

    monkeypatch.setattr(enclosures, "download_url", fake_download)
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, row_id = _row_with_enclosure(project)

    first = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        row_id,
        idempotency_key="test-media-enclosure@sha256:first",
    ).json()
    first_blob = _cell(client, pid, sheet_id, row_id, "media")["blob"]
    second = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        row_id,
        idempotency_key="test-media-enclosure@sha256:second",
    ).json()
    second_blob = _cell(client, pid, sheet_id, row_id, "media")["blob"]
    forced = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        row_id,
        force=True,
        idempotency_key="test-media-enclosure@sha256:forced",
    ).json()
    forced_blob = _cell(client, pid, sheet_id, row_id, "media")["blob"]

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert forced["status"] == "completed"
    assert calls == ["https://cdn.example/ep1.mp3", "https://cdn.example/ep1.mp3"]
    assert first_blob == second_blob
    assert first_blob != forced_blob


def test_internal_queued_enclosure_worker_marks_and_downloads(tmp_path, monkeypatch):
    import frisket.ops.enclosures as enclosures

    monkeypatch.setattr(enclosures, "download_url", _fake_download())
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, row_id = _row_with_enclosure(project)
    ws = client.app.state.workspace

    op_id = enclosures.mark_enclosure_queued(project, sheet_id=sheet_id, row_id=row_id)
    job_id = ws.queue.enqueue(
        enclosures.ENCLOSURE_DOWNLOAD_KIND,
        {
            "project_id": pid,
            "sheet_id": sheet_id,
            "row_id": row_id,
            "workspace_root": str(ws.root),
        },
        max_attempts=1,
    )
    assert op_id
    assert job_id
    assert _cell(client, pid, sheet_id, row_id, "media_status") == "queued"

    Worker(
        ws.queue, ws.registry, worker_id="test-worker", poll_interval=0.01
    ).run_once()

    assert _cell(client, pid, sheet_id, row_id, "media_status") == "downloaded"
    assert _cell(client, pid, sheet_id, row_id, "media")["filename"] == "ep1.mp3"


def test_v1_enclosure_action_rejects_missing_row(tmp_path):
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, _row_id = _row_with_enclosure(project)

    response = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        999,
        idempotency_key="test-media-enclosure@sha256:missing-row",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "invalid_input_ref"
    assert body["errors"][0]["field"] == "scope.row_ids"


def test_enclosure_job_structural_error_completes_with_result(tmp_path):
    import frisket.ops.enclosures as enclosures

    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id = project.add_sheet("empty")
    ws = client.app.state.workspace
    jid = ws.queue.enqueue(
        enclosures.ENCLOSURE_DOWNLOAD_KIND,
        {
            "project_id": pid,
            "sheet_id": sheet_id,
            "row_id": 999,
            "workspace_root": str(ws.root),
        },
        max_attempts=1,
    )

    assert Worker(ws.queue, ws.registry, worker_id="test-worker").run_once()
    job = ws.queue.get(jid)
    assert job.status == "done"
    assert job.result["status"] == "error"
    assert "row not found" in job.result["error"]


def test_v1_enclosure_action_records_error_without_retry(tmp_path, monkeypatch):
    import frisket.ops.enclosures as enclosures

    monkeypatch.setattr(
        enclosures,
        "download_url",
        _fake_download(data=b"", mime="", filename="", err="blocked URL"),
    )
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, row_id = _row_with_enclosure(project)

    response = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        row_id,
        idempotency_key="test-media-enclosure@sha256:error",
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "media_download_failed"
    assert _cell(client, pid, sheet_id, row_id, "media") is None
    assert _cell(client, pid, sheet_id, row_id, "media_status") == "error"
    assert "blocked URL" in _cell(client, pid, sheet_id, row_id, "media_error")
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_enclosure_error_is_redacted_before_row_action_and_receipt(
    tmp_path,
    monkeypatch,
):
    import frisket.ops.enclosures as enclosures

    sentinel = "sk-enclosure-materialize-sentinel"
    raw_error = f"download denied; api_key={sentinel}; retry later"
    safe_error = "download denied; api_key=[REDACTED]; retry later"
    failed_download = _fake_download(data=b"", mime="", filename="", err=raw_error)

    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id, row_id = _row_with_enclosure(project)

    direct = enclosures.materialize_enclosure_row(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        fetch=failed_download,
    )
    assert direct["status"] == "error"
    assert direct["error"] == safe_error
    assert sentinel not in json.dumps(direct, sort_keys=True)
    assert _cell(client, pid, sheet_id, row_id, "media_error") == safe_error
    assert _cell(client, pid, sheet_id, row_id, "enclosure_url") == (
        "https://cdn.example/ep1.mp3"
    )

    monkeypatch.setattr(enclosures, "download_url", failed_download)
    response = _post_materialize_enclosure_action(
        client,
        pid,
        sheet_id,
        row_id,
        idempotency_key="test-media-enclosure@sha256:redacted-error",
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "media_download_failed"
    assert body["errors"][0]["message"] == safe_error
    assert sentinel not in json.dumps(body, sort_keys=True)
    assert _cell(client, pid, sheet_id, row_id, "media_error") == safe_error

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (body["receipt_id"],),
    ).fetchone()
    assert receipt_row is not None
    receipt = json.loads(receipt_row["body"])
    serialized_receipt = json.dumps(receipt, sort_keys=True)
    assert sentinel not in serialized_receipt
    assert receipt["errors"][0]["message"] == safe_error
    failed_cell = next(
        item["ref"]
        for item in receipt["evidence"]
        if item["ref"].get("kind") == "media_cell"
    )
    assert failed_cell["status"] == "error"
    assert failed_cell["error"] == safe_error


def test_import_urls_uses_shared_direct_download_primitive(tmp_path, monkeypatch):
    from frisket.ops import url_import as import_family

    seen: list[str] = []

    def fake_download(url, **kwargs):  # noqa: ANN001, ARG001
        seen.append(url)
        return b"ID3fake", "audio/mpeg", "ep1.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = _client(tmp_path)
    pid, project = _project(client)

    response = client.post(
        f"/api/projects/{pid}/import/urls",
        json={"urls": ["https://cdn.example/ep1.mp3"], "column": "media"},
    )
    assert response.status_code == 200, response.text
    assert seen == ["https://cdn.example/ep1.mp3"]

    sheet_id = response.json()["sheet_id"]
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    cols = {c["name"]: c for c in data["columns"]}
    media = data["rows"][0]["cells"][str(cols["media"]["id"])]
    assert media["blob"]
    assert media["mime"] == "audio/mpeg"
    blob = project.db.execute(
        "SELECT source_url, filename, mime FROM blobs WHERE hash=?", (media["blob"],)
    ).fetchone()
    assert dict(blob) == {
        "source_url": "https://cdn.example/ep1.mp3",
        "filename": "ep1.mp3",
        "mime": "audio/mpeg",
    }


def test_enclosure_filename_prefers_rfc5987_filename_star():
    import frisket.ops.enclosures as enclosures

    resp = httpx.Response(
        200,
        headers={
            "content-disposition": (
                "attachment; filename=generic.mp3; filename*=UTF-8''Episode%20One.mp3"
            )
        },
    )

    assert (
        enclosures._filename_for(resp, "https://cdn.example/download", "audio/mpeg")
        == "Episode One.mp3"
    )


def test_source_config_queued_downloads_new_enclosures(tmp_path, monkeypatch):
    import httpx
    from frisket import ingest
    import frisket.ops.enclosures as enclosures

    monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)
    monkeypatch.setattr(enclosures, "download_url", _fake_download())
    client = _client(tmp_path)
    pid, project = _project(client)
    source_id = SourceStore(project).add_source(
        name="Podcast",
        kind="rss",
        url="https://feed.example/rss.xml",
        config={"download_enclosures": "queued"},
    )
    feed = (
        "<?xml version='1.0'?><rss version='2.0'><channel><title>Podcast</title>"
        "<item><guid>ep1</guid><title>Episode</title>"
        "<link>https://pod.example/ep1</link>"
        "<enclosure url='https://cdn.example/ep1.mp3' length='10' type='audio/mpeg' />"
        "</item></channel></rss>"
    ).encode()

    class FakeResponse:
        is_redirect = False
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def iter_bytes(self):
            yield feed

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def stream(self, method, url):  # noqa: ANN001
            assert method == "GET"
            assert url == "https://feed.example/rss.xml"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "source.poll",
            "scope": {"kind": "project"},
            "params": {"source": source_id},
            "idempotency_key": "enclosure-source-poll-queued",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    source_run = next(
        output["ref"] for output in body["outputs"] if output["kind"] == "source_run"
    )
    sheet_id = source_run["sheet_id"]

    ws = client.app.state.workspace
    queued_enclosures = [
        job
        for job in ws.queue.list_jobs(status="queued")
        if job.kind == enclosures.ENCLOSURE_DOWNLOAD_KIND
    ]
    assert len(queued_enclosures) == 1
    worker = Worker(ws.queue, ws.registry, worker_id="test-worker", poll_interval=0.01)
    while worker.run_once():
        pass

    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    row = data["rows"][0]
    cols = {c["name"]: c for c in data["columns"]}
    assert row["cells"][str(cols["media_status"]["id"])] == "downloaded"
    media = row["cells"][str(cols["media"]["id"])]
    assert media["blob"]
    assert media["filename"] == "ep1.mp3"
    assert (
        json.loads(SourceStore(project).get_source(source_id)["config"])[
            "download_enclosures"
        ]
        == "queued"
    )
