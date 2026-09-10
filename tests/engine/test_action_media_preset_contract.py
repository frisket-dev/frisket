from __future__ import annotations

from pathlib import Path

from frisket.engine.executor.action_specs import ResolvedAction


def test_resolved_action_carries_media_facts() -> None:
    resolved = ResolvedAction(
        sheet_id=4,
        row_ids=(10, 11),
        input_columns={"audio": {"id": 7, "type": "file"}},
        facts={
            "input_column": {"name": "audio", "id": 7},
            "row_ids": [10, 11],
            "blob_refs": [{"kind": "media_transcribe_input_blob", "blob": "sha256:x"}],
        },
    )
    snap = resolved.snapshot()
    assert snap["facts"]["blob_refs"][0]["blob"] == "sha256:x"
    assert snap["row_ids"] == [10, 11]


def test_web_capture_page_is_public_reduced_page_acquisition_contract() -> None:
    from frisket.actions.page_capture import CapturePageParams
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.types import ActionRequest
    from frisket.actions.system import root_action_catalog

    request = ActionRequest(
        action_id="web.capture_page",
        scope={"kind": "sheet_rows", "sheet_id": 4, "row_ids": [10, 11]},
        output_names={"page": "Saved page"},
        params={
            "source": "url",
            "render_mode": "static",
            "include_warc": False,
        },
        idempotency_key="capture-page-contract",
    )
    params, _ = ACTION_REGISTRY.get(request.action_id).bind_request(request)
    assert isinstance(params, CapturePageParams)
    assert params.source.name == "url"
    assert request.output_names["page"] == "Saved page"

    catalog = root_action_catalog()
    by_kind = {entry.kind: entry for entry in catalog.actions}
    assert "web.capture_page" in by_kind
    entry = by_kind["web.capture_page"]
    assert entry.required_capabilities == ["project:write", "external:url_capture"]
    assert "source" in entry.input_schema["properties"]
    assert "output_name" not in entry.input_schema["properties"]
    assert "output_prefix" not in entry.input_schema["properties"]
    assert entry.ui_hints["form"] == "generated"
    assert entry.ui_hints["dynamic_outputs"] is True


def test_blob_output_plan_contract_exists_for_capture_finalize_boundary(
    tmp_path: Path,
) -> None:
    import types

    from frisket.engine.executor.blob_outputs import (
        RowBlobOutput,
        RowBlobPlan,
        stage_blob_bytes,
    )

    plan = RowBlobPlan(
        role="html",
        content_digest="sha256:abc",
        staged_path="/tmp/frisket-stage/abc",
        filename="page-10.html",
        mime="text/html",
        source_url="https://example.test/story",
        metadata={"kind": "web_page_capture"},
    )
    output = RowBlobOutput(primary=plan, supplemental=())
    assert output.primary.role == "html"
    assert output.primary.metadata["kind"] == "web_page_capture"

    project = types.SimpleNamespace(path=tmp_path)
    first = stage_blob_bytes(
        project,
        b"<html>same</html>",
        role="html",
        filename="one.html",
        mime="text/html",
    )
    second = stage_blob_bytes(
        project,
        b"<html>same</html>",
        role="html",
        filename="two.html",
        mime="text/html",
    )
    assert first.content_digest == second.content_digest
    assert first.staged_path != second.staged_path
