"""One-time workspace recipe upgrade for document extraction layouts."""

from __future__ import annotations

import logging

from frisket.contracts.http.document_extraction import ExtractionTemplateSave
from frisket.engine.store import extraction_layouts


_log = logging.getLogger(__name__)

LEGACY_EXTRACTION_LAYOUTS_META_KEY = "legacy_extraction_layouts_upgrade"
LEGACY_EXTRACTION_LAYOUTS_VERSION = "1"


def upgrade_legacy_extraction_layouts(project, project_id, recipes) -> None:
    """Copy this project's legacy saved actions into layouts exactly once.

    The workspace recipe file remains intact. The project metadata stamp is
    written only after the complete scan, so an interrupted database write is
    retried safely through ``imported_recipe_id`` on the next project open.
    """
    if (
        project.get_meta(LEGACY_EXTRACTION_LAYOUTS_META_KEY)
        == LEGACY_EXTRACTION_LAYOUTS_VERSION
    ):
        return

    retry_needed = False
    for entry in recipes:
        if not isinstance(entry, dict):
            continue
        spec = entry.get("spec", {})
        if not isinstance(spec, dict):
            continue
        params = spec.get("params", {})
        if (
            spec.get("action_kind") != "media.extract_document"
            or spec.get("project_id") != project_id
        ):
            continue
        try:
            if not isinstance(params, dict):
                raise ValueError("Invalid saved parameters")
            recipe_id = entry["id"]
            if type(recipe_id) is not int or recipe_id <= 0:
                raise ValueError("Invalid saved recipe identity")
            body = ExtractionTemplateSave(
                sheet_id=spec["sheet_id"],
                source=params["source"],
                reference_row_id=spec.get("reference_row_id"),
                draft=params["template"],
                repeat_group_id=params.get("repeat_group_id"),
            )
            reference_row_id = body.reference_row_id
            if reference_row_id is not None and not project.visible_row_ids(
                body.sheet_id, [reference_row_id]
            ):
                reference_row_id = None
        except (KeyError, TypeError, ValueError):
            _log.warning("Skipped an invalid saved extraction template")
            continue
        try:
            extraction_layouts.save_layout(
                project,
                sheet_id=body.sheet_id,
                source=body.source,
                draft=body.draft.model_dump(mode="json"),
                reference_row_id=reference_row_id,
                repeat_group_id=body.repeat_group_id,
                imported_recipe_id=recipe_id,
            )
        except ValueError:
            # A valid legacy entry can be temporarily unreachable after undo
            # hides its source sheet or column. Leave the upgrade pending so a
            # later redo can restore and import it on the next project open.
            retry_needed = True
            _log.warning("Skipped an invalid saved extraction template")

    if not retry_needed:
        project.set_meta(
            LEGACY_EXTRACTION_LAYOUTS_META_KEY, LEGACY_EXTRACTION_LAYOUTS_VERSION
        )
