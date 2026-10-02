from __future__ import annotations

import io

import pytest
from starlette.datastructures import UploadFile

from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_types import BulkImportLimits, BulkUpload


def _upload(payload: bytes) -> BulkUpload:
    return BulkUpload(
        filename="one.bin",
        logical_path="one.bin",
        mime="application/octet-stream",
        file=UploadFile(file=io.BytesIO(payload), filename="one.bin"),
    )


@pytest.mark.asyncio
async def test_staging_is_durable_by_default_and_may_be_explicitly_ephemeral(
    tmp_path, monkeypatch
):
    syncs: list[int] = []
    monkeypatch.setattr(import_bulk_sources.os, "fsync", syncs.append)

    durable = tmp_path / "durable"
    (durable / "files").mkdir(parents=True)
    await import_bulk_sources.stage_uploads(
        [_upload(b"durable")], durable, False, BulkImportLimits()
    )
    assert len(syncs) == 2  # staged file and its containing directory

    syncs.clear()
    ephemeral = tmp_path / "ephemeral"
    (ephemeral / "files").mkdir(parents=True)
    await import_bulk_sources.stage_uploads(
        [_upload(b"ephemeral")],
        ephemeral,
        False,
        BulkImportLimits(),
        durable=False,
    )
    assert syncs == []
