"""Current project schema upgrade chain tip digest.

Migrations that install live runtime DDL compare ``SCHEMA_DIGEST`` against this
single constant so appending a new schema step updates one tip definition
instead of every guarded migration module.
"""

from __future__ import annotations

from frisket.upgrades.project.generated_columns_migration import (
    GENERATED_COLUMNS_TO_DIGEST,
)

CURRENT_CHAIN_TIP_DIGEST = GENERATED_COLUMNS_TO_DIGEST
