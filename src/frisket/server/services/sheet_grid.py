"""Sheet grid read services for local server routes."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from datetime import datetime
from typing import Any

from frisket.querysets import (
    SheetRowSetError,
    sheet_row_scope_query as shared_sheet_row_scope_query,
)
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.media_download_candidates import (
    compute_media_download_candidates,
)
from frisket.engine.store.transcript_status import compute_transcript_statuses
from frisket.server.route_errors import RouteError


MAX_EXPLICIT_ROW_IDS = 1000
COLUMN_STATS_AUTO_ROW_LIMIT = 100_000
COLUMN_STATS_CHUNK_SIZE = 5_000
COLUMN_STATS_TOP_LIMIT = 12
COLUMN_STATS_HISTOGRAM_BINS = 10


class SheetGridRouteError(RouteError):
    pass


def sheet_columns_payload(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    """Return the grid's canonical column projection without loading rows."""
    return _sheet_data_payload(
        project,
        sheet_id,
        _visible_sheet_columns(project, sheet_id),
        [],
        total=0,
    )["columns"]


class SheetGridService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def sheet_data(
        self,
        project_id: str,
        sheet_id: int,
        *,
        offset: int = 0,
        limit: int = 200,
        parent_row_id: int | None = None,
        filter_: str | None = None,
        sort: str | None = None,
        row_ids: str | None = None,
    ) -> dict:
        project = self._workspace.get(project_id)
        visible_columns = _visible_sheet_columns(project, sheet_id)
        explicit_row_ids = _parse_row_ids_param(row_ids)
        if explicit_row_ids is not None:
            cols = visible_columns
            if explicit_row_ids:
                placeholders = ",".join("?" * len(explicit_row_ids))
                present = {
                    int(r["id"])
                    for r in project.db.execute(
                        "SELECT id FROM rows "
                        "WHERE sheet_id=? AND hidden=0 "
                        f"AND id IN ({placeholders})",
                        (sheet_id, *explicit_row_ids),
                    ).fetchall()
                }
            else:
                present = set()
            ordered_ids = [rid for rid in explicit_row_ids if rid in present]
            # Lens views provide their own ranked row order. The grid keeps that
            # exact order and intentionally ignores filter/sort scope here.
            window_ids = ordered_ids[offset : offset + limit]
            return _sheet_data_payload(
                project,
                sheet_id,
                cols,
                window_ids,
                total=len(ordered_ids),
            )

        cols, where_sql, where_params, order_parts, order_params = (
            _sheet_row_scope_query(
                project,
                sheet_id,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
            )
        )
        total = project.db.execute(
            f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", where_params
        ).fetchone()[0]
        rows = project.db.execute(
            f"""
            SELECT r.id, r.parent_row_id
            FROM rows r
            WHERE {where_sql}
            ORDER BY {", ".join(order_parts)}
            LIMIT ? OFFSET ?
            """,
            [*where_params, *order_params, limit, offset],
        ).fetchall()
        return _sheet_data_payload(
            project,
            sheet_id,
            cols,
            [r["id"] for r in rows],
            total=total,
        )

    def column_stats(
        self,
        project_id: str,
        sheet_id: int,
        column_id: int,
        *,
        force: bool = False,
        parent_row_id: int | None = None,
        filter_: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        require_visible_sheet(project, sheet_id)
        return _column_stats_payload(
            project,
            sheet_id,
            column_id,
            force=force,
            parent_row_id=parent_row_id,
            filter_=filter_,
            sort=sort,
        )

    def locate_sheet_row(
        self,
        project_id: str,
        sheet_id: int,
        row_id: int,
        *,
        page_size: int = 500,
        parent_row_id: int | None = None,
        filter_: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        require_visible_sheet(project, sheet_id)
        _cols, where_sql, where_params, order_parts, order_params = (
            _sheet_row_scope_query(
                project,
                sheet_id,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
            )
        )
        row = project.db.execute(
            f"""
            SELECT located.row_index
            FROM (
              SELECT r.id,
                     ROW_NUMBER() OVER (ORDER BY {", ".join(order_parts)}) - 1
                       AS row_index
              FROM rows r
              WHERE {where_sql}
            ) located
            WHERE located.id=?
            """,
            # Placeholders are bound in SQL text order: the window ORDER BY is
            # inside SELECT text before WHERE, unlike the normal page query.
            [*order_params, *where_params, row_id],
        ).fetchone()
        if row is None:
            return {
                "schema_version": "frisket.sheet_row_location.v1",
                "sheet_id": sheet_id,
                "row_id": row_id,
                "found": False,
                "index": None,
                "page_offset": None,
                "page_size": page_size,
            }
        index = int(row["row_index"])
        return {
            "schema_version": "frisket.sheet_row_location.v1",
            "sheet_id": sheet_id,
            "row_id": row_id,
            "found": True,
            "index": index,
            "page_offset": (index // page_size) * page_size,
            "page_size": page_size,
        }


def _sheet_row_scope_query(
    project: Project,
    sheet_id: int,
    *,
    parent_row_id: int | None = None,
    filter_: str | None = None,
    sort: str | None = None,
) -> tuple[list[Any], str, list[Any], list[str], list[Any]]:
    try:
        return shared_sheet_row_scope_query(
            project,
            sheet_id,
            parent_row_id=parent_row_id,
            filter_=filter_,
            sort=sort,
        )
    except SheetRowSetError as exc:
        raise SheetGridRouteError(400, str(exc)) from exc


def _parse_row_ids_param(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            rid = int(token)
        except ValueError as exc:
            raise SheetGridRouteError(400, f"invalid row_ids value: {token!r}") from exc
        if rid <= 0 or rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if len(out) > MAX_EXPLICIT_ROW_IDS:
            raise SheetGridRouteError(
                400,
                f"too many row_ids: at most {MAX_EXPLICIT_ROW_IDS} allowed per request",
            )
    return out


def _visible_sheet_columns(project: Project, sheet_id: int) -> list[Any]:
    require_visible_sheet(project, sheet_id)
    return project.columns(sheet_id)


def require_visible_sheet(project: Project, sheet_id: int) -> None:
    """Fail closed for both missing and lifecycle-hidden sheets."""
    sheet = project.db.execute(
        "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise SheetGridRouteError(404, "sheet not found")


def _column_stats_is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _column_stats_display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _column_stats_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _column_stats_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None


def _column_stats_file_size(project: Project, value: Any) -> int | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return _column_stats_file_size(project, parsed)
        digest = text
    elif isinstance(value, dict):
        for key in ("size", "size_bytes", "bytes"):
            raw_size = value.get(key)
            if isinstance(raw_size, bool):
                continue
            if isinstance(raw_size, (int, float)) and math.isfinite(float(raw_size)):
                size = int(raw_size)
                return size if size >= 0 else None
            if isinstance(raw_size, str):
                try:
                    size = int(raw_size.strip())
                except ValueError:
                    continue
                return size if size >= 0 else None
        raw_digest = value.get("blob") or value.get("hash") or value.get("blob_hash")
        digest = raw_digest if isinstance(raw_digest, str) else ""
    else:
        return None
    if not digest:
        return None
    row = project.db.execute(
        "SELECT size FROM blobs WHERE hash=?", (digest,)
    ).fetchone()
    if row is None or row["size"] is None:
        return None
    size = int(row["size"])
    return size if size >= 0 else None


def _column_stats_nice_step(raw_step: float) -> float:
    if raw_step <= 0 or not math.isfinite(raw_step):
        return 1
    power = 10 ** math.floor(math.log10(raw_step))
    fraction = raw_step / power
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * power


def _column_stats_histogram(values: list[float]) -> list[dict[str, Any]]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if lo == hi:
        return [{"min": lo, "max": hi, "count": len(values)}]
    target_bins = min(COLUMN_STATS_HISTOGRAM_BINS, max(1, len(values)))
    width = _column_stats_nice_step((hi - lo) / target_bins)
    start = math.floor(lo / width) * width
    end = math.ceil(hi / width) * width
    bin_count = max(1, int(round((end - start) / width)))
    counts = [0] * bin_count
    for value in values:
        index = min(bin_count - 1, max(0, int((value - start) / width)))
        counts[index] += 1
    return [
        {
            "min": start + width * index,
            "max": start + width * (index + 1),
            "count": count,
        }
        for index, count in enumerate(counts)
    ]


def _column_stats_payload(
    project: Project,
    sheet_id: int,
    column_id: int,
    *,
    force: bool,
    parent_row_id: int | None = None,
    filter_: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    cols, where_sql, where_params, _order_parts, _order_params = _sheet_row_scope_query(
        project,
        sheet_id,
        parent_row_id=parent_row_id,
        filter_=filter_,
        sort=sort,
    )
    column = next((c for c in cols if int(c["id"]) == column_id), None)
    if column is None:
        raise SheetGridRouteError(404, "column not found")
    total = project.db.execute(
        f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", where_params
    ).fetchone()[0]
    base: dict[str, Any] = {
        "schema_version": "frisket.column_stats.v1",
        "sheet_id": sheet_id,
        "column": {
            "id": column["id"],
            "name": column["name"],
            "type": column["type"],
            "format": column["format"] if "format" in column.keys() else None,
        },
        "row_count": total,
        "threshold": COLUMN_STATS_AUTO_ROW_LIMIT,
        "computed": False,
        "requires_manual_analyze": bool(
            total > COLUMN_STATS_AUTO_ROW_LIMIT and not force
        ),
    }
    if base["requires_manual_analyze"]:
        return base

    missing = 0
    present_count = 0
    top_counter: Counter[str] = Counter()
    numbers: list[float] = []
    text_values: list[str] = []
    dates: list[datetime] = []
    json_type_counts: Counter[str] = Counter()
    file_sizes: list[int] = []
    is_file_column = str(column["type"]) == "file"
    offset = 0
    while offset < total:
        rows = project.db.execute(
            f"""
            SELECT r.id
            FROM rows r
            WHERE {where_sql}
            ORDER BY r.id
            LIMIT ? OFFSET ?
            """,
            [*where_params, COLUMN_STATS_CHUNK_SIZE, offset],
        ).fetchall()
        row_ids = [int(r["id"]) for r in rows]
        if not row_ids:
            break
        values = project.get_values(sheet_id, column_id, row_ids=row_ids)
        for rid in row_ids:
            value = values.get(rid)
            if _column_stats_is_missing(value):
                missing += 1
                continue
            present_count += 1
            if is_file_column:
                if (file_size := _column_stats_file_size(project, value)) is not None:
                    file_sizes.append(file_size)
                continue
            top_counter[_column_stats_display_value(value)] += 1
            if (parsed_number := _column_stats_number(value)) is not None:
                numbers.append(parsed_number)
            if isinstance(value, str):
                text_values.append(value)
            if (parsed_date := _column_stats_datetime(value)) is not None:
                dates.append(parsed_date)
            json_type_counts[type(value).__name__] += 1
        offset += len(row_ids)

    if is_file_column:
        payload = {
            **base,
            "computed": True,
            "requires_manual_analyze": False,
            "missing": missing,
            "present": present_count,
            "top_values": [],
            "numeric": None,
            "text": None,
            "date": None,
            "json_types": [],
        }
        if file_sizes:
            payload["file"] = {
                "count": len(file_sizes),
                "min_size": min(file_sizes),
                "max_size": max(file_sizes),
            }
        return payload

    top_values = [
        {"value": value, "count": count}
        for value, count in top_counter.most_common(COLUMN_STATS_TOP_LIMIT)
    ]

    numeric_stats: dict[str, Any] | None = None
    if numbers:
        numeric_stats = {
            "count": len(numbers),
            "mean": statistics.fmean(numbers),
            "median": statistics.median(numbers),
            "min": min(numbers),
            "max": max(numbers),
            "histogram": _column_stats_histogram(numbers),
        }
    text_stats: dict[str, Any] | None = None
    if text_values:
        shortest = min(text_values, key=len)
        longest = max(text_values, key=len)
        lengths = [float(len(value)) for value in text_values]
        text_stats = {
            "count": len(text_values),
            "shortest": shortest,
            "shortest_length": len(shortest),
            "longest": longest,
            "longest_length": len(longest),
            "mean_length": statistics.fmean(lengths),
            "median_length": statistics.median(lengths),
            "length_histogram": _column_stats_histogram(lengths),
        }
    date_stats: dict[str, Any] | None = None
    if dates:
        ordered = sorted(dates)
        date_stats = {
            "count": len(dates),
            "min": ordered[0].isoformat(),
            "median": ordered[len(ordered) // 2].isoformat(),
            "max": ordered[-1].isoformat(),
        }

    return {
        **base,
        "computed": True,
        "missing": missing,
        "present": present_count,
        "distinct": len(top_counter),
        "top_values": top_values,
        "numeric": numeric_stats,
        "text": text_stats,
        "date": date_stats,
        "json_types": [
            {"type": key, "count": count}
            for key, count in json_type_counts.most_common()
        ],
    }


def _sheet_data_payload(
    project: Project,
    sheet_id: int,
    cols: list[Any],
    row_ids: list[int],
    *,
    total: int,
) -> dict:
    child_count: dict[int, int] = {}
    parent_of: dict[int, Any] = {}
    if row_ids:
        ph = ",".join("?" * len(row_ids))
        for r in project.db.execute(
            f"SELECT id, parent_row_id FROM rows WHERE id IN ({ph})",
            row_ids,
        ):
            parent_of[r["id"]] = r["parent_row_id"]
        for r in project.db.execute(
            f"SELECT parent_row_id, COUNT(*) c FROM rows "
            f"WHERE hidden=0 AND parent_row_id IN ({ph}) GROUP BY parent_row_id",
            row_ids,
        ):
            child_count[r["parent_row_id"]] = r["c"]
    data: dict[int, dict[str, Any]] = {rid: {} for rid in row_ids}
    meta: dict[int, dict[str, Any]] = {rid: {} for rid in row_ids}
    generation_store = ResultGenerationStore(project)
    managed_columns: dict[int, bool] = {}
    mixed_columns: dict[int, bool] = {}
    latest_run_ids: dict[int, int | None] = {}

    def is_generation_managed(column_id: int) -> bool:
        if column_id not in managed_columns:
            managed_columns[column_id] = generation_store.is_generation_managed(
                column_id
            )
        return managed_columns[column_id]

    def has_mixed_origins(column_id: int) -> bool:
        if column_id not in mixed_columns:
            mixed_columns[column_id] = is_generation_managed(
                column_id
            ) and generation_store.has_mixed_origins(column_id)
        return mixed_columns[column_id]

    def latest_run_id(column: Any) -> int | None:
        column_id = int(column["id"])
        if column_id not in latest_run_ids:
            if is_generation_managed(column_id):
                latest_run_ids[column_id] = generation_store.latest_applied_run_id(
                    column_id
                )
            else:
                latest_run_ids[column_id] = None
        return latest_run_ids[column_id]

    for c in cols:
        column_id = int(c["id"])
        generation_managed = is_generation_managed(column_id)
        vals, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=row_ids)
        for rid in row_ids:
            data[rid][str(column_id)] = vals.get(rid)
            meta[rid].setdefault(str(column_id), {})["current_value_ref"] = refs[rid]

        ran_rows: set[int] = set()
        if generation_managed and row_ids:
            heads = generation_store.read_cell_heads(column_id, row_ids=row_ids)
            ran_rows.update(heads)
            for row_id, head in heads.items():
                if row_id not in meta:
                    continue
                is_error = head.publication_effect == "publish_error"
                if is_error or any(
                    value is not None
                    for value in (head.confidence, head.justification, head.error)
                ):
                    meta[row_id].setdefault(str(column_id), {}).update(
                        {
                            "confidence": head.confidence,
                            "justification": head.justification,
                            "error": head.error,
                            "review_state": head.review_state,
                            "state": "error" if is_error else "complete",
                            **({"outcome": head.outcome} if is_error else {}),
                        }
                    )
        if bool(c["ai_generated"]) and generation_managed:
            for rid in row_ids:
                if rid in ran_rows:
                    continue
                if vals.get(rid) is not None:
                    continue
                meta[rid].setdefault(str(column_id), {}).update({"state": "incomplete"})
            # Do not let replay acceptance reinterpret a mixed head set.
            # Direct callers receive the typed
            # MixedOriginReplayUnsupported gate from the store layer.
            if not has_mixed_origins(column_id):
                for rid, pending in project.pending_replay_values(
                    sheet_id, column_id, row_ids=row_ids
                ).items():
                    meta[rid].setdefault(str(column_id), {})["pending_value"] = {
                        "fresh_value": pending["fresh_value"],
                        "run_id": pending["run_id"],
                        "generated_value_hash": pending["generated_value_hash"],
                    }
    # Full-column pending count feeds the header chip. Mixed columns are
    # intentionally gated until replay acceptance carries an exact head ref.
    replay_pending_counts = {
        c["id"]: project.pending_replay_count(sheet_id, c["id"])
        for c in cols
        if bool(c["ai_generated"])
        and is_generation_managed(int(c["id"]))
        and not has_mixed_origins(int(c["id"]))
    }
    transcript_statuses = compute_transcript_statuses(project, sheet_id, cols)
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        enabled_workbench_plugin_ids,
    )

    media_download_candidates = compute_media_download_candidates(
        cols,
        data,
        row_ids,
        enabled_plugin_ids=enabled_workbench_plugin_ids(project),
    )
    return {
        "columns": [
            {
                "id": c["id"],
                "name": c["name"],
                "type": c["type"],
                "ai_generated": bool(c["ai_generated"]),
                "format": (c["format"] if "format" in c.keys() else None),
                "semantic_type": (
                    c["semantic_type"] if "semantic_type" in c.keys() else None
                ),
                # Values are manual/source facts or exact generation heads;
                # the retired scalar is never reader authority.
                "current_run_id": None,
                "latest_run_id": latest_run_id(c),
                "generation_managed": is_generation_managed(int(c["id"])),
                "mixed_origins": has_mixed_origins(int(c["id"])),
                "transcript_status": transcript_statuses.get(c["id"]),
                "media_download_candidate": media_download_candidates.get(c["id"]),
                "replay_pending_count": replay_pending_counts.get(c["id"], 0),
                "default_hidden": bool(c["default_hidden"]),
            }
            for c in cols
        ],
        "rows": [
            {
                "id": rid,
                "cells": data[rid],
                "meta": meta[rid],
                "parent_row_id": parent_of.get(rid),
                "child_count": child_count.get(rid, 0),
            }
            for rid in row_ids
        ],
        "total": total,
    }
