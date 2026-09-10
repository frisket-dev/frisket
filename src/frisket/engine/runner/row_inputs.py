"""Per-row source-value assembly for the map runner (extracted from
``MapRunner._row_values``): resolve each recipe source column's value for one
row, swap in image pixels for LLM recipes, and apply ``input_template`` when
the spec declares one."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frisket.ops.base import Recipe
from frisket.engine.runner.grounding import (
    MAX_IMAGE_BYTES,
    image_part,
    inject_grounding_segments,
)
from frisket.engine.store import Project
from frisket.authoring.templates import render_column_template


def row_source_snapshot(project, sheet_id, source_column_ids, *, row_ids=None):
    """Pin the semantic input roster and blob descriptors without reading bytes."""
    from frisket.engine.store.artifact_timeline import canonical_json_hash
    from frisket.engine.store.media_blobs import MediaBlobStore
    from frisket.sdk.media import media_text_hash

    row_ids = list(
        row_ids if row_ids is not None else project.visible_row_ids(sheet_id)
    )
    columns = {
        column["id"]: dict(column)
        for column in project.columns(sheet_id, include_hidden=True)
    }
    sources = []

    def blob_snapshot(value):
        # Semantic inputs may include unrelated file columns. Pin their host
        # descriptors without asserting that the handler will read each as PDF.
        digest = value.get("blob") if isinstance(value, dict) else None
        blob = (
            MediaBlobStore(project).blob_row(digest)
            if isinstance(digest, str)
            else None
        )
        if blob is None:
            return None
        source_url = str(blob["source_url"] or "")
        return {
            "blob_hash": digest,
            "filename": blob["filename"],
            "mime": blob["mime"],
            "size": blob["size"],
            "source_url_hash": media_text_hash(source_url) if source_url else None,
        }

    for name, column_id in source_column_ids.items():
        values = project.get_values(sheet_id, column_id, row_ids=row_ids)
        sources.append(
            {
                "column": columns[column_id],
                "values": [
                    {
                        "row_id": row_id,
                        "value_hash": canonical_json_hash(values.get(row_id)),
                        **(
                            {"blob": blob_snapshot(values.get(row_id))}
                            if columns[column_id]["type"]
                            in {"file", "image", "audio", "video"}
                            else {}
                        ),
                    }
                    for row_id in row_ids
                ],
            }
        )
    return {"row_ids": row_ids, "sources": sources}


def is_empty_cell_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return len(value) == 0
    return False


class RowInputValues(dict[str, Any]):
    """Rendered inputs with private facts from the same source-cell read."""

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        source_cells_empty: bool,
        source_cells: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        super().__init__(values)
        self.source_cells_empty = source_cells_empty
        self.source_cells = source_cells


def row_source_values_empty(values: Mapping[str, Any]) -> bool:
    marker = getattr(values, "source_cells_empty", None)
    if isinstance(marker, bool):
        return marker
    return bool(values) and all(is_empty_cell_value(value) for value in values.values())


def _refuse_changed_admitted_input(name: str, row_id: int) -> None:
    from frisket.ops.base import RecipeInvocationHalt

    raise RecipeInvocationHalt(
        "promise_violation",
        "the source value or revision for "
        f"{name!r} on row {row_id} changed after attempt admission; "
        "nothing was sent. Review and confirm the current work scope before "
        "resuming",
    )


def _expected_source_cell(
    snapshot: Mapping[str, Any],
    *,
    source_index: int,
    name: str,
    column_id: int | None,
    row_id: int,
) -> Mapping[str, Any]:
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or source_index >= len(sources):
        _refuse_changed_admitted_input(name, row_id)
    source = sources[source_index]
    if (
        not isinstance(source, Mapping)
        or source.get("name") != name
        or source.get("column_id") != column_id
    ):
        _refuse_changed_admitted_input(name, row_id)
    cells = source.get("cells")
    if not isinstance(cells, list):
        _refuse_changed_admitted_input(name, row_id)
    expected = next(
        (
            cell
            for cell in cells
            if isinstance(cell, Mapping) and cell.get("row_id") == row_id
        ),
        None,
    )
    if expected is None:
        _refuse_changed_admitted_input(name, row_id)
    return expected


def row_values(
    project: Project,
    recipe: Recipe,
    spec: dict,
    col_map: dict[str, int],
    row_id: int,
    *,
    for_model: bool = True,
    column_types: dict[str, str] | None = None,
    capture: dict[str, dict[str, Any]] | None = None,
    admitted_work_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if capture is None and getattr(recipe, "capture_source_cells", False):
        capture = {}
    col_types = column_types
    if col_types is None:
        col_types = {c["name"]: c["type"] for c in project.columns(spec["sheet_id"])}
    values: dict[str, Any] = {}
    source_names = [str(name) for name in recipe.source_columns(spec)]
    # A program may admit provenance-only sources without sending their values
    # to the model. They still participate in the same frozen source checks and
    # returned-result capture as prompt inputs.
    prompt_source_names = set(getattr(recipe, "prompt_source_columns", source_names))
    for source_index, name in enumerate(source_names):
        cid = col_map.get(name)
        if admitted_work_scope is not None:
            expected = _expected_source_cell(
                admitted_work_scope,
                source_index=source_index,
                name=name,
                column_id=cid,
                row_id=row_id,
            )
        else:
            expected = None
        if cid is None:
            continue
        if capture is None and expected is None:
            val = project.get_values(spec["sheet_id"], cid, row_ids=[row_id]).get(
                row_id
            )
        else:
            got, refs = project.get_values_with_refs(
                spec["sheet_id"], cid, row_ids=[row_id]
            )
            val = got.get(row_id)
            ref = refs.get(row_id)
            if expected is not None:
                from frisket.engine.store.cells import replay_generated_value_hash

                canonical_ref = dict(ref) if isinstance(ref, Mapping) else None
                if (
                    expected.get("value_hash") != replay_generated_value_hash(val)
                    or expected.get("value_ref") != canonical_ref
                ):
                    _refuse_changed_admitted_input(name, row_id)
            if capture is not None:
                capture[name] = {
                    "column_id": cid,
                    "column_type": col_types.get(name),
                    "value": val,
                    "value_ref": ref,
                }
        if name not in prompt_source_names:
            continue
        # LLM recipes get image pixels; non-LLM recipes (faces, ffmpeg)
        # need the raw blob reference/path untouched
        if for_model and col_types.get(name) == "image" and val is not None:
            val = image_part(project, val, max_image_bytes=MAX_IMAGE_BYTES) or val
        values[name] = val
    source_cells_empty = bool(prompt_source_names) and all(
        is_empty_cell_value(value) for value in values.values()
    )
    template = str(spec.get("input_template") or "").strip()
    # Evaluations render source and answer separately; collapsing here would
    # conflate the template's synthetic "input" key with a real subject column.
    if template and not spec.get("evaluation_context"):
        # input_template composes the selected columns into one deliberate
        # source value; media parts still ride alongside the rendered text
        # so multimodal recipes do not stringify blobs out of the prompt.
        rendered: dict[str, Any] = {"input": render_column_template(template, values)}
        for name, value in values.items():
            if isinstance(value, dict) and value.get("__image_b64__"):
                rendered[name] = value
        return RowInputValues(
            rendered,
            source_cells_empty=source_cells_empty,
            source_cells=capture,
        )
    if for_model:
        inject_grounding_segments(project, spec, row_id, values)
    return RowInputValues(
        values,
        source_cells_empty=source_cells_empty,
        source_cells=capture,
    )
