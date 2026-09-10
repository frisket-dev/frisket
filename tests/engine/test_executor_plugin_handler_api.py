from __future__ import annotations

from frisket.plugins.sdk import Plugin


def test_plugin_author_importer_name_generates_local_handler_key() -> None:
    plugin = Plugin()

    @plugin.importer(
        "cases",
        title="Import cases",
        columns=[{"name": "case_id", "type": "case_id"}],
    )
    def import_cases(ctx, source):  # pragma: no cover - not invoked
        del ctx, source
        return []

    importer = plugin.importer_for(
        kind="demo.ndjson_cases.importer.cases",
        handler_key="demo.ndjson_cases:cases",
        plugin_id="demo.ndjson_cases",
    )

    assert importer is not None
    assert importer.kind == "cases"
    assert importer.handler_key == "cases"
    assert importer.columns == ({"name": "case_id", "type": "case_id"},)
