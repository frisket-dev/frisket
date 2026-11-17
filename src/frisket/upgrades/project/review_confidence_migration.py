"""Add the ordered result index used by confidence review pages."""

from __future__ import annotations
from frisket.upgrades.project._step import SchemaStep
from frisket.upgrades.project.review_stats_migration import REVIEW_STATS_TO_DIGEST


REVIEW_CONFIDENCE_FROM_DIGEST = REVIEW_STATS_TO_DIGEST
REVIEW_CONFIDENCE_TO_DIGEST = "frisket.schema.v1:7a0c04a6a0810573f0fa6253759a87a5"

REVIEW_CONFIDENCE_STEP: SchemaStep = (
    REVIEW_CONFIDENCE_FROM_DIGEST,
    REVIEW_CONFIDENCE_TO_DIGEST,
    (
        "CREATE INDEX IF NOT EXISTS idx_results_run_col_confidence_row "
        "ON results(run_id,column_id,confidence,row_id)",
    ),
)


__all__ = [
    "REVIEW_CONFIDENCE_FROM_DIGEST",
    "REVIEW_CONFIDENCE_STEP",
    "REVIEW_CONFIDENCE_TO_DIGEST",
]
