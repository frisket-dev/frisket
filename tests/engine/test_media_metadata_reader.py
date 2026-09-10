from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import frisket.engine.executor.media_metadata_read as reader_module
import frisket.ops.media_metadata as metadata
from frisket.actions.types import RowError
from frisket.engine.executor.media_metadata_read import AdmittedMediaMetadataReader
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Project]:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    instance = Project.create(tmp_path / "reader.frisket")
    try:
        yield instance
    finally:
        instance.close()


def _blob(project: Project, width: int = 13) -> str:
    content = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, 7)
    return project.add_blob(content, filename="original.png", mime="image/png")


async def _until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=5)


def test_blank_invalid_missing_and_closed_reads(project: Project) -> None:
    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project)
        try:
            for blank in (None, "", " \t\n"):
                assert await reader.read(blank) is None
            for invalid in ("text", [], 4, {}, {"blob": True}, {"blob": "A" * 64}):
                with pytest.raises(RowError) as caught:
                    await reader.read(invalid)
                assert caught.value.code == "invalid_media_cell"
            with pytest.raises(RowError) as caught:
                await reader.read({"blob": "0" * 64})
            assert caught.value.code == "missing_blob"
            with pytest.raises(TypeError, match="refresh must be boolean"):
                await reader.read(None, refresh=1)  # type: ignore[arg-type]
        finally:
            await reader.aclose()
        assert reader.closed
        await reader.aclose()
        with pytest.raises(RuntimeError, match="reader is closed"):
            await reader.read(None)

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_probe_errors_are_bounded_row_errors(
    project: Project, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    digest = _blob(project)

    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise error_type("private probe details " * 1_000)

    monkeypatch.setattr(reader_module, "extract_media_metadata", fail)

    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project)
        try:
            with pytest.raises(RowError) as caught:
                await reader.read({"blob": digest})
            assert caught.value.code == "metadata_extract_failed"
            assert caught.value.message == "Media metadata extraction failed."
            assert MediaBlobStore(project).media_metadata_cache(digest) is None
        finally:
            await reader.aclose()

    asyncio.run(scenario())


def test_coalescing_retains_each_reference_and_keys_actual_refresh_argument(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _blob(project)
    original_extract = reader_module.extract_media_metadata
    calls: list[dict[str, Any]] = []

    def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(reader_module, "extract_media_metadata", extract)

    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project, preview=True)
        try:
            first, second = await asyncio.gather(
                reader.read(
                    {"blob": digest, "filename": "first.png", "mime": "image/png"}
                ),
                reader.read(
                    {"blob": digest, "filename": "second.jpg", "mime": "image/jpeg"}
                ),
            )
            assert len(calls) == 1
            assert first is not None and second is not None
            assert first["normalized"]["filename"] == "first.png"
            assert second["normalized"]["filename"] == "second.jpg"
            assert first["sources"]["blob"]["claimed_mime"] == "image/png"
            assert second["sources"]["blob"]["claimed_mime"] == "image/jpeg"
            assert all(call["filename"] is None for call in calls)
            assert all(call["claimed_mime"] is None for call in calls)
            # Mutable returned envelopes must not contaminate the cached template.
            first["normalized"]["width_pixels"] = -1
            third = await reader.read({"blob": digest})
            assert third is not None and third["normalized"]["width_pixels"] == 13
            assert third["normalized"]["filename"] == "original.png"
            await reader.read({"blob": digest}, refresh=True)
            await reader.read({"blob": digest}, refresh=True)
            await reader.read({"blob": digest}, refresh=False)
            assert len(calls) == 2
            assert set(reader._completed_templates) == {(digest, False), (digest, True)}
            assert MediaBlobStore(project).media_metadata_cache(digest) is None
        finally:
            await reader.aclose()

    asyncio.run(scenario())


def test_invocation_limits_actual_concurrent_probes_to_eight(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    digests = [_blob(project, width) for width in range(1, 10)]
    original_extract = reader_module.extract_media_metadata
    release = threading.Event()
    eight_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    total = 0

    def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal active, maximum, total
        with lock:
            active += 1
            total += 1
            maximum = max(active, maximum)
            if active == 8:
                eight_started.set()
        try:
            assert release.wait(timeout=10)
            return original_extract(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(reader_module, "extract_media_metadata", extract)

    async def scenario() -> None:
        # The reader submits probes through asyncio.to_thread(). Its shared
        # default executor can have fewer than eight workers on CI runners.
        # Provision capacity above the reader's limit so that limit, rather
        # than the executor, governs how many probes can start.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=9)
        )
        reader = AdmittedMediaMetadataReader(project, preview=True)
        tasks = [
            asyncio.create_task(reader.read({"blob": digest})) for digest in digests
        ]
        try:
            await _until(eight_started.is_set)
            assert total == 8
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(result is not None for result in results)
            assert total == 9 and maximum == 8
        finally:
            release.set()
            await reader.aclose()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_waiter_and_repeated_close_cancellation_hold_blob_until_worker_exits(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _blob(project)
    started = threading.Event()
    cancelled = threading.Event()
    release = threading.Event()
    materialization_released = threading.Event()
    original_materialize = project.materialize_blob
    original_extract = reader_module.extract_media_metadata

    @contextmanager
    def materialize(value: str) -> Iterator[Path]:
        with original_materialize(value) as path:
            try:
                yield path
            finally:
                materialization_released.set()

    def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        assert kwargs["cancel_event"].wait(timeout=10)
        cancelled.set()
        assert release.wait(timeout=10)
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(project, "materialize_blob", materialize)
    monkeypatch.setattr(reader_module, "extract_media_metadata", extract)

    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project)
        waiter = asyncio.create_task(reader.read({"blob": digest}))
        closer: asyncio.Task[None] | None = None
        try:
            await _until(started.is_set)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not materialization_released.is_set()
            assert not cancelled.is_set()
            closer = asyncio.create_task(reader.aclose())
            await _until(cancelled.is_set)
            closer.cancel()
            await asyncio.sleep(0)
            closer.cancel()
            await asyncio.sleep(0)
            assert not closer.done()
            assert not materialization_released.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert materialization_released.is_set()
            assert reader.closed
            assert not reader._futures
            assert MediaBlobStore(project).media_metadata_cache(digest) is None
        finally:
            release.set()
            await reader.aclose()
            await asyncio.gather(
                waiter,
                *([closer] if closer is not None else []),
                return_exceptions=True,
            )

    asyncio.run(scenario())


def test_invocation_cancellation_settles_probe_without_cache_publication(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _blob(project)
    started = threading.Event()
    worker_finished = threading.Event()
    cancel_requested = threading.Event()
    original_extract = reader_module.extract_media_metadata

    def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        assert kwargs["cancel_event"].wait(timeout=10)
        try:
            return original_extract(*args, **kwargs)
        finally:
            worker_finished.set()

    monkeypatch.setattr(reader_module, "extract_media_metadata", extract)

    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project, cancelled=cancel_requested.is_set)
        waiter = asyncio.create_task(reader.read({"blob": digest}))
        try:
            await _until(started.is_set)
            cancel_requested.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, timeout=5)
            assert worker_finished.is_set()
            assert MediaBlobStore(project).media_metadata_cache(digest) is None
            with pytest.raises(asyncio.CancelledError):
                await reader.read(None)
        finally:
            await reader.aclose()
            await asyncio.gather(waiter, return_exceptions=True)

    asyncio.run(scenario())


def test_cancellation_visible_at_probe_completion_prevents_cache_publication(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _blob(project)
    cancel_requested = threading.Event()
    original_extract = reader_module.extract_media_metadata

    def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        envelope = original_extract(*args, **kwargs)
        # The polling await observes a completed worker and must still check
        # invocation cancellation before publishing that worker's result.
        cancel_requested.set()
        return envelope

    monkeypatch.setattr(reader_module, "extract_media_metadata", extract)

    async def scenario() -> None:
        reader = AdmittedMediaMetadataReader(project, cancelled=cancel_requested.is_set)
        try:
            with pytest.raises(asyncio.CancelledError):
                await reader.read({"blob": digest})
            assert cancel_requested.is_set()
            assert not reader._completed_templates
            assert MediaBlobStore(project).media_metadata_cache(digest) is None
        finally:
            await reader.aclose()

    asyncio.run(scenario())
