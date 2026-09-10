"""Independent review of visual reader's owned anchor transaction."""

import asyncio

import pytest

from frisket.actions.types import ColumnRef
from frisket.engine.executor.visual_cuts_read import AdmittedVisualCutsReader
from frisket.engine.store import evidence
from test_visual_cuts_reader import source as source, _bind


def test_cancelled_anchor_creation_closes_owned_transaction(source, monkeypatch):
    project, _, _, _, _ = source
    before = tuple(project.db.iterdump())
    real_record = evidence.record_source_artifact

    def interrupted_record(*args, **kwargs):
        real_record(*args, **kwargs)
        raise asyncio.CancelledError("interrupted timeline anchor publication")

    monkeypatch.setattr(evidence, "record_source_artifact", interrupted_record)

    async def scenario():
        reader = AdmittedVisualCutsReader(project)
        row, bound = _bind(reader, source)
        try:
            with pytest.raises(asyncio.CancelledError):
                await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()

    asyncio.run(scenario())
    observed = {
        "transaction_open": project.db.in_transaction,
        "database_unchanged": tuple(project.db.iterdump()) == before,
    }
    assert observed == {"transaction_open": False, "database_unchanged": True}
    project.db.commit()
    assert tuple(project.db.iterdump()) == before
