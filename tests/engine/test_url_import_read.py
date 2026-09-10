from frisket.engine.executor import url_import_read
from frisket.ops import url_import
from frisket.engine.store.media_blobs import MEDIA_ACQUISITION_NAMESPACE
from frisket.ops.ytdlp import DownloadedMedia
import pytest


def test_acquisition_keeps_mixed_transport_order_errors_and_metadata(monkeypatch):
    calls = []

    def direct(url):
        calls.append(("direct", url))
        if url.endswith("broken"):
            return b"", "", "", "download unavailable"
        return b"pdf bytes", "application/pdf", "report.pdf", None

    def ytdlp(url):
        calls.append(("ytdlp", url))
        return DownloadedMedia(
            b"video bytes",
            "video/mp4",
            "clip.mp4",
            duration_seconds=9.5,
            metadata={"title": "A clip", "channel_id": "channel"},
        )

    monkeypatch.setattr(url_import, "download_url", direct)
    monkeypatch.setattr(url_import.ytdlp, "download_media", ytdlp)
    urls = [
        " https://example.com/report.pdf ",
        "not-a-url",
        "https://www.youtube.com/watch?v=abc12345678",
        "https://example.com/broken",
    ]
    rows, kinds, blobs, blob_refs, sources, errors = url_import.acquire_url_records(
        urls
    )
    assert [row["url"] for row in rows] == [url.strip() for url in urls]
    assert calls == [
        ("direct", urls[0].strip()),
        ("ytdlp", urls[2]),
        ("direct", urls[3]),
    ]
    assert kinds == ["file", "video"]
    assert len(blobs) == len(blob_refs) == 2
    assert [source["status"] for source in sources] == [
        "downloaded",
        "error",
        "downloaded",
        "error",
    ]
    assert [error["row_index"] for error in errors] == [2, 4]
    assert blobs[1]["metadata"][MEDIA_ACQUISITION_NAMESPACE] == {
        "title": "A clip",
        "channel_id": "channel",
        "duration_seconds": 9.5,
    }


def test_closed_url_capability_cannot_acquire(monkeypatch):
    from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager

    with AdmittedImportBlobStager() as stager:
        reader = url_import_read.AdmittedUrlImporter(stager)
        reader.close()
        with pytest.raises(ValueError, match="closed"):
            reader.read(["https://example.com/file"])


@pytest.mark.parametrize("first_url", ["not-a-url", "https://example.com/failed"])
def test_failed_inputs_consume_the_shared_url_budget(monkeypatch, first_url):
    from frisket.actions.types import TableError
    from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager

    calls = []

    def download(url):
        calls.append(url)
        return b"", "", "", "download failed"

    monkeypatch.setattr(url_import, "download_url", download)
    with AdmittedImportBlobStager() as stager:
        reader = url_import_read.AdmittedUrlImporter(stager, max_urls=1)
        assert len(reader.read([first_url]).rows) == 1
        with pytest.raises(TableError) as refused:
            reader.read(["https://example.com/excess"])
        assert refused.value.code == "url_limit_exceeded"
        assert calls == ([first_url] if first_url.startswith("https:") else [])
        assert reader.facts[0] == {
            "kind": "url_list",
            "url_count": 1,
            "downloaded": 0,
            "failed": 1,
        }
