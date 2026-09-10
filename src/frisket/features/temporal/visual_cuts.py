"""PySceneDetect-backed visual scene boundaries.

The detector returns exact frame/time observations.  This module only converts
those observations into Frisket's generic, source-bound ``timeline_points``
value; it does not split media or write project state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from frisket.features.temporal_values import TimelineAnchor, TimelinePointsValue


DEFAULT_CONTENT_THRESHOLD = 27.0
DEFAULT_MIN_SCENE_LEN_FRAMES = 15


@dataclass(frozen=True)
class VisualCut:
    at_ms: int
    frame: int
    timecode: str


def _milliseconds(seconds: float) -> int:
    return int(
        (Decimal(str(seconds)) * Decimal(1000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def detect_visual_cuts(
    path: str | Path,
    *,
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_scene_len_frames: int = DEFAULT_MIN_SCENE_LEN_FRAMES,
) -> tuple[VisualCut, ...]:
    """Return the start of every detected scene after the first.

    PySceneDetect exceptions deliberately pass through to the ordinary map-row
    error path.  There is no isolation subprocess or detector-specific recovery
    policy.
    """

    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(
            threshold=float(threshold),
            min_scene_len=int(min_scene_len_frames),
        )
    )
    manager.detect_scenes(video=video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)

    cuts: list[VisualCut] = []
    seen_ms: set[int] = set()
    for start, _end in scenes[1:]:
        at_ms = _milliseconds(float(start.get_seconds()))
        if at_ms in seen_ms:
            continue
        seen_ms.add(at_ms)
        cuts.append(
            VisualCut(
                at_ms=at_ms,
                frame=int(start.get_frames()),
                timecode=str(start.get_timecode()),
            )
        )
    return tuple(cuts)


def visual_cuts_value(
    path: str | Path,
    *,
    timeline: TimelineAnchor | dict[str, Any],
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_scene_len_frames: int = DEFAULT_MIN_SCENE_LEN_FRAMES,
) -> dict[str, Any]:
    """Detect internal cuts and bind them to ``timeline``."""

    anchor = (
        timeline
        if isinstance(timeline, TimelineAnchor)
        else TimelineAnchor.model_validate(timeline)
    )
    items: list[dict[str, Any]] = []
    for cut in detect_visual_cuts(
        path,
        threshold=threshold,
        min_scene_len_frames=min_scene_len_frames,
    ):
        if cut.at_ms <= 0:
            continue
        if anchor.duration_ms is not None and cut.at_ms >= anchor.duration_ms:
            continue
        items.append(
            {
                "id": f"visual-cut-{cut.frame:010d}",
                "at_ms": cut.at_ms,
                "metadata": {
                    "detector": "pyscenedetect.content",
                    "frame": cut.frame,
                    "native_timecode": cut.timecode,
                    "threshold": float(threshold),
                    "min_scene_len_frames": int(min_scene_len_frames),
                },
            }
        )
    return TimelinePointsValue.model_validate(
        {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": anchor.model_dump(mode="json"),
            "items": items,
        }
    ).model_dump(mode="json")


__all__ = [
    "DEFAULT_CONTENT_THRESHOLD",
    "DEFAULT_MIN_SCENE_LEN_FRAMES",
    "VisualCut",
    "detect_visual_cuts",
    "visual_cuts_value",
]
