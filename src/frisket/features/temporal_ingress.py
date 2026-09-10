"""Host-owned persistence validation for temporal column values.

The column-type registry deliberately performs only context-free validation.
Persisting a temporal value additionally requires proving that its embedded
timeline anchor belongs to an immutable artifact timeline in this project.
This module is the single host seam for that second validation layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from frisket.engine.store.artifact_timeline import (
    TimelineError,
    validate_timeline_anchor,
)
from frisket.features.temporal_values import parse_temporal_value

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from frisket.engine.store import Project


TEMPORAL_COLUMN_TYPES = frozenset(
    {
        "timeline_point",
        "timeline_points",
        "timeline_range",
        "timeline_ranges",
    }
)


def is_temporal_column_type(type_name: str) -> bool:
    """Return whether ``type_name`` requires project-context validation."""

    return type_name in TEMPORAL_COLUMN_TYPES


def validate_temporal_persistence_value(
    project: Project,
    *,
    type_name: str,
    value: Any,
) -> dict[str, Any] | None:
    """Validate one temporal value structurally and against project state.

    ``None`` remains the universal empty-cell value.  All other values are
    parsed through the exact schema for their declared temporal column type,
    then their host-owned timeline anchor is resolved and integrity checked.
    The normalized object is returned so writers may persist the same value
    that was validated.

    Failures use :class:`TimelineError`'s stable public vocabulary.  In
    particular, schema failures become ``invalid_temporal_value`` while store
    failures retain codes such as ``timeline_not_found`` and
    ``timeline_stale``.
    """

    if type_name not in TEMPORAL_COLUMN_TYPES:
        raise TimelineError(
            "invalid_temporal_value",
            f"{type_name!r} is not a temporal column type",
        )
    if value is None:
        return None
    try:
        parsed = parse_temporal_value(type_name, value)
    except (
        LookupError,
        OverflowError,
        RecursionError,
        TypeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise TimelineError(
            "invalid_temporal_value",
            f"value does not match the {type_name} schema",
        ) from exc

    normalized = parsed.model_dump(mode="json")
    resolved = validate_timeline_anchor(project, normalized["timeline"])
    if normalized["timeline"].get("duration_ms") is None:
        # The wire value may omit a known duration, but contextual ingress must
        # still enforce bounds against the resolved artifact clock.
        bounded = {
            **normalized,
            "timeline": {
                **normalized["timeline"],
                "duration_ms": resolved.duration_ms,
            },
        }
        try:
            parse_temporal_value(type_name, bounded)
        except (TypeError, ValidationError, ValueError) as exc:
            raise TimelineError(
                "range_out_of_bounds",
                "temporal coordinates exceed the resolved timeline duration",
            ) from exc
    return normalized


def validate_project_contextual_value(
    project: Project | None,
    *,
    type_name: str,
    value: Any,
) -> Any:
    """Apply the host-owned validation layer when ``type_name`` is temporal.

    Plugin adapters use this after their existing structural validation and
    immediately before handing values to a persistence path.  Non-temporal
    values are returned unchanged, preserving those adapters' established
    behavior.  A temporal value may never cross an unbound adapter: without a
    target project there is no trustworthy artifact namespace in which to
    resolve its anchor.
    """

    if not is_temporal_column_type(type_name):
        return value
    if project is None:
        raise TimelineError(
            "invalid_temporal_value",
            "temporal persistence requires target project context",
        )
    return validate_temporal_persistence_value(
        project,
        type_name=type_name,
        value=value,
    )


def preflight_typed_rows_for_persistence(
    project: Project,
    *,
    column_types_by_name: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Contextually validate typed rows before a writer publishes anything.

    Structural parsing remains owned by each action contract. This shared
    project-bound pass handles the part a Pydantic schema cannot: every
    temporal value must resolve to an immutable timeline in this project.
    Non-temporal values pass through unchanged. The returned copies contain
    the canonical temporal values that were actually validated.
    """

    if not any(
        is_temporal_column_type(type_name)
        for type_name in column_types_by_name.values()
    ):
        return [dict(row) for row in rows]

    validated_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        validated_row: dict[str, Any] = {}
        for name, value in row.items():
            type_name = column_types_by_name.get(name)
            if not isinstance(type_name, str) or not type_name:
                raise TimelineError(
                    "invalid_temporal_value",
                    f"typed row {row_index} has no declared type for column {name!r}",
                )
            try:
                validated_row[name] = validate_project_contextual_value(
                    project,
                    type_name=type_name,
                    value=value,
                )
            except TimelineError as exc:
                raise TimelineError(
                    exc.code,
                    f"row {row_index} column {name!r}: {exc.message}",
                ) from exc
        validated_rows.append(validated_row)
    return validated_rows


__all__ = [
    "TEMPORAL_COLUMN_TYPES",
    "is_temporal_column_type",
    "preflight_typed_rows_for_persistence",
    "validate_project_contextual_value",
    "validate_temporal_persistence_value",
]
