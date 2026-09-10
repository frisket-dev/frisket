"""The complete, immutable engine roster for Find topic changes.

This is intentionally a tuple and exact lookup map, not a registration API.
The durable action and scratch Compare surface both project their options from
this module so a third engine cannot require a second UI-owned list.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .contracts import DialogueSegmenter, EngineDefinition
from .deep_tiling import DeepTilingSegmenter
from .texttiling import TextTilingSegmenter


class UnknownDialogueEngineError(ValueError):
    """The request named no engine in the action-owned roster."""


# Recommended semantic analysis appears first; callers choosing a default must
# still choose the first AVAILABLE definition, never silently substitute an
# engine after a user selected one.
ENGINES: tuple[DialogueSegmenter, ...] = (
    DeepTilingSegmenter(),
    TextTilingSegmenter(),
)

_engine_map = {engine.catalog_definition.id: engine for engine in ENGINES}
if len(_engine_map) != len(ENGINES):  # import-time authoring invariant
    raise RuntimeError("duplicate dialogue segmentation engine id")

ENGINE_BY_ID: MappingProxyType[str, DialogueSegmenter] = MappingProxyType(_engine_map)


def get_segmenter(engine_id: str) -> DialogueSegmenter:
    """Resolve exactly one selected engine; there is deliberately no fallback."""

    try:
        return ENGINE_BY_ID[engine_id]
    except (KeyError, TypeError) as exc:
        raise UnknownDialogueEngineError(
            f"unknown dialogue segmentation engine: {engine_id!r}"
        ) from exc


def engine_definitions() -> tuple[EngineDefinition, ...]:
    """Return current definitions; local dependency availability is live."""

    return tuple(engine.definition for engine in ENGINES)


def engine_catalog() -> tuple[dict[str, Any], ...]:
    """Overlay live availability on the stable reporter-facing engine roster."""

    return tuple(
        {
            **metadata,
            "available": definition.available,
            "error": definition.error,
        }
        for metadata, definition in zip(
            static_engine_catalog(), engine_definitions(), strict=True
        )
    )


def static_engine_catalog() -> tuple[dict[str, Any], ...]:
    """Project immutable authoring metadata without inspecting this runtime."""

    return tuple(
        {
            "id": definition.id,
            "label": definition.label,
            "description": definition.description,
            "version": definition.version,
            "tier": "local",
            "recommended": definition.recommended,
        }
        for definition in (engine.catalog_definition for engine in ENGINES)
    )


__all__ = [
    "ENGINES",
    "ENGINE_BY_ID",
    "UnknownDialogueEngineError",
    "engine_catalog",
    "engine_definitions",
    "get_segmenter",
    "static_engine_catalog",
]
