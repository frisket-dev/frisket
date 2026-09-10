"""Queued-vs-direct policy for v1 action execution.

This is a guardrail, not an executor dispatch table. It records which
catalog-visible action kinds are expected to use the durable project.run queue
and which ones are intentionally direct for the current v1 boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from frisket.contracts.action import ActionCatalog


QUEUE_POLICY_CAPABILITY_PREFIXES = ("external:", "model:", "unsafe:")
QUEUE_POLICY_EXECUTION_MODES = {
    "batch_deduped",
    "cross_sheet",
    "grouped",
    "per_row",
    "source_poll",
}


@dataclass(frozen=True)
class DirectV1ActionPolicy:
    reason: str
    owner: str


INTENTIONALLY_DIRECT_V1_ACTIONS = MappingProxyType(
    {
        "map.template": DirectV1ActionPolicy(
            reason="Deterministic local projection remains synchronous.",
            owner="typed-map-runtime",
        ),
        "map.columns_from_json": DirectV1ActionPolicy(
            reason="Deterministic local JSON projection remains synchronous.",
            owner="typed-map-runtime",
        ),
        "map.clean_column": DirectV1ActionPolicy(
            reason="Deterministic local column cleanup remains synchronous.",
            owner="typed-map-runtime",
        ),
        "map.clean_dates": DirectV1ActionPolicy(
            reason="Deterministic local date cleanup remains synchronous.",
            owner="typed-map-runtime",
        ),
        "import.urls": DirectV1ActionPolicy(
            reason=(
                "URL import uses admitted acquisition and atomic typed table "
                "publication, not the project.run row worker queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.append_csv": DirectV1ActionPolicy(
            reason=(
                "CSV append uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.append_rows": DirectV1ActionPolicy(
            reason=(
                "Row append uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.append_xlsx": DirectV1ActionPolicy(
            reason=(
                "XLSX append uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.update_csv": DirectV1ActionPolicy(
            reason=(
                "CSV update uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.update_rows": DirectV1ActionPolicy(
            reason=(
                "Row update uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "import.update_xlsx": DirectV1ActionPolicy(
            reason=(
                "XLSX update uses the typed-table mutation runtime and publishes "
                "its receipt inline, not through the project.run row queue."
            ),
            owner="typed-table-runtime",
        ),
        "source.poll": DirectV1ActionPolicy(
            reason=(
                "Generic source polling owns source_items/source_runs receipt "
                "semantics and is invoked either directly for manual polls or "
                "through the generic JobQueue's source.poll worker, not the "
                "project.run MapRunner queue."
            ),
            owner="source-runtime",
        ),
        "embedding.index_create": DirectV1ActionPolicy(
            reason=(
                "Index creation publishes metadata inline and may perform one "
                "bounded, consented dimension probe under durable receipt "
                "reservation/checkpoint authority. Bulk vector work belongs "
                "to the separately queued embedding.index_refresh action."
            ),
            owner="typed-embedding-lifecycle",
        ),
        "media.enclosure_materialize": DirectV1ActionPolicy(
            reason=(
                "Enclosure materialization is row-local source media handling "
                "and remains sync/direct through the typed enclosure capability."
            ),
            owner="media-runtime",
        ),
        "web.capture_page": DirectV1ActionPolicy(
            reason=(
                "Page capture writes bounded per-row HTML artifacts and receipt "
                "evidence through the media family. The v1 executor keeps it "
                "direct until capture-specific queue ownership is admitted."
            ),
            owner="media-runtime",
        ),
        # join.semantic's queued spec is the hand-built `_QueuedActionSpec` in
        # executor/action_families/semantic.py next to
        # `_queued_resolve_join_semantic`, not an entry in this dict.
        # enrich.geocode, enrich.census_demographics, and map.python queue via
        # queued_project_run; their request-time gates (geocode's cost
        # confirmation, census's credential gate) are unaffected: both fire in
        # ActionRunService.run_action before placement branching.
        "reduce.group_summary": DirectV1ActionPolicy(
            reason=(
                "Grouped reduce uses reduce-family aggregation and receipt "
                "semantics rather than project.run queue ownership in v1."
            ),
            owner="reduce-runtime",
        ),
        "run.backfill": DirectV1ActionPolicy(
            reason=(
                "Backfill is tied to existing column run/retry controls and stays "
                "direct until run-control queue semantics are admitted separately."
            ),
            owner="run-controls",
        ),
    }
)


def queue_policy_candidate_action_kinds(
    catalog: ActionCatalog | None = None,
) -> set[str]:
    if catalog is None:
        from frisket.actions.system import root_action_catalog

        catalog = root_action_catalog()
    candidates: set[str] = set()
    for entry in catalog.actions:
        if not entry.writes_project or entry.receipt_policy != "writes_receipt":
            continue
        has_policy_capability = any(
            capability.startswith(QUEUE_POLICY_CAPABILITY_PREFIXES)
            for capability in entry.required_capabilities
        )
        if (
            has_policy_capability
            or entry.async_mode == "queued"
            or entry.execution_mode in QUEUE_POLICY_EXECUTION_MODES
        ):
            candidates.add(entry.kind)
    return candidates
