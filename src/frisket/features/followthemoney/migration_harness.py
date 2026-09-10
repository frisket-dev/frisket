from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from frisket.contracts.plugin_write_plan import (
    PROJECT_WRITES_CAPABILITY,
)
from frisket.engine.executor.plugin_write_apply import apply_write_plan
from frisket.features.followthemoney import entities_available
from frisket.features.followthemoney.write_plan import (
    import_plan_to_write_plan as import_plan_to_write_plan,
)
from frisket.features.investigations.rowsets import resolve_investigative_rowset
from frisket.engine.store.project import Project

# This module is the shared byte-equivalence path for BOTH the frozen golden
# test and the dormant frisket.ftm bundled plugin (its docstring/module
# comment below), so it needs the `entities` extra the moment any function
# below actually runs -- but importing this module itself must
# stay safe even without it (a subprocess exec'ing the bundled plugin's
# plugin.py, which imports this, must fail with the remediation string, not
# a bare ModuleNotFoundError deep in a third-party import). `plan_followthemoney_import`
# / `build_followthemoney_export_package` are imported lazily, guarded by
# `_require_entities()`, inside the two functions that actually need them.

# Stable identity the harness stamps on every round-trip so the exporter's
# deterministic ``row_ref`` entity ids match between the core-writer golden capture
# and the plugin WritePlan path (both use fresh projects -> identical autoincrement
# sheet/row ids -> identical entity ids under one project_id).
FTM_HARNESS_PROJECT_ID = "ftm-migration-harness"
FTM_HARNESS_DATASET = "ftm-migration"
FTM_HARNESS_PLUGIN_ID = "frisket.ftm"


def _require_entities() -> None:
    ok, err = entities_available()
    if not ok:
        raise RuntimeError(err)


def _canonical_export_inputs(
    project: Project, plan: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build the export ``source``/``mappings``/resolved rowsets for the supported
    (entity + relationship) sheets of a project written from ``plan``.

    The mapping for each sheet maps every visible FtM property column back to its
    property with a ``row_ref`` id policy — a single canonical mapping shared by both
    the golden capture and this harness, so entity bytes depend only on the (identical)
    project rows."""
    sheet_id_by_name = {
        row["name"]: int(row["id"])
        for row in project.db.execute("SELECT id, name FROM sheets").fetchall()
    }
    rowsets: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    rowsets_by_key: dict[str, dict[str, Any]] = {}
    for sheet in [*plan.get("sheets", []), *plan.get("relationship_sheets", [])]:
        sheet_id = sheet_id_by_name[sheet["sheet_name"]]
        rowset_spec = {"kind": "sheet", "sheet_id": sheet_id}
        rowsets.append(rowset_spec)
        properties = {
            column["ftm_property"]: {"column": column["name"]}
            for column in sheet["columns"]
            if column.get("ftm_property") and not column.get("hidden")
        }
        mappings.append(
            {
                "rowset": dict(rowset_spec),
                "schema": sheet["schema"],
                "id_policy": {"kind": "row_ref"},
                "properties": properties,
            }
        )
        resolved = resolve_investigative_rowset(
            project, rowset_spec, project_id=FTM_HARNESS_PROJECT_ID
        )
        rowsets_by_key[f"{rowset_spec['kind']}:{rowset_spec['sheet_id']}"] = resolved
    source = {"kind": "selected_rowsets", "rowsets": rowsets}
    return source, mappings, rowsets_by_key


def export_entities_bytes(project: Project, plan: dict[str, Any]) -> bytes:
    """Export the FtM entity stream (``entities.ftm.jsonl`` bytes) from a project
    written from ``plan`` through the shared core exporter."""
    _require_entities()
    from frisket.features.followthemoney import build_followthemoney_export_package

    source, mappings, rowsets_by_key = _canonical_export_inputs(project, plan)
    package = build_followthemoney_export_package(
        source=source,
        mappings=mappings,
        rowsets_by_key=rowsets_by_key,
        project_id=FTM_HARNESS_PROJECT_ID,
        media_policy="refs",
        validate=True,
    )
    return bytes(package["artifacts"]["entities"]["content"])


def import_via_write_plan(project: Project, plan: dict[str, Any]) -> None:
    """Apply the plan's additive WritePlan to ``project`` via the host applier (the
    plugin ``project_writes`` mechanism)."""
    write_plan = import_plan_to_write_plan(plan)
    apply_write_plan(
        project,
        write_plan,
        plugin_id=FTM_HARNESS_PLUGIN_ID,
        manifest_sha="sha256:ftm-migration-harness",
        declared_capabilities=(PROJECT_WRITES_CAPABILITY,),
    )


def roundtrip_export_bytes(source_path: str | Path) -> bytes:
    """Import a FollowTheMoney file via WritePlan then export it; return the FtM
    entity stream bytes. Byte-equivalent to the pre-deletion core import->export."""
    _require_entities()
    from frisket.features.followthemoney.import_planner import (
        plan_followthemoney_import,
    )

    source_bytes = Path(source_path).read_bytes()
    plan = plan_followthemoney_import(source_bytes, dataset_name=FTM_HARNESS_DATASET)
    with tempfile.TemporaryDirectory(prefix="ftm-harness-") as tmp:
        project = Project.create(Path(tmp) / "bundle", name="ftm-migration-harness")
        try:
            import_via_write_plan(project, plan)
            return export_entities_bytes(project, plan)
        finally:
            project.close()
