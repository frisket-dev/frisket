"""Bounded random and exact-row selection for review bundle pages."""

from __future__ import annotations

import random
from collections.abc import Iterable, MutableSequence, Sequence
from typing import Any, Protocol, TypeVar

from frisket.engine.store import Project
from frisket.engine.store.runs import REVIEWABLE_OUTCOMES_SQL

from .review import _hydrate_review_bundles, review_bundle_count
from .review_ordering import ReviewBundleKey


_PAGE_SCHEMA = "frisket.review_bundles_page.v1"
_MAX_PAGE_SIZE = 100
_PROBE_FACTOR = 16
_PROBE_QUERY_CHUNK = 300

_T = TypeVar("_T")


class _RandomSource(Protocol):
    def sample(self, population: range, k: int) -> list[int]: ...

    def randrange(self, stop: int) -> int: ...

    def shuffle(self, values: MutableSequence[_T]) -> None: ...


def _validate_limit(limit: int) -> int:
    limit = int(limit)
    if not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError("review sample limit must be between 1 and 100")
    return limit


def _key_from_row(row: Any) -> ReviewBundleKey:
    return ReviewBundleKey(
        run_id=int(row["run_id"]),
        row_id=int(row["row_id"]),
        sheet_id=int(row["sheet_id"]),
        sheet_name=str(row["sheet_name"]),
        action_kind=str(row["action_kind"]),
        model=str(row["model"]) if row["model"] is not None else None,
        confidence=(
            float(row["confidence"]) if row["confidence"] is not None else None
        ),
    )


def _eligible_key_query(
    *,
    field_id: int | None,
    include_reviewed: bool,
    row_id_count: int | None,
    ordered: bool,
) -> str:
    review_filter = (
        ""
        if include_reviewed
        else "AND res.review_state='unreviewed' AND res.review_decision IS NULL "
    )
    field_filter = "AND res.column_id=? " if field_id is not None else ""
    row_filter = (
        "AND res.row_id IN (" + ",".join("?" for _ in range(row_id_count)) + ") "
        if row_id_count
        else ""
    )
    order_by = "ORDER BY res.row_id" if ordered else ""
    return f"""
        SELECT res.run_id,res.row_id,c.sheet_id,s.name AS sheet_name,
               runs.action_kind,runs.model,MIN(res.confidence) AS confidence
        FROM results res
        JOIN run_review_fields review_field
          ON review_field.run_id=res.run_id
         AND review_field.column_id=res.column_id
         AND review_field.is_primary=1
        JOIN columns c ON c.id=res.column_id
        JOIN sheets s ON s.id=c.sheet_id
        JOIN rows rr ON rr.id=res.row_id AND rr.sheet_id=c.sheet_id
        JOIN runs ON runs.id=res.run_id
        WHERE res.run_id=? AND runs.status<>'running'
          AND EXISTS (SELECT 1 FROM ops review_op
                      WHERE review_op.id=runs.op_id
                        AND review_op.status='applied')
          AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
          {review_filter}{field_filter}{row_filter}
        GROUP BY res.row_id
        {order_by}
    """


def _eligible_keys_for_rows(
    project: Project,
    *,
    run_id: int,
    row_ids: Sequence[int],
    field_id: int | None,
    include_reviewed: bool,
) -> dict[int, ReviewBundleKey]:
    found: dict[int, ReviewBundleKey] = {}
    for start in range(0, len(row_ids), _PROBE_QUERY_CHUNK):
        chunk = row_ids[start : start + _PROBE_QUERY_CHUNK]
        if not chunk:
            continue
        params: list[Any] = [int(run_id)]
        if field_id is not None:
            params.append(int(field_id))
        params.extend(int(row_id) for row_id in chunk)
        query = _eligible_key_query(
            field_id=field_id,
            include_reviewed=include_reviewed,
            row_id_count=len(chunk),
            ordered=False,
        )
        for row in project.db.execute(query, params):
            key = _key_from_row(row)
            found[int(key["row_id"])] = key
    return found


def _eligible_key_stream(
    project: Project,
    *,
    run_id: int,
    field_id: int | None,
    include_reviewed: bool,
) -> Iterable[ReviewBundleKey]:
    params: list[Any] = [int(run_id)]
    if field_id is not None:
        params.append(int(field_id))
    query = _eligible_key_query(
        field_id=field_id,
        include_reviewed=include_reviewed,
        row_id_count=None,
        ordered=True,
    )
    for row in project.db.execute(query, params):
        yield _key_from_row(row)


def _result_row_bounds(
    project: Project, *, run_id: int, field_id: int | None
) -> tuple[int, int] | None:
    field_filter = " AND column_id=?" if field_id is not None else ""
    params: tuple[int, ...] = (
        (int(run_id), int(field_id)) if field_id is not None else (int(run_id),)
    )
    first = project.db.execute(
        "SELECT row_id FROM results WHERE run_id=?"
        f"{field_filter} ORDER BY row_id LIMIT 1",
        params,
    ).fetchone()
    if first is None:
        return None
    last = project.db.execute(
        "SELECT row_id FROM results WHERE run_id=?"
        f"{field_filter} ORDER BY row_id DESC LIMIT 1",
        params,
    ).fetchone()
    assert last is not None
    return int(first["row_id"]), int(last["row_id"])


def _reservoir_fill(
    candidates: Iterable[ReviewBundleKey],
    *,
    capacity: int,
    excluded: set[int],
    rng: _RandomSource,
) -> list[ReviewBundleKey]:
    reservoir: list[ReviewBundleKey] = []
    seen = 0
    for candidate in candidates:
        row_id = int(candidate["row_id"])
        if row_id in excluded:
            continue
        seen += 1
        if len(reservoir) < capacity:
            reservoir.append(candidate)
            continue
        replacement = rng.randrange(seen)
        if replacement < capacity:
            reservoir[replacement] = candidate
    rng.shuffle(reservoir)
    return reservoir


def _page(
    project: Project,
    *,
    keys: list[ReviewBundleKey],
    limit: int,
    total: int,
    has_more: bool,
) -> dict[str, Any]:
    return {
        "schema_version": _PAGE_SCHEMA,
        "offset": 0,
        "limit": limit,
        "total": total,
        "has_more": has_more,
        "next_offset": None,
        "next_cursor": None,
        "bundles": _hydrate_review_bundles(project, keys),
    }


def sample_review_bundle_page(
    project: Project,
    *,
    run_id: int,
    field_id: int | None = None,
    include_reviewed: bool = False,
    limit: int = 25,
    exclude_row_ids: Sequence[int] = (),
    rng: _RandomSource | None = None,
) -> dict[str, Any]:
    """Return one unbiased random batch without ranking the whole run."""

    limit = _validate_limit(limit)
    total = review_bundle_count(
        project,
        run_id=int(run_id),
        include_reviewed=include_reviewed,
        field_id=int(field_id) if field_id is not None else None,
    )
    if total == 0:
        return _page(project, keys=[], limit=limit, total=0, has_more=False)

    random_source: _RandomSource = rng or random.SystemRandom()
    excluded = {int(row_id) for row_id in exclude_row_ids}
    target = limit + 1
    selected: list[ReviewBundleKey] = []
    bounds = _result_row_bounds(
        project,
        run_id=int(run_id),
        field_id=int(field_id) if field_id is not None else None,
    )
    if bounds is not None:
        lower, upper = bounds
        span = upper - lower + 1
        probe_count = min(span, _PROBE_FACTOR * target)
        probes = random_source.sample(range(lower, upper + 1), probe_count)
        eligible = _eligible_keys_for_rows(
            project,
            run_id=int(run_id),
            row_ids=probes,
            field_id=int(field_id) if field_id is not None else None,
            include_reviewed=include_reviewed,
        )
        for row_id in probes:
            key = eligible.get(row_id)
            if key is not None and row_id not in excluded:
                selected.append(key)
                excluded.add(row_id)
                if len(selected) == target:
                    break

    if len(selected) < target:
        selected.extend(
            _reservoir_fill(
                _eligible_key_stream(
                    project,
                    run_id=int(run_id),
                    field_id=int(field_id) if field_id is not None else None,
                    include_reviewed=include_reviewed,
                ),
                capacity=target - len(selected),
                excluded=excluded,
                rng=random_source,
            )
        )

    has_more = len(selected) > limit
    return _page(
        project,
        keys=selected[:limit],
        limit=limit,
        total=total,
        has_more=has_more,
    )


def exact_review_bundle_page(
    project: Project,
    *,
    run_id: int,
    row_ids: Sequence[int],
    field_id: int | None = None,
    include_reviewed: bool = False,
) -> dict[str, Any]:
    """Hydrate currently eligible rows in caller order for stable Back pages."""

    requested = list(dict.fromkeys(int(row_id) for row_id in row_ids))
    if len(requested) > _MAX_PAGE_SIZE:
        raise ValueError("exact review pages support at most 100 rows")
    total = review_bundle_count(
        project,
        run_id=int(run_id),
        include_reviewed=include_reviewed,
        field_id=int(field_id) if field_id is not None else None,
    )
    keys_by_row = _eligible_keys_for_rows(
        project,
        run_id=int(run_id),
        row_ids=requested,
        field_id=int(field_id) if field_id is not None else None,
        include_reviewed=include_reviewed,
    )
    keys = [keys_by_row[row_id] for row_id in requested if row_id in keys_by_row]
    return _page(
        project,
        keys=keys,
        limit=max(1, len(requested)),
        total=total,
        has_more=False,
    )


__all__ = ["exact_review_bundle_page", "sample_review_bundle_page"]
