import pytest

from tests.engine.test_runtime_import_preview import importer as importer
from tests.server.test_table_action_previews import _run, preview_host as preview_host


@pytest.mark.parametrize("count,total", [(0, 0), (3, 3), (25, None)])
def test_http_runtime_sample_uses_same_producer_without_publication(
    preview_host, importer, count, total
):
    workspace, _registry, _service, client = preview_host
    imported = importer(project=workspace.get("preview"), count=count)
    imported.request["output_names"] = {"value": "Renamed"}
    before = tuple(imported.project.db.iterdump())
    _, result = _run(client, imported.request)
    assert result["sampled"] == min(count, 20)
    assert result["total"] == total
    assert result["rows"] == [{"Renamed": {"value": n}} for n in range(min(count, 20))]
    assert tuple(imported.project.db.iterdump()) == before
    assert len(imported.calls) == 1
