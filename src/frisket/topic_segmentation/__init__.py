"""Replaceable local engines for transcript topic segmentation."""

from importlib import import_module

from .contracts import (
    DETAIL_LEVELS,
    BetweenUnits,
    BoundaryCandidate,
    BoundaryLocator,
    DialogueSegmenter,
    DialogueUnit,
    EngineDefinition,
    ExactTime,
    PreflightResult,
    SegmentationCancelled,
    SegmentationContext,
    SegmentationEngineUnavailable,
    SegmentationExecutionError,
    SegmentationPreflightError,
    SegmentationResult,
    SegmentationSettingsError,
    SegmentationSnapshot,
    WithinUnit,
    validate_detail_settings,
)

_LAZY_EXPORTS = {
    "DeepTilingSegmenter": ".deep_tiling",
    "TextTilingSegmenter": ".texttiling",
    "ENGINES": ".engines",
    "ENGINE_BY_ID": ".engines",
    "UnknownDialogueEngineError": ".engines",
    "engine_catalog": ".engines",
    "engine_definitions": ".engines",
    "get_segmenter": ".engines",
    "static_engine_catalog": ".engines",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "DETAIL_LEVELS",
    "ENGINES",
    "ENGINE_BY_ID",
    "BetweenUnits",
    "BoundaryCandidate",
    "BoundaryLocator",
    "DeepTilingSegmenter",
    "DialogueSegmenter",
    "DialogueUnit",
    "EngineDefinition",
    "ExactTime",
    "PreflightResult",
    "SegmentationCancelled",
    "SegmentationContext",
    "SegmentationEngineUnavailable",
    "SegmentationExecutionError",
    "SegmentationPreflightError",
    "SegmentationResult",
    "SegmentationSettingsError",
    "SegmentationSnapshot",
    "TextTilingSegmenter",
    "UnknownDialogueEngineError",
    "WithinUnit",
    "engine_catalog",
    "engine_definitions",
    "get_segmenter",
    "static_engine_catalog",
    "validate_detail_settings",
]
