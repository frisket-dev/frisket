"""Ingest-facing projection of the v1 media metadata pipeline.

Covers the probe facts stored by download/import/backfill paths and the owned
namespace helpers that persist them.
"""

import shutil
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from frisket.ops.media_metadata import MEDIA_METADATA_FORMAT_REGISTRY
from frisket.ops.media_probe import (
    _INGEST_PROBED_KINDS,
    _ingest_kind,
    probe_for_ingest,
)
from frisket.engine.runner import CostGate, MapRunner
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    backfill_blob_metadata,
    owned_media_metadata_document,
    update_blob_metadata,
)
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


def _transcription_plan(sheet_id):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    request = ActionRequest(
        action_id="media.transcribe",
        scope=SheetRows(sheet_id=sheet_id),
        params={"source": "audio", "engine": "openai/whisper-1"},
        idempotency_key="probe-estimate",
    )
    return _typed_map_rows_plan(
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request)
    )


def _png_header(width: int = 13, height: int = 7) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def _wav(path, seconds: float = 1.25, sample_rate: int = 8000):
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"\x00\x00" * frames)
    return path


def test_image_probe_projects_v1_fields(tmp_path):
    png = tmp_path / "tiny.png"
    png.write_bytes(_png_header(13, 7))
    assert probe_for_ingest(png, mime="image/png") == {
        "kind": "image",
        "size_bytes": len(png.read_bytes()),
        "format": "png",
        "width": 13,
        "height": 7,
    }


@pytest.mark.parametrize(
    ("adapter_format", "canonical_format"),
    [("PNG", "png"), ("png", "png"), ("quicktime", "quicktime")],
)
def test_ingest_probe_canonicalizes_adapter_format(
    tmp_path, monkeypatch, adapter_format, canonical_format
):
    png = tmp_path / "adapter.png"
    png.write_bytes(_png_header())
    monkeypatch.setattr(
        "frisket.ops.media_probe.extract_media_metadata",
        lambda *_args, **_kwargs: {
            "normalized": {"kind": "image", "format": adapter_format}
        },
    )

    assert probe_for_ingest(png, mime="image/png")["format"] == canonical_format


def test_text_like_blobs_record_classification_and_size_only(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<!doctype html><html><title>Example</title></html>")
    assert probe_for_ingest(html, mime="text/html") == {
        "kind": "html",
        "size_bytes": html.stat().st_size,
    }

    rss = tmp_path / "feed.xml"
    rss.write_text("<rss version='2.0'><channel><title>Feed</title></channel></rss>")
    assert probe_for_ingest(rss, mime="application/rss+xml") == {
        "kind": "feed",
        "size_bytes": rss.stat().st_size,
    }

    text = tmp_path / "notes.txt"
    text.write_text("hello\nworld")
    assert probe_for_ingest(text, mime="text/plain") == {
        "kind": "text",
        "size_bytes": text.stat().st_size,
    }


def test_probe_is_non_throwing_for_missing_files(tmp_path):
    assert probe_for_ingest(tmp_path / "gone.mp3", mime="audio/mpeg") == {
        "kind": "audio",
        "probe_error": "file missing",
    }


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_audio_metadata_uses_v1_pipeline(tmp_path):
    wav = _wav(tmp_path / "a.wav", seconds=1.25)
    meta = probe_for_ingest(wav, mime="audio/wav")
    assert meta["kind"] == "audio"
    assert meta["format"] == "wav"
    assert meta["duration_seconds"] == pytest.approx(1.25, abs=0.01)
    assert meta["sample_rate"] == 8000
    assert meta["channels"] == 1


def test_every_registry_extension_reaches_the_probe():
    """Closure: the ingest classifier cannot disagree with the format registry.

    ``_ingest_kind`` decides whether ``extract_media_metadata`` runs AT ALL.
    Its extension sets used to be retyped by hand and had drifted from
    MEDIA_METADATA_FORMAT_REGISTRY by eleven entries, so e.g. a ``.heic``
    photo or a ``.mts`` video arriving without a reliable Content-Type
    classified as ``kind="file"`` and never got duration/dimensions/pages.
    They are derived from the registry now; this goes red if a future
    extension is added to the registry that the ingest path would not probe.
    """
    registry_exts = MEDIA_METADATA_FORMAT_REGISTRY["extension_aliases"]
    unprobed = sorted(
        ext
        for ext in registry_exts
        if _ingest_kind(None, f"sample.{ext}") not in _INGEST_PROBED_KINDS
    )
    assert unprobed == []


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_probe_runs_for_a_registry_extension_without_a_content_type(tmp_path):
    """The probed consequence, end to end: ``.wave`` is the registry's alias
    for WAV, ``mimetypes`` does not know it, and the ingest paths often have
    no server-supplied Content-Type. Before the registry derivation this
    returned ``{"kind": "file", "size_bytes": ...}`` — no duration, ever."""
    audio = _wav(tmp_path / "recording.wave", seconds=1.25)
    meta = probe_for_ingest(audio, mime=None, filename="recording.wave")
    assert meta["kind"] == "audio"
    assert meta["format"] == "wav"
    assert meta["duration_seconds"] == pytest.approx(1.25, abs=0.01)


def test_blob_metadata_persists_and_backfills(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="p")
    try:
        digest = project.add_blob(
            _png_header(4, 3),
            filename="a.png",
            mime="image/png",
            metadata=owned_media_metadata_document(probe={"kind": "image", "width": 4}),
        )
        assert MediaBlobStore(project).probe_metadata(digest)["width"] == 4
        MediaBlobStore(project).replace_probe_metadata(
            digest, {"kind": "image", "width": 4, "height": 3}
        )
        assert MediaBlobStore(project).probe_metadata(digest)["height"] == 3

        blank = project.add_blob(b"hello\nworld", filename="a.txt", mime="text/plain")
        out = backfill_blob_metadata(project)
        assert out["scanned"] == 1
        assert out["updated"] == 1
        assert MediaBlobStore(project).probe_metadata(blank) == {
            "kind": "text",
            "size_bytes": 11,
        }
    finally:
        project.close()


def test_forced_refresh_replaces_only_probe_namespace(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "force-refresh.frisket", name="force")
    try:
        cache = {
            "generation": 7,
            "content_facts_hash": "sha256:" + "a" * 64,
        }
        source_url = "https://media.example/watch/123"
        digest = project.add_blob(
            b"probe input",
            filename="clip.mp4",
            mime="video/mp4",
            source_url=source_url,
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 40.0, "width": 640},
                acquisition={
                    "title": "Acquisition title",
                    "duration_seconds": 42.5,
                    "webpage_url": source_url,
                    "managed_runtime": {"content_hash": "sha256:runtime"},
                },
                owner={"_media_metadata_v1": cache},
            ),
        )

        monkeypatch.setattr(
            "frisket.ops.media_probe.probe_for_ingest",
            lambda *_args, **_kwargs: {
                "kind": "video",
                "duration_seconds": 99.0,
                "width": 1920,
                "height": 1080,
            },
        )

        refreshed = update_blob_metadata(project, digest, force=True)

        assert refreshed["duration_seconds"] == 99.0
        assert refreshed["width"] == 1920
        assert refreshed["height"] == 1080
        store = MediaBlobStore(project)
        assert store.probe_metadata(digest) == refreshed
        assert store.acquisition_metadata(digest) == {
            "title": "Acquisition title",
            "duration_seconds": 42.5,
            "webpage_url": source_url,
            "managed_runtime": {"content_hash": "sha256:runtime"},
        }
        assert store.metadata(digest)["_media_metadata_v1"] == cache
        assert MediaBlobStore(project).blob_row(digest)["source_url"] == source_url
    finally:
        project.close()


def test_file_import_stores_probe_metadata_and_identity_cell(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Meta"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/files",
        files=[("files", ("tiny.png", _png_header(9, 5), "image/png"))],
    )
    assert response.status_code == 200, response.text
    sheet_id = response.json()["sheet_id"]
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    cols = {c["name"]: c for c in data["columns"]}
    cell = data["rows"][0]["cells"][str(cols["media"]["id"])]
    assert set(cell) == {"blob", "mime", "filename"}
    assert cell["filename"] == "tiny.png"
    assert cell["mime"] == "image/png"
    assert cols["size"]["format"] == "filesize"
    assert data["rows"][0]["cells"][str(cols["size"]["id"])] == len(_png_header(9, 5))

    project = client.app.state.workspace.get(pid)
    metadata = MediaBlobStore(project).probe_metadata(cell["blob"])
    assert metadata["kind"] == "image"
    assert metadata["width"] == 9
    assert metadata["height"] == 5


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_billable_transcribe_estimate_uses_blob_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-estimate-key")
    project = Project.create(tmp_path / "t.frisket", name="t")
    try:
        wav = _wav(tmp_path / "a.wav", seconds=2.0)
        digest = project.add_blob(wav.read_bytes(), filename="a.wav", mime="audio/wav")
        with project.materialize_blob(digest) as path:
            metadata = probe_for_ingest(path, mime="audio/wav", digest=digest)
        MediaBlobStore(project).replace_probe_metadata(digest, metadata)
        sheet = project.add_sheet("audio")
        cols = {"audio": project.add_column(sheet, "audio", type="audio")}
        project.add_rows(
            sheet,
            [{"audio": {"blob": digest, "mime": "audio/wav", "filename": "a.wav"}}],
            cols,
        )

        plan = _transcription_plan(sheet)
        est = MapRunner(
            project, router=None, authority=UnroutedOnlyAuthority(project)
        ).estimate(plan.spec_dict(), program=plan.program)
        assert est["cost"] == pytest.approx(0.0002)
        assert est["cost_source"] == "pricing_data"
        assert est["engine"] == "openai/whisper-1"
        assert est["pricing_key"] == "openai/whisper-1.audio_second"
        assert est["audio_seconds"] == pytest.approx(2.0, abs=0.01)
        assert "provider_cost_usd" not in est
        assert "units" not in est
    finally:
        project.close()


def test_billable_provider_transcribe_unknown_without_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-estimate-key")
    project = Project.create(tmp_path / "t.frisket", name="t")
    try:
        digest = project.add_blob(
            b"not audio",
            filename="a.bin",
            mime="application/octet-stream",
        )
        sheet = project.add_sheet("audio")
        cols = {"audio": project.add_column(sheet, "audio", type="file")}
        project.add_rows(sheet, [{"audio": {"blob": digest}}], cols)
        plan = _transcription_plan(sheet)
        runner = MapRunner(
            project,
            router=None,
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        est = runner.estimate(plan.spec_dict(), program=plan.program)
        assert est["cost"] is None
        assert est["cost_source"] == "unknown"
        with pytest.raises(CostGate):
            runner.prepare_run(plan.spec_dict(), program=plan.program)
    finally:
        project.close()


def test_metadata_backfill_endpoint_updates_blank_blobs(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Backfill"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    digest = project.add_blob(b"hello\nworld", filename="a.txt", mime="text/plain")
    assert MediaBlobStore(project).metadata(digest) == {}
    response = client.post(f"/api/projects/{pid}/blobs/metadata/backfill", json={})
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1
    assert MediaBlobStore(project).metadata(digest) == owned_media_metadata_document(
        probe={"kind": "text", "size_bytes": 11}
    )
