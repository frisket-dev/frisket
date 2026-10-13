"""Validated model and capability catalog shared by every Frisket surface.

The bundled document is the offline fallback.  The existing daily pricing
refresher may install a newer Frisket-published copy at runtime; each candidate
is validated completely before this module swaps its single provider snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Generic, TypeVar

CATALOG_PATH = Path(__file__).with_name("model_catalog.json")
SCHEMA_VERSION = "frisket.model_catalog.v1"
SUPPORTED_PROVIDERS = frozenset(
    {"anthropic", "openai", "gemini", "openrouter", "ollama"}
)
MAX_MODELS_PER_PROVIDER = 100


_V = TypeVar("_V")


class _AtomicMapping(Mapping[str, _V], Generic[_V]):
    """A stable imported object whose complete backing snapshot can change."""

    def __init__(self, snapshot: dict[str, _V]) -> None:
        self._snapshot = snapshot

    def install(self, snapshot: dict[str, _V]) -> None:
        # Reference assignment is atomic on supported CPython runtimes. Readers
        # see the complete old or complete new mapping, never clear/update gaps.
        self._snapshot = snapshot

    def __getitem__(self, key: str) -> _V:
        return self._snapshot[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._snapshot)

    def __len__(self) -> int:
        return len(self._snapshot)

    def items(self):
        return self._snapshot.items()

    def keys(self):
        return self._snapshot.keys()

    def values(self):
        return self._snapshot.values()


def _validated_catalog(
    data: object,
) -> tuple[date, dict[str, list[dict[str, Any]]]]:
    if not isinstance(data, dict):
        raise ValueError("model catalog must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported model catalog schema version")
    updated = data.get("updated")
    if not isinstance(updated, str):
        raise ValueError("model catalog updated date must be a string")
    try:
        updated_date = date.fromisoformat(updated)
    except ValueError as exc:
        raise ValueError("model catalog updated date must be ISO-8601") from exc
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError("model catalog providers must be a non-empty object")
    provider_ids = set(providers)
    if provider_ids != SUPPORTED_PROVIDERS:
        missing = SUPPORTED_PROVIDERS - provider_ids
        unknown = provider_ids - SUPPORTED_PROVIDERS
        problem = min(missing) if missing else min(unknown)
        raise ValueError(
            f"model catalog provider set is incomplete or unknown: {problem}"
        )

    normalized: dict[str, list[dict[str, Any]]] = {}
    for provider, entries in providers.items():
        if not isinstance(provider, str) or not isinstance(entries, list):
            raise ValueError("invalid model catalog provider entries")
        if len(entries) > MAX_MODELS_PER_PROVIDER:
            raise ValueError("model catalog provider exceeds model limit")
        seen: set[str] = set()
        normalized_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "label", "vision"}:
                raise ValueError("invalid model catalog entry")
            model_id = entry["id"]
            label = entry["label"]
            vision = entry["vision"]
            if (
                not isinstance(model_id, str)
                or not model_id
                or model_id.strip() != model_id
                or not isinstance(label, str)
                or not label
                or type(vision) is not bool
            ):
                raise ValueError("invalid model catalog entry")
            if model_id in seen:
                raise ValueError(f"duplicate model catalog id: {provider}/{model_id}")
            seen.add(model_id)
            normalized_entries.append(
                {"id": model_id, "label": label, "vision": vision}
            )
        normalized[provider] = normalized_entries
    return updated_date, normalized


def load_model_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    _updated_date, providers = _validated_catalog(data)
    return {
        "schema_version": SCHEMA_VERSION,
        "updated": data["updated"],
        "providers": providers,
    }


_BUNDLED = load_model_catalog()
_installed_updated = date.fromisoformat(_BUNDLED["updated"])
MODEL_ENTRIES: _AtomicMapping[list[dict[str, Any]]] = _AtomicMapping(
    _BUNDLED["providers"]
)


class _ModelIds(Mapping[str, list[str]]):
    def __getitem__(self, provider: str) -> list[str]:
        return [entry["id"] for entry in MODEL_ENTRIES[provider]]

    def __iter__(self) -> Iterator[str]:
        return iter(MODEL_ENTRIES)

    def __len__(self) -> int:
        return len(MODEL_ENTRIES)


MODEL_CATALOG: Mapping[str, list[str]] = _ModelIds()


def install_model_catalog(data: object, *, newer_only: bool = False) -> None:
    """Validate and atomically replace the process-wide provider snapshot."""
    updated, providers = _validated_catalog(data)
    global _installed_updated
    if newer_only and updated < _installed_updated:
        raise ValueError("model catalog is older than the installed snapshot")
    MODEL_ENTRIES.install(providers)
    _installed_updated = updated
