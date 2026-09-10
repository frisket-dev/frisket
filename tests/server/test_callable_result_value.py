"""Domain returns survive durable replay and both MCP result adapters."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from frisket.contracts.action import ActionError, Receipt, ReceiptIO
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.mcp import HostedBackend, LocalBackend
from frisket.server.mcp import backends


def _receipt(value, *, status="completed"):
    return Receipt(
        receipt_id="receipt_report",
        action_id="action_report",
        action_kind="query.preview",
        project_id="demo",
        idempotency_key="report-once",
        status=status,
        value=value,
        outputs=[
            ReceiptIO(
                name="csv",
                ref={"kind": "export_artifact", "format": "csv", "path": "report.csv"},
            )
        ],
        errors=[ActionError(code="callable_failed", message="Later work failed")]
        if status == "failed"
        else [],
    )


@pytest.mark.parametrize("value", [None, False, 0, "", [1, {"count": 2}]])
def test_domain_value_round_trips_receipt_storage_and_result(tmp_path, value):
    path = tmp_path / "values.frisket"
    project = Project.create(path, name="Values")
    receipt = _receipt(value)
    try:
        ReceiptStore(project).insert_completed(receipt)
        stored = ReceiptStore(project).find_by_idempotency_key("report-once")
        assert stored is not None
        restored = stored.parsed()
        result = _result_from_receipt(restored)
        assert result.value == value
        assert type(result.value) is type(value)
        assert result.outputs[0].ref == receipt.outputs[0].ref
        assert result.model_dump(mode="json")["value"] == value
    finally:
        project.close()


@pytest.mark.parametrize("backend_kind", ["local", "hosted"])
@pytest.mark.parametrize(
    ("status", "has_receipt"),
    [
        ("completed", True),
        ("partial", True),
        ("failed", True),
        ("cancelled", True),
        ("failed", False),
    ],
)
def test_mcp_preserves_terminal_receipt_values_and_completed_effects(
    tmp_path, monkeypatch, backend_kind, status, has_receipt
):
    value = {"matched": 2, "exported": True} if status == "completed" else None
    receipt = _receipt(value, status=status)
    body = _result_from_receipt(receipt).model_dump(mode="json")
    if not has_receipt:
        body["receipt_id"] = None
    status_code = 400 if status in {"failed", "cancelled"} else 200
    request = {
        "action_id": "query.preview",
        "scope": {"kind": "project"},
        "params": {"query": {}, "limit": 1, "offset": 0},
        "idempotency_key": "report-once",
    }

    async def invoke():
        if backend_kind == "local":
            monkeypatch.setattr(
                backends.ActionRunService,
                "run_action",
                lambda *args: SimpleNamespace(payload=body, status_code=status_code),
            )
            return await LocalBackend(tmp_path / "workspace").run_action(
                "demo", request
            )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status_code, json=body)
            ),
            base_url="http://frisket.test",
        ) as client:
            return await HostedBackend(client=client).run_action("demo", request)

    if not has_receipt:
        with pytest.raises(ValueError, match="callable_failed"):
            asyncio.run(invoke())
        return
    result = asyncio.run(invoke())
    assert result == {
        "status": status,
        "done": True,
        "receipt_id": receipt.receipt_id,
        "outputs": body["outputs"],
        "value": value,
        "errors": body["errors"],
        "warnings": [],
    }
