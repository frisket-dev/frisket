from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import frisket.ops.media_metadata as metadata
import frisket.engine.executor.media_metadata_read as reader_module
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor import action_lifecycle
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.ai.llm import ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.runner import MapRunner
from frisket.engine.runner.result_generations import declare_bound_run_outputs
from frisket.sdk.replay import (
    output_column_result_value_hash,
    output_column_value_hash,
)
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MEDIA_METADATA_CACHE_NAMESPACE,
    MediaBlobStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import (
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


def _png_header(width: int = 13, height: int = 7) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def _action(
    *,
    sheet_id: int,
    row_ids: list[int],
    prefix: str,
    key: str,
    mode: str = "object",
    refresh: bool = False,
    input_column: str = "asset",
    replace_existing: bool = False,
) -> dict[str, Any]:
    return {
        "action_id": "media.extract_metadata",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {
            "source": input_column,
            "output_mode": mode,
            "refresh": refresh,
        },
        "output_names": {
            field["role"]: field["name"] for field in _fields(mode, prefix)
        },
        "replace_existing": replace_existing,
        "idempotency_key": key,
    }


def _fields(mode: str, prefix: str) -> list[dict[str, Any]]:
    registered = ACTION_REGISTRY.get("media.extract_metadata")
    params = registered.definition.run.params_model.model_validate(
        {"source": "asset", "output_mode": mode}
    )
    return [
        {
            "role": field.key,
            "name": prefix if mode == "object" else f"{prefix}_{field.key}",
            "column_type": field.column_type,
            "format": field.format,
            "schema": dict(field.schema),
        }
        for field in registered.definition.run.resolve_output_fields(params)
    ]


def _column(project: Project, sheet_id: int, name: str):
    row = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=?", (sheet_id, name)
    ).fetchone()
    assert row is not None, name
    return row


def _single_image_sheet(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("Assets")
    input_id = project.add_column(sheet_id, "asset", type="image")
    digest = project.add_blob(
        _png_header(11, 7), filename="asset.png", mime="image/png"
    )
    row_id = project.add_rows(
        sheet_id,
        [{"asset": media_cell(digest, filename="asset.png", mime="image/png")}],
        {"asset": input_id},
    )[0]
    return sheet_id, row_id


def _disable_optional_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)


def _stub_successful_image_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        metadata, "_resolve_exiftool_path", lambda: Path("/fake/exiftool")
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    monkeypatch.setattr(metadata, "_tool_version", lambda *_args: "13.59")
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(
                [{"File:FileType": "PNG", "File:MIMEType": "image/png"}]
            ).encode(),
            stderr=b"",
        ),
    )


def test_receipt_counts_and_published_envelopes_retain_row_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "diagnostics.frisket")
    try:
        sheet_id, valid_row = _single_image_sheet(project)
        input_id = int(_column(project, sheet_id, "asset")["id"])
        bad_row = project.add_rows(
            sheet_id, [{"asset": "not a media cell"}], {"asset": input_id}
        )[0]
        _disable_optional_adapters(monkeypatch)
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[valid_row, bad_row],
                prefix="meta",
                key="metadata-diagnostics",
            ),
            project_id="metadata-diagnostics",
        )
        assert result.status == "partial", result.errors
        details_id = int(_column(project, sheet_id, "meta")["id"])
        values = project.get_values(sheet_id, details_id)
        assert values[valid_row]["normalized"]["probe_status"] == "partial"
        assert any(
            warning["code"] == "missing_dependency"
            for warning in values[valid_row]["warnings"]
        )
        errors = RunResultStore(project).row_error_summary(result.run_id)
        assert errors["total_failed_rows"] == 1
        assert errors["groups"][0]["code"] == "invalid_media_cell"
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
        counts = next(
            item["ref"]
            for item in receipt["evidence"]
            if item["ref"]["kind"] == "map_rows_run_counts"
        )
        assert counts["total_rows"] == 2
        assert counts["completed_rows"] == 2
        assert counts["failed_rows"] == 1
        assert counts["failed_row_ids"] == [bad_row]
    finally:
        project.close()


def test_cache_bypass_warning_rebounds_a_near_limit_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _png_header(17, 11)
    path = tmp_path / "near-limit.png"
    path.write_bytes(payload)
    _stub_successful_image_adapter(monkeypatch)
    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename="near-limit.png",
        claimed_mime="image/png",
    )
    xmp = envelope["sources"]["xmp"]
    pad_index = 0
    while len(metadata.canonical_json_bytes(envelope)) <= (metadata.MAX_DETAILS_BYTES):
        xmp[f"xmp.pad_{pad_index:02d}"] = "x" * metadata.MAX_SOURCE_STRING_BYTES
        pad_index += 1
    last_key = f"xmp.pad_{pad_index - 1:02d}"
    low = 0
    high = metadata.MAX_SOURCE_STRING_BYTES
    while low < high:
        middle = (low + high + 1) // 2
        xmp[last_key] = "x" * middle
        if len(metadata.canonical_json_bytes(envelope)) <= (metadata.MAX_DETAILS_BYTES):
            low = middle
        else:
            high = middle - 1
    xmp[last_key] = "x" * low
    assert len(metadata.canonical_json_bytes(envelope)) <= (metadata.MAX_DETAILS_BYTES)

    warning = {
        "code": "internal_error",
        "subtype": "cache_write_bypassed",
        "source": "cache",
        "message": (
            "Metadata was extracted but the shared blob cache could not be updated."
        ),
    }
    naive = copy.deepcopy(envelope)
    naive["warnings"].append(warning)
    assert len(metadata.canonical_json_bytes(naive)) > (metadata.MAX_DETAILS_BYTES)

    bounded = reader_module._cache_bypass_warning(envelope)

    assert len(metadata.canonical_json_bytes(bounded)) <= (metadata.MAX_DETAILS_BYTES)
    assert any(
        item.get("subtype") == "cache_write_bypassed" for item in bounded["warnings"]
    )


def test_oversized_cache_proposal_bypasses_cache_without_failing_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "oversized-cache.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        monkeypatch.setattr(
            reader_module,
            "cache_entry_from_envelope",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("media metadata cache facts exceed the cache bound")
            ),
        )

        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-oversized-cache",
            ),
            project_id="metadata-oversized-cache",
        )

        assert result.status == "completed", result.errors
        output = _column(project, sheet_id, "meta")
        value = project.get_values(sheet_id, int(output["id"]), row_ids=[row_id])[
            row_id
        ]
        assert any(
            warning.get("subtype") == "cache_write_bypassed"
            for warning in value["warnings"]
        )
        digest = project.db.execute("SELECT hash FROM blobs").fetchone()[0]
        assert MediaBlobStore(project).media_metadata_cache(str(digest)) is None
    finally:
        project.close()


def test_cancelled_probe_teardown_survives_repeat_outer_cancellation() -> None:
    async def scenario() -> None:
        cancel_event = threading.Event()
        worker_finished = threading.Event()

        def worker() -> None:
            assert cancel_event.wait(timeout=2)
            # Bespoke asyncio/threading scaffolding for this recipe module
            # (not Queue/Worker/limiter), so no clock-injection seam applies.
            # realtime: margin over cancel_again's asyncio.sleep(0.01) so the repeat cancellation genuinely races the worker still running
            time.sleep(0.05)
            worker_finished.set()

        probe = asyncio.create_task(asyncio.to_thread(worker))

        async def owner() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancel_event.set()
                await reader_module._settle(probe)
                raise

        owner_task = asyncio.create_task(owner())
        await asyncio.sleep(0)
        owner_task.cancel()

        async def cancel_again() -> None:
            await asyncio.sleep(0.01)
            owner_task.cancel()

        repeated = asyncio.create_task(cancel_again())
        with pytest.raises(asyncio.CancelledError):
            await owner_task
        await repeated
        assert worker_finished.is_set()
        assert probe.done()

    asyncio.run(scenario())


def test_completed_template_lru_obeys_recency_count_and_byte_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader_module, "_MAX_RUN_TEMPLATE_CACHE_ENTRIES", 2)
    monkeypatch.setattr(reader_module, "_MAX_RUN_TEMPLATE_CACHE_BYTES", 7)
    monkeypatch.setattr(
        reader_module,
        "media_metadata_cache_facts_size",
        lambda template: int(template["weight"]),
    )
    monkeypatch.setattr(
        reader_module,
        "overlay_reference_evidence",
        lambda template, **_kwargs: template,
    )
    project = Project.create(tmp_path / "lru.frisket")
    try:
        keys = [(project.add_blob(value), False) for value in (b"a", b"b", b"c", b"d")]

        async def scenario():
            reader = reader_module.AdmittedMediaMetadataReader(project)
            try:
                reader._remember(keys[0], {"weight": 3})
                reader._remember(keys[1], {"weight": 3})
                assert await reader.read({"blob": keys[0][0]}) == {"weight": 3}
                reader._remember(keys[2], {"weight": 3})
                assert list(reader._completed_templates) == [keys[0], keys[2]]
                assert reader._completed_template_bytes == 6
                # An oversized value may be returned, but must not violate the
                # invocation cache's hard byte budget.
                reader._remember(keys[3], {"weight": 8})
                assert list(reader._completed_templates) == [keys[0], keys[2]]
                assert reader._completed_template_bytes == 6
            finally:
                await reader.aclose()

        asyncio.run(scenario())
    finally:
        project.close()


def test_completed_digest_future_is_evicted_without_deleting_replacement() -> None:
    async def scenario() -> None:
        reader = reader_module.AdmittedMediaMetadataReader(SimpleNamespace())
        key = ("a" * 64, False)
        completed = asyncio.get_running_loop().create_future()
        completed.set_result({"normalized": {"blob_hash": key[0]}})
        reader._futures[key] = completed
        reader._forget_future(key, completed)
        assert key not in reader._futures

        replacement = asyncio.get_running_loop().create_future()
        reader._futures[key] = replacement
        reader._forget_future(key, completed)
        assert reader._futures[key] is replacement
        replacement.cancel()
        await reader.aclose()

    asyncio.run(scenario())


def test_preview_dedupes_sequential_references_without_populating_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "preview.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(17, 11), filename="preview.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "asset": media_cell(
                        digest, filename="preview-a.png", mime="image/png"
                    )
                },
                {
                    "asset": media_cell(
                        digest, filename="preview-b.png", mime="image/png"
                    )
                },
            ],
            {"asset": input_id},
        )
        _stub_successful_image_adapter(monkeypatch)
        monkeypatch.setattr(MapRunner, "_row_worker_count", lambda *_args: 1)
        original_extract = reader_module.extract_media_metadata
        calls = 0

        def counted_extract(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted_extract)
        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("columns", "ops", "runs", "results", "receipts")
        }

        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(
                _action(
                    sheet_id=sheet_id,
                    row_ids=row_ids,
                    prefix="meta",
                    key="metadata-preview",
                )
            ),
        )
        preview = asyncio.run(
            MapRunner(
                project,
                ModelRouter(cache=None, cache_mode="off"),
                authority=UnroutedOnlyAuthority(project),
            ).preview(plan.spec_dict(), program=plan.program)
        )

        first = preview.values[row_ids[0]]["meta"]["value"]
        second = preview.values[row_ids[1]]["meta"]["value"]
        assert calls == 1
        assert first["normalized"]["kind"] == "image"
        assert first["normalized"]["width_pixels"] == 17
        assert first["normalized"]["filename"] == "preview-a.png"
        assert second["normalized"]["filename"] == "preview-b.png"
        after = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in before
        }
        assert after == before
        assert MediaBlobStore(project).media_metadata_cache(digest) is None
    finally:
        project.close()


def test_object_mode_dedupes_digest_overlays_references_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "object-cache.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(19, 12), filename="original.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"asset": media_cell(digest, mime="image/png", filename="first.png")},
                {"asset": media_cell(digest, mime="video/mp4", filename="renamed.jpg")},
            ],
            {"asset": input_id},
        )
        _stub_successful_image_adapter(monkeypatch)
        original_extract = reader_module.extract_media_metadata
        calls = 0

        def counted_extract(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted_extract)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="meta",
                key="metadata-object-first",
            ),
            project_id="metadata-object-cache",
        )
        assert first.status == "completed", first.errors
        assert calls == 1

        output = _column(project, sheet_id, "meta")
        values = project.get_values(sheet_id, int(output["id"]), row_ids=row_ids)
        assert values[row_ids[0]]["normalized"]["filename"] == "first.png"
        assert values[row_ids[1]]["normalized"]["filename"] == "renamed.jpg"
        assert values[row_ids[0]]["normalized"]["blob_hash"] == f"sha256:{digest}"
        cache = MediaBlobStore(project).media_metadata_cache(digest)
        assert cache is not None
        assert cache["generation"] == 1

        def unexpected_probe(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("compatible shared cache was not reused")

        monkeypatch.setattr(reader_module, "extract_media_metadata", unexpected_probe)
        cached = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="cached",
                key="metadata-object-cached",
            ),
            project_id="metadata-object-cache",
        )
        assert cached.status == "completed", cached.errors
        cached_column = _column(project, sheet_id, "cached")
        assert (
            project.get_values(sheet_id, int(cached_column["id"]), row_ids=row_ids)
            == values
        )
    finally:
        project.close()


def test_cache_namespace_copied_to_another_blob_is_reprobed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "copied-cache.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        first_digest = project.add_blob(
            _png_header(31, 13), filename="first.png", mime="image/png"
        )
        second_digest = project.add_blob(
            _png_header(7, 5), filename="second.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "asset": media_cell(
                        first_digest, filename="first.png", mime="image/png"
                    )
                },
                {
                    "asset": media_cell(
                        second_digest, filename="second.png", mime="image/png"
                    )
                },
            ],
            {"asset": input_id},
        )
        _stub_successful_image_adapter(monkeypatch)
        adapter_title = {"value": "First blob title"}

        def exiftool_result(*_args, **_kwargs):
            return metadata._CommandResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "File:FileType": "PNG",
                            "File:MIMEType": "image/png",
                            "EXIF:Title": adapter_title["value"],
                        }
                    ]
                ).encode(),
                stderr=b"",
            )

        monkeypatch.setattr(metadata, "_run_bounded", exiftool_result)
        seeded = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_ids[0]],
                prefix="first_meta",
                key="metadata-copied-cache-seed",
            ),
            project_id="metadata-copied-cache",
        )
        assert seeded.status == "completed", seeded.errors
        first_output = _column(project, sheet_id, "first_meta")
        first_value = project.get_values(
            sheet_id, int(first_output["id"]), row_ids=[row_ids[0]]
        )[row_ids[0]]
        assert first_value["normalized"]["title"] == "First blob title"
        assert first_value["normalized"]["width_pixels"] == 31

        store = MediaBlobStore(project)
        copied = store.media_metadata_cache(first_digest)
        assert copied is not None
        store.merge_metadata(
            second_digest,
            {MEDIA_METADATA_CACHE_NAMESPACE: copy.deepcopy(copied)},
        )
        project.db.commit()
        assert store.media_metadata_cache(second_digest) is None

        adapter_title["value"] = "Second blob title"
        original_extract = reader_module.extract_media_metadata
        calls = 0

        def counted_extract(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted_extract)
        extracted = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_ids[1]],
                prefix="second_meta",
                key="metadata-copied-cache-target",
            ),
            project_id="metadata-copied-cache",
        )
        assert extracted.status == "completed", extracted.errors
        assert calls == 1

        second_output = _column(project, sheet_id, "second_meta")
        second_value = project.get_values(
            sheet_id, int(second_output["id"]), row_ids=[row_ids[1]]
        )[row_ids[1]]
        assert second_value["normalized"]["title"] == "Second blob title"
        assert second_value["normalized"]["title"] != first_value["normalized"]["title"]
        assert second_value["normalized"]["width_pixels"] == 7
        assert second_value["normalized"]["height_pixels"] == 5
        assert second_value["normalized"]["blob_hash"] == f"sha256:{second_digest}"
        second_cache = store.media_metadata_cache(second_digest)
        assert second_cache is not None
        assert second_cache["generation"] == 1
    finally:
        project.close()


def test_refresh_reprobes_once_then_reuses_cache_for_sequential_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "refresh.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(9, 5), filename="asset.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"asset": media_cell(digest, filename="a.png", mime="image/png")},
                {"asset": media_cell(digest, filename="b.png", mime="image/png")},
            ],
            {"asset": input_id},
        )
        _stub_successful_image_adapter(monkeypatch)
        monkeypatch.setattr(MapRunner, "_row_worker_count", lambda *_args: 1)
        original_extract = reader_module.extract_media_metadata
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="first",
                key="metadata-refresh-first",
            ),
            project_id="metadata-refresh",
        )
        refreshed = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="refreshed",
                key="metadata-refresh-second",
                refresh=True,
            ),
            project_id="metadata-refresh",
        )
        assert first.status == refreshed.status == "completed"
        assert calls == 2
        assert MediaBlobStore(project).media_metadata_cache(digest)["generation"] == 2
        refreshed_column = _column(project, sheet_id, "refreshed")
        values = project.get_values(
            sheet_id, int(refreshed_column["id"]), row_ids=row_ids
        )
        assert values[row_ids[0]]["normalized"]["filename"] == "a.png"
        assert values[row_ids[1]]["normalized"]["filename"] == "b.png"
    finally:
        project.close()


def test_nonreusable_refresh_memoizes_sequential_duplicates_and_retries_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "refresh-nonreusable.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(9, 5), filename="asset.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"asset": media_cell(digest, filename="a.png", mime="image/png")},
                {"asset": media_cell(digest, filename="b.png", mime="image/png")},
            ],
            {"asset": input_id},
        )
        monkeypatch.setattr(MapRunner, "_row_worker_count", lambda *_args: 1)

        _stub_successful_image_adapter(monkeypatch)
        seeded = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_ids[0]],
                prefix="seed",
                key="metadata-refresh-nonreusable-seed",
            ),
            project_id="metadata-refresh-nonreusable",
        )
        assert seeded.status == "completed", seeded.errors
        old_cache = MediaBlobStore(project).media_metadata_cache(digest)
        assert old_cache is not None

        # A refresh that cannot satisfy the reusable-cache contract is still
        # returned to every reference in this execution. Duplicate rows reuse
        # that completed template rather than running another bounded probe or
        # falling back to the older compatible persistent cache entry.
        _disable_optional_adapters(monkeypatch)
        original_extract = reader_module.extract_media_metadata
        calls = 0

        def counted_extract(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted_extract)
        refreshed = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="degraded",
                key="metadata-refresh-nonreusable-run",
                refresh=True,
            ),
            project_id="metadata-refresh-nonreusable",
        )
        assert refreshed.status == "completed", refreshed.errors
        assert calls == 1
        assert MediaBlobStore(project).media_metadata_cache(digest) == old_cache
        output = _column(project, sheet_id, "degraded")
        values = project.get_values(sheet_id, int(output["id"]), row_ids=row_ids)
        assert {values[row_id]["normalized"]["probe_status"] for row_id in row_ids} == {
            "partial"
        }
        assert values[row_ids[0]]["normalized"]["filename"] == "a.png"
        assert values[row_ids[1]]["normalized"]["filename"] == "b.png"

        # Run-local reuse must not become a cross-action retry cache.
        _stub_successful_image_adapter(monkeypatch)
        recovered = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="recovered",
                key="metadata-refresh-nonreusable-retry",
                refresh=True,
            ),
            project_id="metadata-refresh-nonreusable",
        )
        assert recovered.status == "completed", recovered.errors
        assert calls == 2
        assert MediaBlobStore(project).media_metadata_cache(digest) != old_cache
    finally:
        project.close()


def test_columns_mode_publishes_23_typed_projections_and_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "columns.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(23, 9), filename="asset.png", mime="image/png"
        )
        row_id = project.add_rows(
            sheet_id,
            [{"asset": media_cell(digest, mime="image/png", filename="asset.png")}],
            {"asset": input_id},
        )[0]
        _disable_optional_adapters(monkeypatch)

        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="asset_meta",
                key="metadata-columns",
                mode="columns",
            ),
            project_id="metadata-columns",
        )
        assert result.status == "completed", result.errors

        names = [f"asset_meta_{role}" for role in metadata.MEDIA_METADATA_ROLES]
        assert len(names) == 23
        details_column = _column(project, sheet_id, "asset_meta_details")
        details = project.get_values(
            sheet_id, int(details_column["id"]), row_ids=[row_id]
        )[row_id]
        for role, name in zip(metadata.MEDIA_METADATA_ROLES, names, strict=True):
            projected = _column(project, sheet_id, name)
            value = project.get_values(
                sheet_id, int(projected["id"]), row_ids=[row_id]
            )[row_id]
            assert value == details["normalized"][role], role
    finally:
        project.close()


def test_object_and_columns_details_are_byte_equal_and_retain_rich_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "details-equivalence.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(23, 9), filename="asset.png", mime="image/png"
        )
        row_id = project.add_rows(
            sheet_id,
            [{"asset": media_cell(digest, mime="image/png", filename="asset.png")}],
            {"asset": input_id},
        )[0]
        _stub_successful_image_adapter(monkeypatch)
        monkeypatch.setattr(
            metadata,
            "_run_bounded",
            lambda *_args, **_kwargs: metadata._CommandResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "File:FileType": "PNG",
                            "File:MIMEType": "image/png",
                            "EXIF:GPSLatitude": "40.7",
                            "EXIF:SerialNumber": "001234",
                            "XMP:OriginalPath": (
                                "/Users/operator/private/original.png"
                            ),
                            "XMP:Description": (
                                "https://example.test/object?X-Amz-Credential=secret"
                            ),
                        }
                    ]
                ).encode(),
                stderr=b"",
            ),
        )

        object_result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="object_meta",
                key="metadata-details-object",
            ),
            project_id="metadata-details-equivalence",
        )
        columns_result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="split_meta",
                key="metadata-details-columns",
                mode="columns",
            ),
            project_id="metadata-details-equivalence",
        )
        assert object_result.status == columns_result.status == "completed"

        object_column = _column(project, sheet_id, "object_meta")
        details_column = _column(project, sheet_id, "split_meta_details")
        object_value = project.get_values(
            sheet_id, int(object_column["id"]), row_ids=[row_id]
        )[row_id]
        details_value = project.get_values(
            sheet_id, int(details_column["id"]), row_ids=[row_id]
        )[row_id]
        assert metadata.canonical_json_bytes(
            details_value
        ) == metadata.canonical_json_bytes(object_value)

        assert details_value["sources"]["exif"] == {
            "exif.gps_latitude": 40.7,
            "exif.serial_number": "001234",
        }
        assert details_value["sources"]["xmp"] == {
            "xmp.description": ("https://example.test/object?X-Amz-Credential=secret"),
            "xmp.original_path": "/Users/operator/private/original.png",
        }
        cache = MediaBlobStore(project).media_metadata_cache(digest)
        assert cache is not None
        assert (
            cache["content_facts"]["envelope"]["sources"]["exif"]
            == (details_value["sources"]["exif"])
        )
        assert (
            cache["content_facts"]["envelope"]["sources"]["xmp"]
            == (details_value["sources"]["xmp"])
        )
    finally:
        project.close()


def test_columns_mode_rejects_unrelated_ai_sibling_even_with_overwrite_intent(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "columns-collision.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        row_id = project.add_rows(
            sheet_id,
            [{"asset": None}],
            {"asset": input_id},
        )[0]
        sibling_id = project.add_column(
            sheet_id,
            "meta_title",
            type="number",
            ai_generated=True,
            format="filesize",
        )
        action = _action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            prefix="meta",
            key="metadata-columns-collision",
            mode="columns",
        )
        action["replace_existing"] = True

        result = run_action_spec(
            project,
            action,
            project_id="metadata-columns-collision",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "output_column_exists"
        assert result.errors[0].details == {"columns": ["meta_title"]}
        sibling = project.get_column(sibling_id)
        assert sibling["type"] == "number"
        assert sibling["format"] == "filesize"
        assert {column["name"] for column in project.columns(sheet_id)} == {
            "asset",
            "meta_title",
        }
    finally:
        project.close()


def test_metadata_prefix_is_not_a_namespace_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "prefix-coexistence.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        custom_id = project.add_column(sheet_id, "meta_custom", type="text")
        _stub_successful_image_adapter(monkeypatch)

        columns_result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-prefix-columns",
                mode="columns",
            ),
            project_id="metadata-prefix-coexistence",
        )
        assert columns_result.status == "completed", columns_result.errors

        object_result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-prefix-object",
                mode="object",
            ),
            project_id="metadata-prefix-coexistence",
        )
        assert object_result.status == "completed", object_result.errors
        assert _column(project, sheet_id, "meta")["current_run_id"] == (
            object_result.run_id
        )
        assert _column(project, sheet_id, "meta_title")["current_run_id"] == (
            columns_result.run_id
        )
        assert project.get_column(custom_id)["name"] == "meta_custom"
    finally:
        project.close()


def test_raw_maprunner_cannot_launch_metadata_without_action_lifecycle(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "raw-runner-gate.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        output_id = project.add_column(
            sheet_id,
            "meta",
            type="json",
            ai_generated=True,
        )
        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("columns", "ops", "runs", "receipts")
        }

        with pytest.raises(
            ValueError,
            match="unknown action kind 'media.extract_metadata'",
        ):
            asyncio.run(
                MapRunner(
                    project, ModelRouter(), authority=UnroutedOnlyAuthority(project)
                ).run(
                    {
                        "action_kind": "media.extract_metadata",
                        "sheet_id": sheet_id,
                        "input_column": "asset",
                        "input_columns": ["asset"],
                        "row_ids": [row_id],
                        "output_mode": "object",
                        "output_prefix": "meta",
                        "refresh": False,
                    },
                    confirmed=True,
                )
            )

        assert {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in before
        } == before
        assert project.get_column(output_id)["current_run_id"] is None
    finally:
        project.close()


def test_atomic_metadata_prepare_failure_rolls_back_family_op_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "atomic-prepare.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("columns", "ops", "runs", "receipts")
        }

        def fail_start_run(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("forced metadata run preparation failure")

        monkeypatch.setattr(RunResultStore, "start_run", fail_start_run)
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-atomic-prepare-failure",
            ),
            project_id="metadata-atomic-prepare-failure",
        )

        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["map_rows_failed"]
        assert {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in before
        } == before
        assert [column["name"] for column in project.columns(sheet_id)] == ["asset"]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_metadata_resume_never_recreates_changed_claimed_outputs(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "atomic-resume-family.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(
                _action(
                    sheet_id=sheet_id,
                    row_ids=[row_id],
                    prefix="meta",
                    key="metadata-resume",
                )
            ),
        )
        spec = plan.spec_dict()
        runner = MapRunner(
            project,
            ModelRouter(),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        claim_token = "claim:metadata-resume"
        claims = OutputColumnClaimStore(project)
        acquired, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["meta"],
            action_kind="media.extract_metadata",
            claim_token=claim_token,
            details={"output_fields": list(plan.output_fields)},
        )
        assert conflict is None and len(acquired) == 1
        prepared = runner.prepare_run(spec, program=plan.program, confirmed=True)
        assert (
            claims.bind_to_run(
                claim_token=claim_token,
                run_id=prepared.run_id,
                expected_output_names=["meta"],
            )
            == 1
        )
        declare_bound_run_outputs(
            project,
            run_id=prepared.run_id,
            output_fields=list(plan.output_fields),
            program=plan.program,
            claim_token=claim_token,
        )
        output = _column(project, sheet_id, "meta")
        output_id = int(output["id"])
        project.db.execute(
            "UPDATE columns SET name='detached_meta' WHERE id=?", (output_id,)
        )
        project.db.commit()
        column_count = project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0]

        with pytest.raises(
            GenerationStateError,
            match="changed its output descriptors",
        ):
            asyncio.run(
                runner.run(
                    spec,
                    program=plan.program,
                    confirmed=True,
                    resume_run_id=prepared.run_id,
                    claim_token=claim_token,
                )
            )

        assert project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0] == (
            column_count
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='meta'",
                (sheet_id,),
            ).fetchone()[0]
            == 0
        )
        assert project.get_column(output_id)["name"] == "detached_meta"
    finally:
        project.close()


def test_all_failed_metadata_family_stays_visible_and_reuses_after_source_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "all-failed-retry.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        row_id = project.add_rows(
            sheet_id,
            [{"asset": "not-a-media-cell"}],
            {"asset": input_id},
        )[0]
        _stub_successful_image_adapter(monkeypatch)

        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-all-failed-first",
            ),
            project_id="metadata-all-failed-retry",
        )
        assert first.status == "failed"
        output = _column(project, sheet_id, "meta")
        output_id = int(output["id"])
        assert int(output["hidden"]) == 0
        assert int(output["current_run_id"]) == first.run_id
        assert first.receipt_id is not None

        digest = project.add_blob(
            _png_header(21, 12), filename="fixed.png", mime="image/png"
        )
        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": input_id,
                    "value": media_cell(
                        digest,
                        filename="fixed.png",
                        mime="image/png",
                    ),
                }
            ],
            label="fix metadata source",
        )

        second = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-all-failed-second",
                replace_existing=True,
            ),
            project_id="metadata-all-failed-retry",
        )
        assert second.status == "completed", second.errors
        reused = _column(project, sheet_id, "meta")
        assert int(reused["id"]) == output_id
        assert int(reused["hidden"]) == 0
        assert int(reused["current_run_id"]) == first.run_id
    finally:
        project.close()


def test_direct_cancelled_metadata_family_has_receipt_and_reuses_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "direct-cancel-retry.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        cancel_deps = ExecutorDeps(
            map_runner_factory=lambda p, router: MapRunner(
                p,
                router or ModelRouter(),
                concurrency=1,
                should_cancel=lambda _run_id: True,
                allow_action_lifecycle_only_recipes=True,
                authority=UnroutedOnlyAuthority(p),
            )
        )

        first_action = _action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            prefix="meta",
            key="metadata-direct-cancel-first",
        )
        first = run_action_spec(
            project,
            first_action,
            project_id="metadata-direct-cancel-retry",
            deps=cancel_deps,
        )
        assert first.status == "cancelled", first.errors
        output = _column(project, sheet_id, "meta")
        output_id = int(output["id"])
        assert int(output["hidden"]) == 0
        receipt = project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        assert receipt is not None and receipt["status"] == "cancelled"

        run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        same_key = run_action_spec(
            project,
            first_action,
            project_id="metadata-direct-cancel-retry",
        )
        assert same_key.status == "cancelled", same_key.errors
        assert same_key.run_id == first.run_id
        assert same_key.receipt_id == first.receipt_id
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == (
            run_count
        )

        second = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-direct-cancel-second",
                replace_existing=True,
            ),
            project_id="metadata-direct-cancel-retry",
        )
        assert second.status == "completed", second.errors
        assert int(_column(project, sheet_id, "meta")["id"]) == output_id
    finally:
        project.close()


def test_metadata_family_reuse_tracks_source_id_across_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "source-rename.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        source_id = int(_column(project, sheet_id, "asset")["id"])
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-source-rename-first",
            ),
            project_id="metadata-source-rename",
        )
        assert first.status == "completed", first.errors
        output_id = int(_column(project, sheet_id, "meta")["id"])

        project.db.execute(
            "UPDATE columns SET name='renamed_asset' WHERE id=?", (source_id,)
        )
        project.db.commit()
        second = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-source-rename-second",
                replace_existing=True,
                input_column="renamed_asset",
            ),
            project_id="metadata-source-rename",
        )

        assert second.status == "completed", second.errors
        assert int(_column(project, sheet_id, "meta")["id"]) == output_id
        stored = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (second.receipt_id,)
        ).fetchone()
        receipt = json.loads(str(stored["body"]))
        source_ref = next(
            item["ref"]
            for item in receipt["inputs"]
            if item["ref"]["kind"] == "source_column"
        )
        assert source_ref["name"] == "renamed_asset"
        assert source_ref["column_id"] == source_id
    finally:
        project.close()


@pytest.mark.parametrize(
    ("mode", "refresh"),
    [("object", False), ("columns", True)],
)
def test_same_prefix_new_key_requires_explicit_replacement_of_complete_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    refresh: bool,
) -> None:
    project = Project.create(tmp_path / f"family-rerun-{mode}.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{mode}-first",
                mode=mode,
            ),
            project_id=f"metadata-family-{mode}",
        )
        assert first.status == "completed", first.errors

        fields = _fields(mode, "meta")
        generations = ResultGenerationStore(project)
        before = {
            str(field["name"]): (
                int(_column(project, sheet_id, str(field["name"]))["id"]),
                generations.latest_applied_run_id(
                    int(_column(project, sheet_id, str(field["name"]))["id"])
                ),
            )
            for field in fields
        }

        refused = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{mode}-without-replacement",
                mode=mode,
            ),
            project_id=f"metadata-family-{mode}",
        )
        assert refused.status == "failed"
        assert refused.errors[0].code == "output_column_exists"

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{mode}-rerun",
                replace_existing=True,
                mode=mode,
                refresh=refresh,
            ),
            project_id=f"metadata-family-{mode}",
        )

        assert rerun.status == "completed", rerun.errors
        for name, (column_id, prior_run_id) in before.items():
            assert prior_run_id is not None
            column = _column(project, sheet_id, name)
            assert int(column["id"]) == column_id
            assert generations.latest_applied_run_id(column_id) == rerun.run_id
            assert rerun.run_id != prior_run_id
    finally:
        project.close()


@pytest.mark.parametrize("mode", ["object", "columns"])
def test_targeted_rerun_preserves_untargeted_atomic_family_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    project = Project.create(
        tmp_path / f"targeted-family-rerun-{mode}.frisket", name="metadata"
    )
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(11, 7), filename="before.png", mime="image/png"
        )
        target_row, untargeted_row = project.add_rows(
            sheet_id,
            [
                {"asset": media_cell(digest, filename="before.png", mime="image/png")},
                {
                    "asset": media_cell(
                        "0" * 64, filename="missing.png", mime="image/png"
                    )
                },
            ],
            {"asset": input_id},
        )
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[target_row, untargeted_row],
                prefix="meta",
                key=f"metadata-targeted-{mode}-first",
                mode=mode,
            ),
            project_id=f"metadata-targeted-{mode}",
        )
        assert first.status == "partial", first.errors

        fields = _fields(mode, "meta")
        output_ids = {
            str(field["name"]): int(
                _column(project, sheet_id, str(field["name"]))["id"]
            )
            for field in fields
        }
        details_name = "meta" if mode == "object" else "meta_details"
        details_id = output_ids[details_name]
        generations = ResultGenerationStore(project)
        prior_run_id = generations.latest_applied_run_id(details_id)
        assert prior_run_id is not None
        project.db.execute(
            "UPDATE results SET review_state='rejected' "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (prior_run_id, untargeted_row, details_id),
        )
        project.db.execute(
            "UPDATE runs SET cost_actual=7.25 WHERE id=?", (prior_run_id,)
        )
        project.db.commit()

        manual_value = {"manual_overlay": "keep me"}
        target_manual_value = {"manual_overlay": "keep target correction"}
        project.apply_edits(
            [
                {
                    "row_id": untargeted_row,
                    "column_id": details_id,
                    "value": manual_value,
                },
                {
                    "row_id": target_row,
                    "column_id": details_id,
                    "value": target_manual_value,
                },
            ],
            label="preserve metadata overlay",
        )

        def live_snapshot() -> dict[str, dict[int, Any]]:
            return {
                name: project.get_values(
                    sheet_id,
                    column_id,
                    row_ids=[target_row, untargeted_row],
                )
                for name, column_id in output_ids.items()
            }

        preserved_fields = (
            "value",
            "confidence",
            "justification",
            "error",
            "error_code",
            "review_state",
            "outcome",
        )

        def result_snapshot(run_id: int, row_id: int) -> dict[int, dict[str, Any]]:
            return {
                int(row["column_id"]): {field: row[field] for field in preserved_fields}
                for row in project.db.execute(
                    "SELECT column_id,value,confidence,justification,error,error_code,"
                    "review_state,outcome FROM results WHERE run_id=? AND row_id=?",
                    (run_id, row_id),
                ).fetchall()
            }

        before = live_snapshot()
        prior_untargeted = result_snapshot(prior_run_id, untargeted_row)
        assert prior_untargeted[details_id]["error_code"] == "missing_blob"
        assert prior_untargeted[details_id]["review_state"] == "rejected"
        assert before[details_name][untargeted_row] == manual_value
        assert before[details_name][target_row] == target_manual_value

        replacement_digest = project.add_blob(
            _png_header(17, 9), filename="after.png", mime="image/png"
        )
        project.apply_edits(
            [
                {
                    "row_id": target_row,
                    "column_id": input_id,
                    "value": media_cell(
                        replacement_digest,
                        filename="after.png",
                        mime="image/png",
                    ),
                }
            ],
            label="replace targeted media",
        )

        rerun_action = _action(
            sheet_id=sheet_id,
            row_ids=[target_row],
            prefix="meta",
            key=f"metadata-targeted-{mode}-rerun",
            replace_existing=True,
            mode=mode,
        )
        rerun = run_action_spec(
            project,
            rerun_action,
            project_id=f"metadata-targeted-{mode}",
        )
        assert rerun.status == "completed", rerun.errors

        new_run_id = generations.latest_applied_run_id(details_id)
        assert new_run_id is not None
        assert new_run_id == rerun.run_id
        assert new_run_id != prior_run_id
        run = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (new_run_id,)
        ).fetchone()
        assert run is not None
        assert (run["total_rows"], run["completed_rows"], run["failed_rows"]) == (
            1,
            1,
            0,
        )
        assert float(run["cost_actual"]) == 0.0
        run_store = RunResultStore(project)
        assert run_store.row_error_summary(new_run_id) is None
        assert new_run_id not in run_store.batch_row_error_summaries([new_run_id])
        assert (
            project.db.execute(
                "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY position",
                (new_run_id,),
            ).fetchall()[0]["row_id"]
            == target_row
        )

        # A scoped successor publishes only its selected rows.  Untargeted
        # cells retain their exact prior heads; they are not copied into the
        # successor run as mutable carry-forward rows.
        assert result_snapshot(new_run_id, untargeted_row) == {}
        assert result_snapshot(prior_run_id, untargeted_row) == prior_untargeted
        assert (
            project.db.execute(
                "SELECT run_id FROM cell_result_heads WHERE column_id=? AND row_id=?",
                (details_id, untargeted_row),
            ).fetchone()["run_id"]
            == prior_run_id
        )
        assert project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?",
            (new_run_id,),
        ).fetchone()[0] == len(output_ids)
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=? "
                "AND (tokens_in IS NOT NULL OR tokens_out IS NOT NULL)",
                (new_run_id,),
            ).fetchone()[0]
            == 0
        )

        after = live_snapshot()
        raw_details = project.get_values(
            sheet_id,
            details_id,
            row_ids=[target_row, untargeted_row],
            apply_edits=False,
        )
        assert raw_details[target_row]["normalized"]["filename"] == "after.png"
        assert after[details_name][target_row] == target_manual_value
        assert after[details_name][untargeted_row] == manual_value

        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (rerun.receipt_id,)
            ).fetchone()["body"]
        )
        input_rows = next(
            item for item in receipt["inputs"] if item["ref"]["kind"] == "source_column"
        )
        assert input_rows["ref"]["row_ids"] == [target_row]
        counts = next(
            item["ref"]
            for item in receipt["evidence"]
            if item["ref"]["kind"] == "map_rows_run_counts"
        )
        assert counts["failed_rows"] == 0
        assert counts["total_rows"] == counts["completed_rows"] == 1
        assert counts["failed_row_ids"] == []
        details_ref = next(
            item["ref"] for item in receipt["outputs"] if item["name"] == details_name
        )
        assert details_ref["value_hash"] == output_column_result_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=details_id,
            row_ids=[target_row],
        )
        assert details_ref["value_hash"] != output_column_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=details_id,
            row_ids=[target_row],
        )

        assert project.undo() == rerun.op_ids[0]
        assert live_snapshot() == before
        assert project.redo() == rerun.op_ids[0]
        assert live_snapshot() == after

        later_manual_value = {"manual_overlay": "later target correction"}
        project.apply_edits(
            [
                {
                    "row_id": target_row,
                    "column_id": details_id,
                    "value": later_manual_value,
                }
            ],
            label="revise targeted metadata overlay",
        )
        run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        replay = run_action_spec(
            project,
            rerun_action,
            project_id=f"metadata-targeted-{mode}",
        )
        assert replay.status == "completed", replay.errors
        assert replay.run_id == rerun.run_id
        assert replay.receipt_id == rerun.receipt_id
        assert (
            project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == run_count
        )
        assert live_snapshot()[details_name][target_row] == later_manual_value
    finally:
        project.close()


def test_metadata_replay_rejects_declared_output_format_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "format-drift.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        action = _action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            prefix="meta",
            key="metadata-format-drift",
            mode="columns",
        )
        first = run_action_spec(project, action, project_id="metadata-format-drift")
        assert first.status == "completed", first.errors

        size_column = _column(project, sheet_id, "meta_size_bytes")
        assert size_column["format"] == "filesize"
        project.set_column_format(int(size_column["id"]), None)

        run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        replay = run_action_spec(project, action, project_id="metadata-format-drift")
        assert replay.status == "failed"
        assert [error.code for error in replay.errors] == ["stale_replay"]
        assert replay.errors[0].details == {
            "receipt_id": first.receipt_id,
            "output": "meta_size_bytes",
            "column_id": int(size_column["id"]),
            "expected_format": "filesize",
            "current_format": None,
        }
        assert (
            project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == run_count
        )
    finally:
        project.close()


@pytest.mark.parametrize("damage", ["missing", "hidden"])
def test_changed_family_does_not_implicitly_authorize_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    project = Project.create(tmp_path / f"family-{damage}.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{damage}-first",
                mode="columns",
            ),
            project_id=f"metadata-family-{damage}",
        )
        assert first.status == "completed", first.errors
        if damage == "missing":
            project.db.execute(
                "UPDATE columns SET name='detached_title' "
                "WHERE sheet_id=? AND name='meta_title'",
                (sheet_id,),
            )
        else:
            project.db.execute(
                "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='meta_title'",
                (sheet_id,),
            )
        project.db.commit()
        run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{damage}-rerun",
                mode="columns",
            ),
            project_id=f"metadata-family-{damage}",
        )

        assert rerun.status == "failed"
        assert rerun.errors[0].code == "output_column_exists"
        assert int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) == (
            run_count
        )
    finally:
        project.close()


def test_mixed_family_does_not_implicitly_authorize_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "family-mixed.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        for prefix in ("meta", "other"):
            result = run_action_spec(
                project,
                _action(
                    sheet_id=sheet_id,
                    row_ids=[row_id],
                    prefix=prefix,
                    key=f"metadata-family-mixed-{prefix}",
                    mode="columns",
                ),
                project_id="metadata-family-mixed",
            )
            assert result.status == "completed", result.errors

        other_run_id = int(_column(project, sheet_id, "other_title")["current_run_id"])
        project.db.execute(
            "UPDATE columns SET current_run_id=? "
            "WHERE sheet_id=? AND name='meta_title'",
            (other_run_id, sheet_id),
        )
        project.db.commit()

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-mixed-rerun",
                mode="columns",
            ),
            project_id="metadata-family-mixed",
        )

        assert rerun.status == "failed"
        assert rerun.errors[0].code == "output_column_exists"
    finally:
        project.close()


def test_different_source_does_not_implicitly_authorize_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "family-source.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        project.add_column(sheet_id, "other_asset", type="image")
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-source-first",
                mode="columns",
            ),
            project_id="metadata-family-source",
        )
        assert first.status == "completed", first.errors

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-source-rerun",
                mode="columns",
                input_column="other_asset",
            ),
            project_id="metadata-family-source",
        )

        assert rerun.status == "failed"
        assert rerun.errors[0].code == "output_column_exists"
    finally:
        project.close()


@pytest.mark.parametrize(
    ("tampered_field", "tampered_value"),
    [
        ("name", "wrong_name"),
        ("type", "text"),
        ("run_id", -1),
    ],
)
def test_same_key_replay_rejects_tampered_output_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_field: str,
    tampered_value: Any,
) -> None:
    project = Project.create(
        tmp_path / f"family-tampered-{tampered_field}.frisket", name="metadata"
    )
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{tampered_field}-first",
            ),
            project_id=f"metadata-family-{tampered_field}",
        )
        assert first.status == "completed", first.errors

        run_id = int(_column(project, sheet_id, "meta")["current_run_id"])
        receipt_row = project.db.execute(
            "SELECT id,body FROM receipts WHERE run_id=?", (run_id,)
        ).fetchone()
        assert receipt_row is not None
        body = json.loads(str(receipt_row["body"]))
        body["outputs"][0]["ref"][tampered_field] = tampered_value
        project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (json.dumps(body, sort_keys=True), receipt_row["id"]),
        )
        project.db.commit()

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key=f"metadata-family-{tampered_field}-first",
            ),
            project_id=f"metadata-family-{tampered_field}",
        )

        assert rerun.status == "failed"
        assert rerun.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_explicit_replacement_is_not_authorized_by_an_unrelated_newer_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "family-backfill.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-backfill-first",
            ),
            project_id="metadata-family-backfill",
        )
        assert first.status == "completed", first.errors

        run_id = int(_column(project, sheet_id, "meta")["current_run_id"])
        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE run_id=? AND action_kind=?",
            (run_id, "media.extract_metadata"),
        ).fetchone()
        assert receipt_row is not None
        body = json.loads(str(receipt_row["body"]))
        body.update(
            {
                "receipt_id": "zzzz-backfill-after-metadata",
                "action_id": "backfill-after-metadata",
                "action_kind": "run.backfill",
                "idempotency_key": None,
                "params_hash": "backfill",
                "outputs": [],
            }
        )
        project.db.execute(
            "INSERT INTO receipts "
            "(id,run_id,action_kind,action_id,params_hash,status,body) "
            "VALUES (?,?,?,?,?,'completed',?)",
            (
                body["receipt_id"],
                run_id,
                body["action_kind"],
                body["action_id"],
                body["params_hash"],
                json.dumps(body, sort_keys=True),
            ),
        )
        project.db.commit()

        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-backfill-rerun",
                replace_existing=True,
            ),
            project_id="metadata-family-backfill",
        )

        assert rerun.status == "completed", rerun.errors
    finally:
        project.close()


def test_direct_rerun_rechecks_output_identity_after_claim_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "family-direct-race.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        _stub_successful_image_adapter(monkeypatch)
        first = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-direct-race-first",
            ),
            project_id="metadata-family-direct-race",
        )
        assert first.status == "completed", first.errors
        run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

        acquire = action_lifecycle._acquire_output_claims_for_runner

        def mutate_then_claim(*args, **kwargs):  # noqa: ANN002, ANN003
            project.db.execute(
                "UPDATE columns SET name='renamed_meta' WHERE sheet_id=? AND name='meta'",
                (sheet_id,),
            )
            project.db.commit()
            return acquire(*args, **kwargs)

        monkeypatch.setattr(
            action_lifecycle,
            "_acquire_output_claims_for_runner",
            mutate_then_claim,
        )
        rerun = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-family-direct-race-rerun",
                replace_existing=True,
            ),
            project_id="metadata-family-direct-race",
        )

        assert rerun.status == "failed"
        assert rerun.errors[0].code == "map_rows_failed"
        assert _column(project, sheet_id, "renamed_meta") is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='meta'",
                (sheet_id,),
            ).fetchone()[0]
            == 0
        )
        assert int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) == (
            run_count
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_post_claim_preparation_failure_cleans_claim_and_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "post-claim-failure.frisket")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        acquired = []
        acquire = action_lifecycle._acquire_output_claims_for_runner

        def observe_claim(*args, **kwargs):
            result = acquire(*args, **kwargs)
            acquired.append(result is None)
            return result

        def fail_prepare(*args, **kwargs):
            assert acquired == [True]
            raise RuntimeError("post-claim metadata preparation exploded")

        monkeypatch.setattr(
            action_lifecycle, "_acquire_output_claims_for_runner", observe_claim
        )
        monkeypatch.setattr(MapRunner, "_prepare", fail_prepare)
        key = "metadata-post-claim-failure"
        result = run_action_spec(
            project,
            _action(sheet_id=sheet_id, row_ids=[row_id], prefix="meta", key=key),
            project_id="metadata-post-claim-failure",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "map_rows_failed"
        assert acquired == [True]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?", (key,)
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def test_explicit_replacement_rejects_unmanaged_matching_columns(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "family-unrelated.frisket", name="metadata")
    try:
        sheet_id, row_id = _single_image_sheet(project)
        fields = _fields("columns", "meta")
        for field in fields:
            project.add_column(
                sheet_id,
                str(field["name"]),
                type=str(field["column_type"]),
                format=field.get("format"),
                ai_generated=True,
            )
        action = _action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            prefix="meta",
            key="metadata-family-unrelated",
            mode="columns",
        )
        action["replace_existing"] = True

        result = run_action_spec(
            project,
            action,
            project_id="metadata-family-unrelated",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "output_column_exists"
        assert result.errors[0].details["columns"] == [fields[0]["name"]]
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert len(project.columns(sheet_id)) == len(fields) + 1
    finally:
        project.close()


def test_row_errors_are_stable_and_do_not_trigger_failure_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "row-errors.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="file")
        digest = project.add_blob(
            _png_header(3, 2), filename="valid.png", mime="image/png"
        )
        missing_digest = hashlib.sha256(b"not stored").hexdigest()
        source_values: list[Any] = [
            media_cell(digest, mime="image/png", filename="valid.png"),
            None,
            *[f"malformed-{index}" for index in range(11)],
            {"blob": missing_digest, "filename": "missing.png"},
        ]
        row_ids = project.add_rows(
            sheet_id,
            [{"asset": value} for value in source_values],
            {"asset": input_id},
        )
        _disable_optional_adapters(monkeypatch)

        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="meta",
                key="metadata-row-errors",
            ),
            project_id="metadata-row-errors",
        )
        assert result.status == "partial", result.errors
        stored = project.db.execute(
            "SELECT row_id,outcome,error_code FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id",
            (result.run_id, int(_column(project, sheet_id, "meta")["id"])),
        ).fetchall()
        by_row = {int(row["row_id"]): row for row in stored}
        assert len(by_row) == len(row_ids)
        assert by_row[row_ids[0]]["outcome"] == "ok"
        assert by_row[row_ids[1]]["outcome"] == "empty"
        for row_id in row_ids[2:-1]:
            assert by_row[row_id]["outcome"] == "row_error"
            assert by_row[row_id]["error_code"] == "invalid_media_cell"
        assert by_row[row_ids[-1]]["outcome"] == "row_error"
        assert by_row[row_ids[-1]]["error_code"] == "missing_blob"
    finally:
        project.close()


def test_all_blank_scope_completes_with_null_metadata(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "blank.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="file")
        row_ids = project.add_rows(
            sheet_id,
            [{"asset": None}, {"asset": None}],
            {"asset": input_id},
        )
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="meta",
                key="metadata-all-blank",
            ),
            project_id="metadata-all-blank",
        )
        assert result.status == "completed", result.errors
        output = _column(project, sheet_id, "meta")
        assert project.get_values(sheet_id, int(output["id"]), row_ids=row_ids) == {
            row_id: None for row_id in row_ids
        }
    finally:
        project.close()


def test_standard_receipt_undo_and_redo_own_output_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "undo.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(8, 6), filename="asset.png", mime="image/png"
        )
        row_id = project.add_rows(
            sheet_id,
            [{"asset": media_cell(digest, filename="asset.png", mime="image/png")}],
            {"asset": input_id},
        )[0]
        _disable_optional_adapters(monkeypatch)
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-undo",
            ),
            project_id="metadata-undo",
        )
        assert result.status == "completed", result.errors
        assert result.receipt_id is not None
        assert project.db.execute(
            "SELECT 1 FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert "meta" in {column["name"] for column in project.columns(sheet_id)}

        assert project.undo() == result.op_ids[0]
        assert "meta" not in {column["name"] for column in project.columns(sheet_id)}
        assert project.redo() == result.op_ids[0]
        restored = _column(project, sheet_id, "meta")
        assert not bool(restored["hidden"])
        assert restored["current_run_id"] == result.run_id
    finally:
        project.close()


def test_columns_mode_undo_and_redo_own_all_24_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "columns-undo.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(8, 6), filename="asset.png", mime="image/png"
        )
        row_id = project.add_rows(
            sheet_id,
            [{"asset": media_cell(digest, filename="asset.png", mime="image/png")}],
            {"asset": input_id},
        )[0]
        _disable_optional_adapters(monkeypatch)
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=[row_id],
                prefix="meta",
                key="metadata-columns-undo",
                mode="columns",
            ),
            project_id="metadata-columns-undo",
        )
        assert result.status == "completed", result.errors
        names = {f"meta_{role}" for role in metadata.MEDIA_METADATA_ROLES}
        names.add("meta_details")
        placeholders = ",".join("?" for _ in names)

        def family_rows():
            return project.db.execute(
                f"SELECT name,hidden,current_run_id FROM columns "
                f"WHERE sheet_id=? AND name IN ({placeholders}) ORDER BY name",
                (sheet_id, *sorted(names)),
            ).fetchall()

        created = family_rows()
        assert len(created) == 24
        assert {row["name"] for row in created} == names
        assert {int(row["hidden"]) for row in created} == {0}

        assert project.undo() == result.op_ids[0]
        assert {int(row["hidden"]) for row in family_rows()} == {1}

        assert project.redo() == result.op_ids[0]
        restored = family_rows()
        assert {int(row["hidden"]) for row in restored} == {0}
        assert {int(row["current_run_id"]) for row in restored} == {result.run_id}
    finally:
        project.close()


def test_local_probe_failure_is_a_stable_row_error_per_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "probe-error.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(
            _png_header(4, 4), filename="asset.png", mime="image/png"
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"asset": media_cell(digest, filename="a.png")},
                {"asset": media_cell(digest, filename="b.png")},
            ],
            {"asset": input_id},
        )
        calls = 0

        def fail_probe(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise ValueError("unstable adapter detail")

        monkeypatch.setattr(reader_module, "extract_media_metadata", fail_probe)
        result = run_action_spec(
            project,
            _action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                prefix="meta",
                key="metadata-probe-error",
            ),
            project_id="metadata-probe-error",
        )
        assert result.status == "failed"
        # Failed results are deliberately not retained. Depending on whether
        # the row workers overlap, they may share the in-flight task or retry.
        assert 1 <= calls <= len(row_ids)
        rows = project.db.execute(
            "SELECT outcome,error_code,error FROM results WHERE run_id=? ORDER BY row_id",
            (result.run_id,),
        ).fetchall()
        assert len(rows) == 2
        assert {row["outcome"] for row in rows} == {"row_error"}
        assert {row["error_code"] for row in rows} == {"metadata_extract_failed"}
        assert {row["error"] for row in rows} == {"Media metadata extraction failed."}
    finally:
        project.close()
