"""Extend bounded text caching and index native numeric ranges without rewriting data."""

from __future__ import annotations
import sqlite3
from frisket.upgrades.project._step import SchemaStep
from frisket.upgrades.project.historical_current_cells import backfill_short_text_values
from frisket.engine.store.schema import NUMERIC_CURRENT_INDEX_SQL


SHORT_TEXT_CACHE_FROM_DIGEST = "frisket.schema.v1:bd1859c234b8d13f81e7c64a694579a2"
SHORT_TEXT_CACHE_TO_DIGEST = "frisket.schema.v1:bba6b2017e73276ec859d32dc995b59a"


def _apply_short_text_cache(db: sqlite3.Connection) -> None:
    backfill_short_text_values(db)
    db.execute(NUMERIC_CURRENT_INDEX_SQL)


SHORT_TEXT_CACHE_STEP: SchemaStep = (
    SHORT_TEXT_CACHE_FROM_DIGEST,
    SHORT_TEXT_CACHE_TO_DIGEST,
    _apply_short_text_cache,
)
