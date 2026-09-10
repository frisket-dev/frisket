import asyncio
from contextlib import asynccontextmanager

import pytest

import frisket.preview.ocr as preview
from frisket.engine.store import Project


@pytest.mark.parametrize("fails", (False, True))
def test_rapidocr_compare_uses_one_request_pool_and_always_closes(
    tmp_path, monkeypatch, fails
):
    events = []
    page = tmp_path / "page.png"

    class Pool:
        async def ocr(self, paths):
            assert paths == [str(page.absolute())]
            events.append("read")
            if fails:
                raise RuntimeError("recognition failed")
            return [{"text": "recognized", "blocks": []}]

    @asynccontextmanager
    async def scope(*, expected_rows, language):
        assert expected_rows == 1
        assert language == "ja"
        events.append("open")
        try:
            yield Pool()
        finally:
            events.append("close")

    monkeypatch.setattr(preview, "rapidocr_execution_scope", scope)
    project = Project.create(tmp_path / "compare.frisket", name="Compare")
    try:
        operation = preview.run_ocr_engine_preview(
            "rapidocr",
            [preview.RenderedPreviewPage(page=1, path=page)],
            project=project,
            language="ja",
            scratch=tmp_path,
        )
        if fails:
            with pytest.raises(RuntimeError, match="recognition failed"):
                asyncio.run(operation)
        else:
            result = asyncio.run(operation)
            assert result.pages == [{"text": "recognized", "blocks": []}]
        assert events == ["open", "read", "close"]
    finally:
        project.close()


def test_billable_compare_refuses_before_opening_local_resources(tmp_path, monkeypatch):
    def forbidden_scope(**kwargs):
        raise AssertionError("Billable refusal must precede resource acquisition")

    monkeypatch.setattr(preview, "rapidocr_execution_scope", forbidden_scope)
    project = Project.create(tmp_path / "compare.frisket", name="Compare")
    try:
        with pytest.raises(preview.BillablePreviewDispatch):
            asyncio.run(
                preview.run_ocr_engine_preview(
                    "datalab", [], project=project, language=None, scratch=tmp_path
                )
            )
    finally:
        project.close()
