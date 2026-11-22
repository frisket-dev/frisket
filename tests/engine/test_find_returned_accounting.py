"""Paid scan facts survive later windows and publication failures."""

import asyncio
import sqlite3

import pytest

from frisket.actions.types import ActionRequest
from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore
from tests.engine.test_map_find_runtime import (
    _FindAdapter,
    _queued_find,
    _run_queued_find,
)


def _find(project, *, rows=1):
    sheet = project.add_sheet("Sources")
    column = project.add_column(sheet, "body", "text")
    project.add_rows(sheet, [{"body": "China trade policy"}] * rows, {"body": column})
    request = ActionRequest(
        action_id="map.find",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        sheet_name="Findings",
        idempotency_key="returned-facts",
        params={
            "source": "body",
            "instruction": "Find China mentions",
            "model": "anthropic/claude-haiku-4-5",
        },
    )
    return _queued_find(project, request, project_id="find-accounting")


def test_find_publication_error_keeps_known_purchase(make_project, monkeypatch):
    project = make_project("find")
    envelope = _find(project)
    adapter = _FindAdapter("unused", matches=[])

    def blocked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(GeneratedColumnClaimStore, "acquire", blocked)
    result = _run_queued_find(project, envelope, adapter)
    assert result.status == "failed"
    assert len(adapter.requests) == 1
    row = project.db.execute(
        "SELECT count(*),sum(provider_cost_usd) FROM model_calls"
    ).fetchone()
    assert tuple(row) == pytest.approx((1, 0.0004))
    assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_find_persists_first_plan_before_second_plan_is_interrupted(make_project):
    project = make_project("interrupted")
    envelope = _find(project, rows=2)

    class Interrupted(_FindAdapter):
        async def complete(self, request, client):
            if self.requests:
                assert (
                    project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0]
                    == 1
                )
                raise asyncio.CancelledError("stopped during next window")
            return await super().complete(request, client)

    adapter = Interrupted("unused", matches=[])
    result = _run_queued_find(project, envelope, adapter)
    assert result.status == "cancelled"
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (envelope.receipt_id,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1
    assert project.db.execute(
        "SELECT sum(provider_cost_usd) FROM model_calls"
    ).fetchone()[0] == pytest.approx(0.0004)


def test_find_publication_attaches_each_already_recorded_purchase_once(make_project):
    project = make_project("completed")
    envelope = _find(project, rows=2)
    result = _run_queued_find(project, envelope, _FindAdapter("unused", matches=[]))
    assert result.status == "completed"
    facts = project.db.execute(
        "SELECT count(*),sum(provider_cost_usd),count(DISTINCT run_id) FROM model_calls"
    ).fetchone()
    assert tuple(facts) == pytest.approx((2, 0.0008, 1))
    assert project.db.execute("SELECT cost_actual FROM runs").fetchone()[
        0
    ] == pytest.approx(0.0008)
