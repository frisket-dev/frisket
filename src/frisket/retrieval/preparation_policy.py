"""Typed project policy for bounded retrieval preparation."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any

from frisket.retrieval.content import TextChunkRecipe, TranscriptChunkRecipe
from frisket.retrieval.media import (
    AudioWindowRecipe,
    FixedClipRecipe,
    PeriodicFrameRecipe,
    ShotClipRecipe,
)


PREPARATION_POLICY_META_KEY = "retrieval_preparation_policy_v1"
DEFAULT_TEXT_RECIPE = TextChunkRecipe()
DEFAULT_TRANSCRIPT_RECIPE = TranscriptChunkRecipe()
DEFAULT_VIDEO_RECIPE = ShotClipRecipe()
DEFAULT_AUDIO_RECIPE: AudioWindowRecipe | None = None


@dataclass(frozen=True, slots=True)
class PreparationPolicy:
    """Persistent selection of bounded local producer strategies."""

    text_recipe: TextChunkRecipe | None = DEFAULT_TEXT_RECIPE
    transcript_recipe: TranscriptChunkRecipe | None = DEFAULT_TRANSCRIPT_RECIPE
    image_enabled: bool = True
    pdf_pages_enabled: bool = True
    video_recipe: PeriodicFrameRecipe | FixedClipRecipe | ShotClipRecipe | None = (
        DEFAULT_VIDEO_RECIPE
    )
    audio_recipe: AudioWindowRecipe | None = DEFAULT_AUDIO_RECIPE

    def __post_init__(self) -> None:
        if self.text_recipe is not None and not isinstance(
            self.text_recipe, TextChunkRecipe
        ):
            raise TypeError("text_recipe must be TextChunkRecipe or None")
        if self.transcript_recipe is not None and not isinstance(
            self.transcript_recipe, TranscriptChunkRecipe
        ):
            raise TypeError("transcript_recipe must be TranscriptChunkRecipe or None")
        if not isinstance(self.image_enabled, bool):
            raise TypeError("image_enabled must be boolean")
        if not isinstance(self.pdf_pages_enabled, bool):
            raise TypeError("pdf_pages_enabled must be boolean")
        if self.video_recipe is not None and not isinstance(
            self.video_recipe, PeriodicFrameRecipe | FixedClipRecipe | ShotClipRecipe
        ):
            raise TypeError("video_recipe must be a video recipe or None")
        if self.audio_recipe is not None and not isinstance(
            self.audio_recipe, AudioWindowRecipe
        ):
            raise TypeError("audio_recipe must be AudioWindowRecipe or None")


DEFAULT_PREPARATION_POLICY = PreparationPolicy()
_MISSING = object()
_POLICY_KEYS = frozenset(field.name for field in dataclasses.fields(PreparationPolicy))


def _knob(name: str) -> str:
    return f"{PREPARATION_POLICY_META_KEY}.{name}"


def _recipe_payload(recipe: object) -> dict[str, object] | None:
    return None if recipe is None else dataclasses.asdict(recipe)


def preparation_policy_json(policy: PreparationPolicy) -> str:
    if not isinstance(policy, PreparationPolicy):
        raise TypeError("policy must be a PreparationPolicy")
    return json.dumps(
        {
            "text_recipe": _recipe_payload(policy.text_recipe),
            "transcript_recipe": _recipe_payload(policy.transcript_recipe),
            "image_enabled": policy.image_enabled,
            "pdf_pages_enabled": policy.pdf_pages_enabled,
            "video_recipe": _recipe_payload(policy.video_recipe),
            "audio_recipe": _recipe_payload(policy.audio_recipe),
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _optional_recipe(
    name: str,
    value: object,
    default: object,
    recipes: dict[str, type[Any]],
) -> object:
    if value is _MISSING:
        return default
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{_knob(name)} must be an object or null")
    strategy = value.get("strategy")
    recipe_type = recipes.get(strategy) if isinstance(strategy, str) else None
    if recipe_type is None:
        raise ValueError(f"{_knob(name)}.strategy is unsupported: {strategy!r}")
    allowed = {field.name for field in dataclasses.fields(recipe_type)}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(
            f"unknown preparation policy knob {_knob(name)}.{unexpected[0]}"
        )
    try:
        return recipe_type(**value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {_knob(name)}: {exc}") from exc


def _boolean(name: str, value: object, default: bool) -> bool:
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{_knob(name)} must be boolean")
    return value


def parse_preparation_policy(raw: str) -> PreparationPolicy:
    """Parse stored JSON, refusing malformed or unknown configuration knobs."""

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{PREPARATION_POLICY_META_KEY} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{PREPARATION_POLICY_META_KEY} must be an object")
    unexpected = sorted(set(value) - _POLICY_KEYS)
    if unexpected:
        raise ValueError(f"unknown preparation policy knob {_knob(unexpected[0])}")
    return PreparationPolicy(
        text_recipe=_optional_recipe(
            "text_recipe",
            value.get("text_recipe", _MISSING),
            DEFAULT_PREPARATION_POLICY.text_recipe,
            {"text_codepoint_windows": TextChunkRecipe},
        ),
        transcript_recipe=_optional_recipe(
            "transcript_recipe",
            value.get("transcript_recipe", _MISSING),
            DEFAULT_PREPARATION_POLICY.transcript_recipe,
            {"transcript_segment_windows": TranscriptChunkRecipe},
        ),
        image_enabled=_boolean(
            "image_enabled",
            value.get("image_enabled", _MISSING),
            DEFAULT_PREPARATION_POLICY.image_enabled,
        ),
        pdf_pages_enabled=_boolean(
            "pdf_pages_enabled",
            value.get("pdf_pages_enabled", _MISSING),
            DEFAULT_PREPARATION_POLICY.pdf_pages_enabled,
        ),
        video_recipe=_optional_recipe(
            "video_recipe",
            value.get("video_recipe", _MISSING),
            DEFAULT_PREPARATION_POLICY.video_recipe,
            {
                "periodic_frames": PeriodicFrameRecipe,
                "fixed_clips": FixedClipRecipe,
                "shot_clips_ffmpeg_scene_v1": ShotClipRecipe,
            },
        ),
        audio_recipe=_optional_recipe(
            "audio_recipe",
            value.get("audio_recipe", _MISSING),
            DEFAULT_PREPARATION_POLICY.audio_recipe,
            {"audio_windows": AudioWindowRecipe},
        ),
    )


def read_preparation_policy(project) -> PreparationPolicy:
    raw = project.get_meta(PREPARATION_POLICY_META_KEY)
    if raw is None:
        return DEFAULT_PREPARATION_POLICY
    return parse_preparation_policy(raw)


def write_preparation_policy(project, policy: PreparationPolicy) -> None:
    """Persist producer selection and enqueue one existing dirty-scope rebuild."""

    from frisket.engine.store.search_index_work import enqueue_dirty_scope

    encoded = preparation_policy_json(policy)
    with project.transaction_scope():
        project.set_meta(PREPARATION_POLICY_META_KEY, encoded, commit=False)
        enqueue_dirty_scope(project.db)


__all__ = [
    "DEFAULT_PREPARATION_POLICY",
    "DEFAULT_TEXT_RECIPE",
    "DEFAULT_TRANSCRIPT_RECIPE",
    "DEFAULT_VIDEO_RECIPE",
    "PREPARATION_POLICY_META_KEY",
    "PreparationPolicy",
    "parse_preparation_policy",
    "preparation_policy_json",
    "read_preparation_policy",
    "write_preparation_policy",
]
