from __future__ import annotations

from pathlib import Path

from helpers import make_client as _client
from frisket.server.services.sample_project import (
    SAMPLE_AGENCY_PAYMENT_ROWS,
    SAMPLE_AGENCY_PAYMENTS_SHEET_NAME,
    SAMPLE_ARTICLE_ROWS,
    SAMPLE_BLANK_COLUMN,
    SAMPLE_COUNCIL_AUDIO_ROWS,
    SAMPLE_COUNCIL_AUDIO_SHEET_NAME,
    SAMPLE_COUNCIL_MEETINGS_SHEET_NAME,
    SAMPLE_CONTRACT_ROWS,
    SAMPLE_CONTRACTS_SHEET_NAME,
    SAMPLE_DOCKET_SHEET_NAME,
    SAMPLE_FILINGS_SHEET_NAME,
    SAMPLE_LAWSUIT_ROWS,
    SAMPLE_LOCAL_MODEL_LAB_ROWS,
    SAMPLE_SMALL_MODEL_LAB_SHEET_NAME,
    SAMPLE_MULTILINGUAL_ROWS,
    SAMPLE_MULTILINGUAL_SHEET_NAME,
    SAMPLE_SHEET_NAME,
    SampleProjectSeedService,
)


def test_seeded_sample_project_deletes_with_unstarted_queued_work(
    tmp_path: Path,
) -> None:
    """A worker-less local install must not trap its sample behind queued work."""
    client = _client(tmp_path)
    created = client.post("/api/projects", json={"name": "Sample project"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    seeded = client.post(f"/api/projects/{pid}/seed-sample")
    assert seeded.status_code == 200, seeded.text

    queue = client.app.state.workspace.queue
    job_id = queue.enqueue("project.run", {"project_id": pid})

    deleted = client.request(
        "DELETE",
        f"/api/projects/{pid}",
        json={"confirm_name": "Sample project"},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "deleted": pid}
    assert queue.get(job_id).status == "cancelled"
    assert client.get("/api/projects").json() == []


def test_seed_sample_second_call_reuses_existing_sheet_no_duplicate(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Idempotency check"}).json()["id"]

    first = client.post(f"/api/projects/{pid}/seed-sample")
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["sheet_name"] == SAMPLE_SHEET_NAME
    assert first_payload["blank_column"] == SAMPLE_BLANK_COLUMN
    assert first_payload["rows"] == SAMPLE_ARTICLE_ROWS

    second = client.post(f"/api/projects/{pid}/seed-sample")
    assert second.status_code == 200, second.text
    second_payload = second.json()

    # Same sheet reused, not a fresh one created underneath.
    assert second_payload["sheet_id"] == first_payload["sheet_id"]
    assert second_payload["rows"] == first_payload["rows"]
    assert second_payload["sheet_name"] == SAMPLE_SHEET_NAME
    assert second_payload["blank_column"] == SAMPLE_BLANK_COLUMN

    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    dispatch_sheets = [s for s in sheets if s["name"] == SAMPLE_SHEET_NAME]
    assert len(dispatch_sheets) == 1, (
        "re-seeding an already-seeded project must not create a second "
        f"Dispatches sheet: {sheets}"
    )
    assert dispatch_sheets[0]["rows"] == SAMPLE_ARTICLE_ROWS
    contracts = [s for s in sheets if s["name"] == SAMPLE_CONTRACTS_SHEET_NAME]
    assert len(contracts) == 1
    assert contracts[0]["rows"] == SAMPLE_CONTRACT_ROWS
    docket = [s for s in sheets if s["name"] == SAMPLE_DOCKET_SHEET_NAME]
    filings = [s for s in sheets if s["name"] == SAMPLE_FILINGS_SHEET_NAME]
    meetings = [s for s in sheets if s["name"] == SAMPLE_COUNCIL_MEETINGS_SHEET_NAME]
    audio = [s for s in sheets if s["name"] == SAMPLE_COUNCIL_AUDIO_SHEET_NAME]
    multilingual = [s for s in sheets if s["name"] == SAMPLE_MULTILINGUAL_SHEET_NAME]
    agency_payments = [
        s for s in sheets if s["name"] == SAMPLE_AGENCY_PAYMENTS_SHEET_NAME
    ]
    local_model_lab = [
        s for s in sheets if s["name"] == SAMPLE_SMALL_MODEL_LAB_SHEET_NAME
    ]
    assert len(docket) == 1
    assert len(filings) == 1
    assert docket[0]["rows"] == SAMPLE_LAWSUIT_ROWS
    assert filings[0]["rows"] == SAMPLE_LAWSUIT_ROWS
    assert len(meetings) == 1
    assert len(audio) == 1
    assert meetings[0]["rows"] == SAMPLE_COUNCIL_AUDIO_ROWS
    assert audio[0]["rows"] == SAMPLE_COUNCIL_AUDIO_ROWS
    assert len(multilingual) == 1
    assert multilingual[0]["rows"] == SAMPLE_MULTILINGUAL_ROWS
    assert len(agency_payments) == 1
    assert agency_payments[0]["rows"] == SAMPLE_AGENCY_PAYMENT_ROWS

    agency_data = client.get(
        f"/api/projects/{pid}/sheets/{agency_payments[0]['id']}/data?offset=0&limit=20"
    ).json()
    agency_columns = {
        column["name"]: str(column["id"]) for column in agency_data["columns"]
    }
    assert set(agency_columns) == {
        "payment_id",
        "agency",
        "vendor",
        "paid_at",
        "amount_usd",
        "memo",
    }
    agency_values = [
        str(row["cells"][agency_columns["agency"]]) for row in agency_data["rows"]
    ]
    assert len(set(agency_values)) == 12
    assert " Public Works Department" in agency_values
    assert "Public Works Department " in agency_values
    assert "agency_clean" not in agency_columns

    assert len(local_model_lab) == 1
    assert local_model_lab[0]["rows"] == SAMPLE_LOCAL_MODEL_LAB_ROWS

    lab_data = client.get(
        f"/api/projects/{pid}/sheets/{local_model_lab[0]['id']}/data?offset=0&limit=20"
    ).json()
    lab_columns = {column["name"]: str(column["id"]) for column in lab_data["columns"]}
    assert set(lab_columns) == {
        "record_id",
        "easy_notice",
        "expected_notice_type",
        "challenge_text",
        "challenge_answer",
    }
    assert "small_model_label" not in lab_columns
    assert "small_model_answer" not in lab_columns
    expected_labels = {
        str(row["cells"][lab_columns["expected_notice_type"]])
        for row in lab_data["rows"]
    }
    assert expected_labels == {"meeting", "inspection", "contract"}

    multilingual_data = client.get(
        f"/api/projects/{pid}/sheets/{multilingual[0]['id']}/data?offset=0&limit=20"
    ).json()
    multilingual_columns = {
        column["name"]: str(column["id"]) for column in multilingual_data["columns"]
    }
    assert set(multilingual_columns) == {
        "record_id",
        "name_as_written",
        "source",
        "record_note",
    }
    written_values = {
        str(row["cells"][multilingual_columns["name_as_written"]])
        for row in multilingual_data["rows"]
    }
    assert "Александр Петров" in written_values
    assert "ألكسندر بيتروف" in written_values
    assert "アレクサンドル・ペトロフ" in written_values
    assert "latin_text" not in multilingual_columns

    data = client.get(
        f"/api/projects/{pid}/sheets/{dispatch_sheets[0]['id']}/data?offset=0&limit=24"
    ).json()
    column_names = {column["name"] for column in data["columns"]}
    assert {"record_id", "headline", "story", "canonical_url"} <= column_names
    assert {"summary", "topic", "relevance", "word_count"}.isdisjoint(column_names)
    story_id = next(
        column["id"] for column in data["columns"] if column["name"] == "story"
    )
    story_lengths = [len(str(row["cells"][str(story_id)])) for row in data["rows"]]
    assert min(story_lengths) < 200
    assert max(story_lengths) > 1_000

    filing_data = client.get(
        f"/api/projects/{pid}/sheets/{filings[0]['id']}/data?offset=0&limit=1"
    ).json()
    media_column = next(
        column for column in filing_data["columns"] if column["name"] == "media"
    )
    assert media_column["type"] == "file"
    media = filing_data["rows"][0]["cells"][str(media_column["id"])]
    blob = client.get(f"/api/projects/{pid}/blobs/{media['blob']}")
    assert blob.status_code == 200
    assert blob.headers["content-type"].startswith("application/pdf")
    assert blob.content.startswith(b"%PDF-1.4")

    audio_data = client.get(
        f"/api/projects/{pid}/sheets/{audio[0]['id']}/data?offset=0&limit=1"
    ).json()
    audio_column = next(
        column for column in audio_data["columns"] if column["name"] == "media"
    )
    assert audio_column["type"] == "audio"
    audio_media = audio_data["rows"][0]["cells"][str(audio_column["id"])]
    audio_blob = client.get(f"/api/projects/{pid}/blobs/{audio_media['blob']}")
    assert audio_blob.status_code == 200
    assert audio_blob.headers["content-type"].startswith("audio/mpeg")
    assert audio_blob.content.startswith(b"ID3")


def test_seed_sample_third_call_still_idempotent(tmp_path: Path) -> None:
    """Not just "second call is special" — repeated re-seeding stays stable."""
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Repeat clicks"}).json()["id"]

    payloads = [
        client.post(f"/api/projects/{pid}/seed-sample").json() for _ in range(3)
    ]
    sheet_ids = {p["sheet_id"] for p in payloads}
    assert len(sheet_ids) == 1, f"expected one stable sheet id, got {sheet_ids}"

    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert len([s for s in sheets if s["name"] == SAMPLE_SHEET_NAME]) == 1
    assert len([s for s in sheets if s["name"] == SAMPLE_CONTRACTS_SHEET_NAME]) == 1
    assert len([s for s in sheets if s["name"] == SAMPLE_DOCKET_SHEET_NAME]) == 1
    assert len([s for s in sheets if s["name"] == SAMPLE_FILINGS_SHEET_NAME]) == 1
    assert (
        len([s for s in sheets if s["name"] == SAMPLE_COUNCIL_MEETINGS_SHEET_NAME]) == 1
    )
    assert len([s for s in sheets if s["name"] == SAMPLE_COUNCIL_AUDIO_SHEET_NAME]) == 1
    assert len([s for s in sheets if s["name"] == SAMPLE_MULTILINGUAL_SHEET_NAME]) == 1
    assert (
        len([s for s in sheets if s["name"] == SAMPLE_AGENCY_PAYMENTS_SHEET_NAME]) == 1
    )
    assert (
        len([s for s in sheets if s["name"] == SAMPLE_SMALL_MODEL_LAB_SHEET_NAME]) == 1
    )


def test_seed_service_reuse_path_skips_import(tmp_path: Path) -> None:
    """Unit-level check on the service so the reuse branch is pinned even if
    the route wiring changes: the second .seed() call must not touch the
    upload_csv path after both sample sheets exist."""
    from frisket.server.workspace import Workspace

    workspace = Workspace(tmp_path / "ws2")
    created = workspace.create("Service level")
    pid = created["id"]

    service = SampleProjectSeedService(workspace)
    first = service.seed(pid)
    assert first["rows"] == SAMPLE_ARTICLE_ROWS

    calls = {"n": 0}
    original_upload_csv = service._import.upload_csv

    def _guarded_upload_csv(*args, **kwargs):
        calls["n"] += 1
        return original_upload_csv(*args, **kwargs)

    service._import.upload_csv = _guarded_upload_csv  # type: ignore[method-assign]

    second = service.seed(pid)

    assert calls["n"] == 0, "reuse path must not call ImportCsvUploadService.upload_csv"
    assert second["sheet_id"] == first["sheet_id"]
    assert second["rows"] == first["rows"]
