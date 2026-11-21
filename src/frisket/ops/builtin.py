"""Action program lookup by action kind, and the prompt hash of a program run."""

from __future__ import annotations

import json

from .base import ActionProgram


def get_action_program(action_kind: str) -> ActionProgram:
    if action_kind != action_kind.strip() or "." not in action_kind:
        raise ValueError(f"action kind {action_kind!r} is not canonical")
    raise ValueError(f"unknown action kind {action_kind!r}")


def prompt_hash_of(program: ActionProgram, spec: dict) -> str:
    import hashlib

    stable = json.dumps(
        {
            "action_kind": spec["action_kind"],
            "action_version": program.version,
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
