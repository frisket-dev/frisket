"""Current project schema upgrade chain tip digest.

Migrations that install live runtime DDL compare ``SCHEMA_DIGEST`` against this
single constant so appending a new schema step updates one tip definition
instead of every guarded migration module.
"""

from __future__ import annotations

from frisket.upgrades.project.feeds_migration import (
    FEEDS_TO_DIGEST,
)

CURRENT_CHAIN_TIP_DIGEST = FEEDS_TO_DIGEST
