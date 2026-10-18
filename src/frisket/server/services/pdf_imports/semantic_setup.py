"""Shared semantic-search setup facts for reviewed PDF imports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol


class SemanticSetupRequirement(Protocol):
    setup_ref: str
    display_name: str
    runtime_ready: bool
    weights_ready: bool
    runtime_installable: bool
    download_bytes_if_missing: int
    license: str
    source_url: str


def bind_semantic_setup_consent(value: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the exact consent hash shown during PDF import review."""

    facts = dict(value)
    binding = {
        key: facts.get(key)
        for key in (
            "setup_ref",
            "display_name",
            "runtime_ready",
            "download_bytes",
            "additional_runtime_download",
            "license",
            "source_url",
            "available",
        )
    }
    needs_confirmation = bool(facts.get("available", True) and not facts.get("ready"))
    return {
        **facts,
        "needs_confirmation": needs_confirmation,
        "confirmation_hash": (
            hashlib.sha256(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if needs_confirmation
            else None
        ),
    }


def semantic_setup_facts(
    requirement: SemanticSetupRequirement,
) -> dict[str, Any]:
    """Project one engine requirement into reviewed PDF setup facts."""

    ready = bool(requirement.runtime_ready and requirement.weights_ready)
    return bind_semantic_setup_consent(
        {
            "setup_ref": str(requirement.setup_ref),
            "display_name": str(requirement.display_name),
            "ready": ready,
            "runtime_ready": bool(requirement.runtime_ready),
            "download_bytes": (
                None
                if requirement.weights_ready
                else int(requirement.download_bytes_if_missing)
            ),
            "additional_runtime_download": bool(
                requirement.runtime_installable and not requirement.runtime_ready
            ),
            "license": str(requirement.license),
            "source_url": str(requirement.source_url),
            "available": bool(
                ready or requirement.runtime_ready or requirement.runtime_installable
            ),
        }
    )


__all__ = ["bind_semantic_setup_consent", "semantic_setup_facts"]
