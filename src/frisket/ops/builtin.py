"""Plugin recipe lookup and hashing for shared runner programs."""

from __future__ import annotations

import json

from .base import Recipe


def get_recipe(action_kind: str) -> Recipe:
    from frisket.authoring.plugin_registry import default_registry

    if action_kind != action_kind.strip() or "." not in action_kind:
        raise ValueError(f"action kind {action_kind!r} is not canonical")
    recipe = default_registry().get_recipe(action_kind)
    if recipe is None:
        raise ValueError(f"unknown action kind {action_kind!r}")
    return recipe


def prompt_hash_of(recipe: Recipe, spec: dict) -> str:
    import hashlib

    stable = json.dumps(
        {
            "action_kind": spec["action_kind"],
            "action_version": recipe.version,
            "spec": {
                k: v
                for k, v in spec.items()
                if k
                not in (
                    "row_ids",
                    "sheet_id",
                    "confirmed",
                    "consented_promise_set_hash",
                )
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(stable.encode()).hexdigest()[:16]
