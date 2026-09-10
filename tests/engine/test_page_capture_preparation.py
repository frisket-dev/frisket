from types import SimpleNamespace

import pytest

from frisket.actions.file_types import UrlColumn
from frisket.actions.page_capture_types import CaptureOptions, PreparedPageCapture
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.page_capture_action import prepare_page_capture_action
from frisket.engine.store import Project


def test_capture_authoring_types_are_public_sdk_exports():
    import frisket.sdk as sdk
    from frisket.actions import page_capture_types

    for name in (
        "CaptureOptions",
        "CapturedPages",
        "PageCapturer",
        "PreparedPageCapture",
    ):
        assert getattr(sdk, name) is getattr(page_capture_types, name)
        assert name in sdk.__all__


def test_cluster_authoring_types_are_public_sdk_exports():
    import frisket.sdk as sdk
    from frisket.actions import cluster_types

    for name in (
        "ClusterColumn",
        "ClusterOptions",
        "ClusterReview",
        "ValueClusterer",
        "PreparedClustering",
        "ClusteredValues",
    ):
        assert getattr(sdk, name) is getattr(cluster_types, name)
        assert name in sdk.__all__


@pytest.fixture
def capture_case(tmp_path):
    project = Project.create(tmp_path / "capture.frisket", name="Capture")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "actual_url", type="link")
    rows = project.add_rows(
        sheet, [{"actual_url": "https://example.test"}], {"actual_url": column}
    )
    project.db.commit()

    def prepare(handler, *, options=None, output_names=None, sheet_name=None):
        bound = SimpleNamespace(
            action=SimpleNamespace(
                action_id="custom.capture",
                definition=SimpleNamespace(run=SimpleNamespace(handler=handler)),
            ),
            params=SimpleNamespace(
                differently_named=UrlColumn("actual_url"),
                options=options or CaptureOptions(),
            ),
            request=ActionRequest(
                action_id="custom.capture",
                idempotency_key="capture-preparation",
                params={},
                scope=SheetRows(sheet_id=sheet, row_ids=rows),
                output_names=output_names or {},
                sheet_name=sheet_name,
            ),
        )
        return prepare_page_capture_action(project, bound)

    yield project, column, rows, prepare
    project.close()


def test_preparation_uses_actual_arguments_without_project_effects(capture_case):
    project, column, rows, prepare = capture_case
    before = project.db.total_changes
    plan = prepare(
        lambda params, capture: capture.prepare(
            params.differently_named, options=params.options
        ),
        output_names={"page": "archive"},
    )
    assert plan.resolved["input_column"]["id"] == column
    assert plan.resolved["row_ids"] == rows
    assert plan.output_names == {"page": "archive"}
    assert plan.creates_sheet is False
    assert plan.required_capabilities == ("project:write", "external:url_capture")
    assert project.db.total_changes == before


def test_preparation_returns_only_its_own_issued_handle(capture_case):
    *_, prepare = capture_case
    with pytest.raises(ValueError, match="issued by this invocation"):
        prepare(lambda params, capture: PreparedPageCapture(selected_row_count=1))


def test_links_schema_and_capabilities_follow_actual_options(capture_case):
    *_, prepare = capture_case
    plan = prepare(
        lambda params, capture: capture.prepare(
            params.differently_named,
            options=CaptureOptions(output_mode="links", render_mode="playwright"),
        ),
        sheet_name="Extracted links",
        output_names={"url": "destination"},
    )
    assert plan.creates_sheet is True
    assert plan.required_capabilities[-1] == "external:browser_render"
    assert plan.output_names["url"] == "destination"
    assert {field["key"] for field in plan.output_fields} == {
        "url",
        "anchor_text",
        "source_url",
    }


@pytest.mark.parametrize("mode", ["page", "links"])
def test_prepared_capture_publishes_actual_names_and_replays(
    capture_case, monkeypatch, mode
):
    from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
    from frisket.engine.executor.page_capture import execute_prepared_capture
    from frisket.ops.capture.url import StaticUrlFetchResult

    project, _, _, prepare = capture_case
    monkeypatch.setattr("frisket.ops.capture.url.url_is_safe", lambda url: True)
    plan = prepare(
        lambda params, capture: capture.prepare(
            params.differently_named, options=params.options
        ),
        options=CaptureOptions(output_mode=mode),
        output_names={"page": "archive"} if mode == "page" else {"url": "destination"},
        sheet_name="Extracted" if mode == "links" else None,
    )
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return StaticUrlFetchResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=b'<html><a href="/other">Other</a></html>',
            elapsed_ms=1,
            redirects=[],
        )

    def execute():
        return execute_prepared_capture(
            project,
            _TypedProjectEnvelope(
                kind="custom.capture", idempotency_key="prepared-capture", params={}
            ),
            plan.capture,
            project_id="capture",
            params_hash="sha256:prepared",
            resolved=plan.resolved,
            url_capture_fetcher=fetch,
            url_capture_browser=None,
        )

    first = execute()
    assert first.status == "completed", first
    assert execute().receipt_id == first.receipt_id
    assert len(calls) == 1
    sheet_id = (
        plan.capture.sheet_id
        if mode == "page"
        else next(
            sheet["id"] for sheet in project.sheets() if sheet["name"] == "Extracted"
        )
    )
    assert ("archive" if mode == "page" else "destination") in {
        column["name"] for column in project.columns(sheet_id)
    }
