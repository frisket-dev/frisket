from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_clip import ClipSource, cut_and_store_clip


def _wav_bytes(seconds: float, *, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


def _clip_source(blob_hash: str, *, duration_ms: int = 5000) -> ClipSource:
    return ClipSource(
        span_stable_id="span-1",
        blob_hash=blob_hash,
        mime="audio/wav",
        filename="hearing.wav",
        artifact_duration_ms=duration_ms,
        start_ms=1000,
        end_ms=2000,
        label="hearing",
    )


def _new_project(tmp_path: Path, name: str = "lineage") -> Project:
    return Project.create(tmp_path / f"{name}.frisket")


def test_creating_a_clip_records_a_lineage_row(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    source_hash = project.add_blob(
        _wav_bytes(5.0), filename="hearing.wav", mime="audio/wav"
    )
    source = _clip_source(source_hash)

    derived_hash = asyncio.run(
        cut_and_store_clip(project, source, start_ms=1000, end_ms=2000)
    )

    assert derived_hash != source_hash
    # The derived clip landed in the project's own blob store.
    assert project.db.execute(
        "SELECT 1 FROM blobs WHERE hash=?", (derived_hash,)
    ).fetchone()

    edge = project.blob_derivation(derived_hash)
    assert edge is not None
    assert edge["source_hash"] == source_hash
    assert edge["derived_hash"] == derived_hash
    assert edge["op"]

    chain = project.blob_derivation_chain(derived_hash)
    assert len(chain) == 1
    assert chain[0]["source_hash"] == source_hash
    assert chain[0]["params"]["start_ms"] == 1000
    assert chain[0]["params"]["end_ms"] == 2000


def test_lineage_survives_project_reopen(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    source_hash = project.add_blob(
        _wav_bytes(5.0), filename="hearing.wav", mime="audio/wav"
    )
    source = _clip_source(source_hash)
    derived_hash = asyncio.run(
        cut_and_store_clip(project, source, start_ms=1000, end_ms=2000)
    )
    project.close()

    reopened = Project(tmp_path / "lineage.frisket")
    edge = reopened.blob_derivation(derived_hash)
    assert edge is not None
    assert edge["source_hash"] == source_hash
    chain = reopened.blob_derivation_chain(derived_hash)
    assert len(chain) == 1
    assert chain[0]["source_hash"] == source_hash


def test_a_clip_of_a_clip_chains(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    source_hash = project.add_blob(
        _wav_bytes(5.0), filename="hearing.wav", mime="audio/wav"
    )
    first_source = _clip_source(source_hash, duration_ms=5000)
    first_clip_hash = asyncio.run(
        cut_and_store_clip(project, first_source, start_ms=1000, end_ms=2000)
    )

    # Clip the derived clip itself (its own duration is 1000ms: [0,1000)).
    second_source = ClipSource(
        span_stable_id="span-2",
        blob_hash=first_clip_hash,
        mime="audio/wav",
        filename="hearing_clip.wav",
        artifact_duration_ms=1000,
        start_ms=0,
        end_ms=500,
        label="hearing clip",
    )
    second_clip_hash = asyncio.run(
        cut_and_store_clip(project, second_source, start_ms=0, end_ms=500)
    )

    assert second_clip_hash != first_clip_hash
    assert second_clip_hash != source_hash

    # One-hop edge: the second clip's immediate source is the first clip.
    immediate = project.blob_derivation(second_clip_hash)
    assert immediate is not None
    assert immediate["source_hash"] == first_clip_hash

    # The full chain walks both hops back to the ORIGINAL recording.
    chain = project.blob_derivation_chain(second_clip_hash)
    assert [edge["source_hash"] for edge in chain] == [first_clip_hash, source_hash]


def test_recording_derivation_fails_closed_when_source_blob_missing(
    tmp_path: Path,
) -> None:
    project = _new_project(tmp_path)
    # A derived blob DOES exist in the store...
    derived_hash = project.add_blob(
        _wav_bytes(1.0), filename="clip.wav", mime="audio/wav"
    )
    missing_source = "0" * 64

    with pytest.raises(BlobNotFoundError):
        project.record_blob_derivation(
            derived_hash=derived_hash,
            source_hash=missing_source,
            op="ffmpeg_clip",
            params={"start_ms": 0, "end_ms": 500},
        )

    # ...and no dangling lineage row was silently written for it.
    assert project.blob_derivation(derived_hash) is None
    row = project.db.execute(
        "SELECT COUNT(*) AS n FROM blob_derivations WHERE derived_hash=?",
        (derived_hash,),
    ).fetchone()
    assert row["n"] == 0
