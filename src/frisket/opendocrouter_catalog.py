"""Validated document-model metadata, shared by contracts and pricing.

The bundled snapshot and daily updates live in the existing pricing document.
This leaf has no provider clients or background work: consumers read one
immutable snapshot, and the pricing refresher replaces it only after validation.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_URL = "https://www.opendocrouter.ai/v1/models"
PREFIX = "opendocrouter/"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*(?:/[A-Za-z0-9][A-Za-z0-9._:-]*)+")


@dataclass(frozen=True)
class DocumentModel:
    id: str
    name: str
    version: str
    max_sync_pages: int
    max_charge_per_page_usd: float | None
    avg_charge_per_page_usd: float
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float

    @property
    def engine(self) -> str:
        return PREFIX + self.id


@dataclass(frozen=True)
class DocumentCatalog:
    price_version: str
    models: tuple[DocumentModel, ...]
    retired_models: tuple[DocumentModel, ...] = ()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid OpenDocRouter catalog text")
    return value


def _rate(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid OpenDocRouter rate")
    return float(value)


def _parse_models(rows: object, seen: set[str]) -> tuple[DocumentModel, ...]:
    if not isinstance(rows, list):
        raise ValueError("invalid OpenDocRouter catalog")
    models = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid OpenDocRouter model")
        try:
            model_id = _text(row["id"])
            if not _MODEL_ID.fullmatch(model_id) or model_id in seen:
                raise ValueError("invalid or duplicate OpenDocRouter model ID")
            limit = row["max_sync_pages"]
            if type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError("invalid OpenDocRouter sync page limit")
            rates = row["price_per_million_tokens"]
            models.append(
                DocumentModel(
                    model_id,
                    _text(row["name"]),
                    _text(row["version"]),
                    limit,
                    _rate(row["max_charge_per_page_usd"])
                    if row.get("max_charge_per_page_usd") is not None
                    else None,
                    _rate(row["avg_charge_per_page_usd"]),
                    _rate(rates["input"]),
                    _rate(rates["cached_input"]),
                    _rate(rates["output"]),
                )
            )
            seen.add(model_id)
        except (KeyError, TypeError) as exc:
            raise ValueError("incomplete OpenDocRouter model metadata") from exc
    return tuple(models)


def parse_catalog(data: object) -> DocumentCatalog:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("invalid OpenDocRouter catalog")
    seen: set[str] = set()
    models = _parse_models(data["data"], seen)
    if not models:
        raise ValueError("empty OpenDocRouter catalog")
    retired = _parse_models(data.get("retired_data", []), seen)
    return DocumentCatalog(_text(data.get("price_version")), models, retired)


def catalog_document(catalog: DocumentCatalog) -> dict:
    """Normalized published data; discard provider benchmark/extra metadata."""

    def row(model: DocumentModel) -> dict:
        return {
            "id": model.id,
            "name": model.name,
            "version": model.version,
            "max_sync_pages": model.max_sync_pages,
            "max_charge_per_page_usd": model.max_charge_per_page_usd,
            "avg_charge_per_page_usd": model.avg_charge_per_page_usd,
            "price_per_million_tokens": {
                "input": model.input_per_million,
                "cached_input": model.cached_input_per_million,
                "output": model.output_per_million,
            },
        }

    document = {
        "price_version": catalog.price_version,
        "data": [row(model) for model in catalog.models],
    }
    if catalog.retired_models:
        document["retired_data"] = [row(model) for model in catalog.retired_models]
    return document


_catalog = parse_catalog(
    json.loads((Path(__file__).parent / "ai/llm/pricing_data.json").read_text())[
        "opendocrouter"
    ]
)


def current_catalog() -> DocumentCatalog:
    return _catalog


def install_catalog(catalog: DocumentCatalog) -> None:
    global _catalog
    previous = _catalog
    active = {model.engine for model in catalog.models}
    retired = {
        model.engine: model
        for model in (*previous.models, *previous.retired_models)
        if model.engine not in active
    }
    retired.update(
        {
            model.engine: model
            for model in catalog.retired_models
            if model.engine not in active
        }
    )
    _catalog = DocumentCatalog(
        catalog.price_version,
        catalog.models,
        tuple(retired[engine] for engine in sorted(retired)),
    )


def known_models() -> tuple[DocumentModel, ...]:
    """Active discovery rows followed by durable, unlisted retired rows."""
    catalog = _catalog
    return (*catalog.models, *catalog.retired_models)


def find_model(engine: str) -> DocumentModel | None:
    return next((m for m in _catalog.models if m.engine == engine), None)


def find_known_model(engine: str) -> DocumentModel | None:
    return next((model for model in known_models() if model.engine == engine), None)
