from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from frisket.actions.core import GoogleSheetsExport
from frisket.actions.registry import ACTION_REGISTRY


# --- placement & lifecycle policy vocabulary -------------------------------


class PlacementPolicy(str, Enum):
    """Where an action's durable work runs."""

    # caller waits; resolve -> precheck -> compute -> write returns the result.
    INLINE = "inline"
    # current MapRunner queue lifecycle: a project ``runs`` row + project.run job.
    QUEUED_PROJECT_RUN = "queued_project_run"
    # generic queued action with no ``runs`` row (an action.run job).
    QUEUED_ACTION_JOB = "queued_action_job"


class RunCoordinatePolicy(str, Enum):
    """Whether provider-effect facts are guaranteed to carry run coordinates."""

    RUN_BOUND = "run_bound"
    RUNLESS = "runless"


@dataclass(frozen=True)
class ConfirmationPolicy:
    """Whether an action pauses for confirmation before durable execution."""

    required: bool = False
    # generic reason vocabulary.
    reason: (
        Literal[
            "model_cost",
            "external_metered",
            "remote_egress",
            "destructive",
            "irreversible_external",
            "unknown_estimate",
        ]
        | None
    ) = None

    @classmethod
    def none(cls) -> ConfirmationPolicy:
        return cls(required=False, reason=None)


@dataclass(frozen=True)
class IdempotencyPolicy:
    """How replay/idempotency is keyed for an action."""

    replay_existing_receipt: bool = True

    @classmethod
    def default(cls) -> IdempotencyPolicy:
        return cls(replay_existing_receipt=True)


@dataclass(frozen=True)
class ReservationPolicy:
    """Whether a running receipt is reserved before side effects."""

    reserves_receipt: bool = False

    @classmethod
    def none(cls) -> ReservationPolicy:
        return cls(reserves_receipt=False)


@dataclass(frozen=True)
class FailureCommitPolicy:
    """Whether a failed attempt persists a terminal receipt or rolls back."""

    persist_failure: bool = False

    @classmethod
    def rollback(cls) -> FailureCommitPolicy:
        return cls(persist_failure=False)

    @classmethod
    def persist(cls) -> FailureCommitPolicy:
        return cls(persist_failure=True)


@dataclass(frozen=True)
class ExternalClaimPolicy:
    """Claim-before-provider policy for irreversible external effects.

    This is not a placement policy. It names actions that must reserve their
    idempotency key before an external provider may create/update something, and
    whose post-claim provider failures must persist a terminal receipt.
    """

    claimed: bool = False
    provider: str | None = None
    operation: str | None = None

    @classmethod
    def none(cls) -> ExternalClaimPolicy:
        return cls(claimed=False, provider=None, operation=None)

    @classmethod
    def google_sheets_export(cls) -> ExternalClaimPolicy:
        return cls(
            claimed=True,
            provider="google_sheets",
            operation="export_tabs",
        )


@dataclass(frozen=True)
class CleanupPolicy:
    """Post-commit / cleanup hook presence."""

    has_cleanup: bool = False

    @classmethod
    def none(cls) -> CleanupPolicy:
        return cls(has_cleanup=False)


@dataclass(frozen=True)
class LifecyclePolicy:
    placement: PlacementPolicy
    confirmation: ConfirmationPolicy = field(default_factory=ConfirmationPolicy.none)
    idempotency: IdempotencyPolicy = field(default_factory=IdempotencyPolicy.default)
    reservation: ReservationPolicy = field(default_factory=ReservationPolicy.none)
    failure_commit: FailureCommitPolicy = field(
        default_factory=FailureCommitPolicy.rollback
    )
    external_claim: ExternalClaimPolicy = field(
        default_factory=ExternalClaimPolicy.none
    )
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy.none)


@dataclass(frozen=True)
class ActionExecutionSpec:
    """Declared execution facts for one first-party action kind.

    ``lifecycle.placement`` and ``run_coordinate`` are authoritative today.
    ``run_coordinate`` says only whether provider-effect facts are guaranteed to
    carry run/attempt/epoch coordinates; it does not describe placement, paidness,
    tariffs, admission, or whether an action actually invokes a provider.

    The other lifecycle axes (confirmation, idempotency, reservation,
    failure_commit, cleanup) still carry their defaults — runtime behavior for
    those axes lives in the executor seams, not here, until later stages populate
    them. Do not treat a defaulted axis as "this action does not confirm / reserve
    / persist failure"; consult the executor spec for that behavior until the axis
    is wired (tracked by the restructure lane).
    """

    kind: str
    params_model: type[BaseModel]
    lifecycle: LifecyclePolicy
    run_coordinate: RunCoordinatePolicy


# --- resolved facts & execution payloads (used from stage 3 on) ------------


@dataclass(frozen=True)
class ResolvedAction:
    """Resolved, side-effect-free facts about an action, threaded through
    compute -> write -> present and (redacted) into queued snapshots.

    ``snapshot()`` must stay JSON-only, deterministic, and redacted: no sqlite
    rows, Project objects, Path/file handles, provider clients, raw bytes,
    fetched bodies, secrets, or tokens.
    """

    sheet_id: int | None = None
    row_ids: tuple[int, ...] = ()
    input_columns: Mapping[str, Any] = field(default_factory=dict)
    runner_spec: Mapping[str, Any] | None = None
    output_targets: tuple[Mapping[str, Any], ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_resolve_dict(cls, resolved: Mapping[str, Any]) -> ResolvedAction:
        """Wrap a resolve_fn output dict as a ResolvedAction.

        The full resolve dict lives in ``facts`` so threaded writers/present can
        read any op-specific value (input_column, row_ids, blob_refs, ...) that
        used to be pulled out by ``write_kwargs_override``. Common structured
        fields are lifted when present for typed access.
        """

        facts = dict(resolved)
        # The lifted typed fields are advisory convenience only (writers read the
        # full dict from .facts). Coerce defensively so a stray / non-int value in a
        # worker payload becomes a clean omission, never a finalize-time crash; bool
        # is excluded because bool is an int subclass.
        row_ids: tuple[int, ...] = ()
        row_ids_raw = facts.get("row_ids")
        if isinstance(row_ids_raw, (list, tuple)):
            coerced: list[int] = []
            for value in row_ids_raw:
                if value is None or isinstance(value, bool):
                    continue
                try:
                    coerced.append(int(value))
                except (TypeError, ValueError):
                    continue
            row_ids = tuple(coerced)
        sheet_id_raw = facts.get("sheet_id")
        sheet_id: int | None = None
        if sheet_id_raw is not None and not isinstance(sheet_id_raw, bool):
            try:
                sheet_id = int(sheet_id_raw)
            except (TypeError, ValueError):
                sheet_id = None
        return cls(sheet_id=sheet_id, row_ids=row_ids, facts=facts)

    def snapshot(self) -> dict[str, Any]:
        # The JSON round-trip is a fail-fast redaction/serializability guard, not
        # a hot path; do not call snapshot() in tight per-row loops.
        payload: dict[str, Any] = {
            "sheet_id": self.sheet_id,
            "row_ids": list(self.row_ids),
            "input_columns": dict(self.input_columns),
            "runner_spec": dict(self.runner_spec) if self.runner_spec else None,
            "output_targets": [dict(t) for t in self.output_targets],
            "facts": dict(self.facts),
        }
        # Round-trip through STRICT JSON to fail fast on any non-serializable /
        # un-redacted value (sqlite rows, Path, bytes, provider clients) or
        # non-standard float (NaN/Infinity) leaking into facts.
        return json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))


# --- named principled outliers ---------------------------------------------
#
# These do not fit the forward compute/write/present action lifecycle and are
# intentionally not modelled as ActionExecutionSpec. Each must record why (the
# blank-mind test) so the registry coverage test stays honest.
ACCEPTED_EXECUTION_OUTLIERS: Mapping[str, str] = MappingProxyType({})


# --- declared run-coordinate guarantee -----------------------------------
#
# This axis is deliberately independent of placement and provider/admission
# policy. A kind is RUN_BOUND only when every provider-effect fact it can emit
# is guaranteed to carry run/attempt/epoch coordinates. RUNLESS means facts may
# legitimately omit those coordinates; it does not say whether a provider is
# called. CI requires these first-party execution-metadata declarations to
# partition the typed registry's action IDs; runtime-extensible recipe
# registrations are a separate domain.
_RUNLESS_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "cell.edit",
        "cell.edit_query",
        "column.add",
        "column.patch",
        "column.set_type",
        "derive.collection_expand",
        "derive.join",
        "derive.link_table",
        "derive.table_from_list",
        "derive.temporal_segments",
        "derive.transcript_segments",
        "embedding.index_cluster",
        "embedding.index_delete",
        "embedding.index_export",
        "embedding.index_project",
        "embedding.index_update_policy",
        "export.column_tables",
        "export.google_sheets",
        "export.sheet_csv",
        "export.sheet_jsonl",
        "export.sheet_parquet",
        "export.work_log",
        "import.csv",
        "import.email",
        "import.files",
        "import.geojson",
        "import.kml",
        "import.ndjson",
        "import.pdf",
        "import.rows",
        "import.runtime",
        "import.urls",
        "import.xlsx",
        "media.enclosure_materialize",
        # Accepted execution-spec outliers are non-provider actions, but they
        # still participate in the complete public run-coordinate partition.
        "operation.redo",
        "operation.undo",
        "plugin.load",
        "query.preview",
        "replay.accept",
        "replay.accept_column",
        "replay.dismiss",
        "map.find",
        "resolve.combine",
        "resolve.entities",
        "resolve.fill_missing",
        "resolve.replace",
        "resolve.substitute",
        "review.decision",
        "row.add",
        "row.delete",
        "sheet.refresh",
        "source.check",
        "source.create",
        "source.delete",
        "source.poll",
        "source.update",
        "temporal.extract_range",
        "web.capture_page",
    }
)
_RUN_BOUND_ACTION_KINDS: frozenset[str] = frozenset(
    set(ACTION_REGISTRY.action_ids) - _RUNLESS_ACTION_KINDS
)


# --- declared placement ----------------------------------------------------
#
# Placement is declared here; queue policy is a projection of this authority.
_QUEUED_PROJECT_RUN_KINDS: frozenset[str] = frozenset(
    {
        "cluster.values",
        "web.capture_screenshot",
        "enrich.census_demographics",
        "enrich.geocode",
        "join.semantic",
        "map.api_call",
        "map.ask",
        "map.classify",
        "map.extract",
        "map.mcp_extract",
        "map.find_topic_sections",
        "map.find_visual_cuts",
        "map.judge",
        "map.ner",
        "map.python",
        "map.regex_extract",
        "map.summarize",
        "map.to_geo_point",
        "map.translate",
        "media.extract_metadata",
        "media.extract_faces",
        "media.extract_pdf_tables",
        "media.fetch_url",
        "media.ocr",
        "media.to_markdown",
        "media.transcribe",
        "media.video_frames",
        "media.ytdlp_download",
        "research.answer",
        "research.web_search",
    }
)
# Declared queued_action_job placement: actions with no project `runs` row that
# dispatch through the generic action.run job.
_QUEUED_ACTION_JOB_KINDS: frozenset[str] = frozenset(
    {
        "embedding.index_refresh",
        "temporal.extract_range",
        "derive.temporal_segments",
        "derive.transcript_segments",
        "map.find",
    }
)


# First-party INLINE placement is an explicit declaration keyed to a one-line
# justification (local + bounded + no network/model + synchronous request/
# response semantics). CI contract tests require the inline, queued, and
# outlier execution-metadata sets to partition the typed registry exactly;
# runtime-extensible recipes are outside this inventory.
_INLINE_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "import.append_csv": "Bounded local CSV append publishes rows and its receipt in-request.",
        "import.append_rows": "Bounded row append publishes rows and its receipt in-request.",
        "import.append_xlsx": "Bounded local XLSX append publishes rows and its receipt in-request.",
        "import.update_csv": "Bounded local CSV update publishes cell edits and its receipt in-request.",
        "import.update_rows": "Bounded row update publishes cell edits and its receipt in-request.",
        "import.update_xlsx": "Bounded local XLSX update publishes cell edits and its receipt in-request.",
        "plugin.load": "Bounded local package validation and receipt without activation or network.",
        # -- direct row/column/cell CRUD: local sheet mutation, no network ---
        "cell.edit": "Single-cell write is local, bounded, and synchronous.",
        "cell.edit_query": (
            "Query-scoped cell edit resolves and applies a bounded set of cell "
            "writes synchronously in-request; no network."
        ),
        "column.add": "Column add is a local, bounded sheet-schema mutation.",
        "column.patch": "Column patch is a local, bounded column-metadata mutation.",
        "column.set_type": (
            "Column type change is a local schema mutation validated in-request."
        ),
        "row.add": "Row add is a local, bounded sheet mutation; no network.",
        "row.delete": "Row delete is a local, bounded sheet mutation; no network.",
        "review.decision": (
            "Review decision records a single accept/reject verdict "
            "synchronously; local, bounded."
        ),
        "replay.accept": (
            "Replay accept writes the fresh generated value as one local edit "
            "op synchronously; no network."
        ),
        "replay.accept_column": (
            "Replay accept-all writes one batched edit op over a column's "
            "pending cells synchronously; local, bounded, no network."
        ),
        "replay.dismiss": (
            "Replay dismiss records one durable keep-edit row synchronously; "
            "local, bounded, no network."
        ),
        "sheet.refresh": (
            "Sheet refresh recomputes already-loaded sheet state synchronously; "
            "local, bounded."
        ),
        "query.preview": (
            "Query preview evaluates local filters or stored vectors and may "
            "compute a bounded local query embedding synchronously; no network."
        ),
        "resolve.entities": (
            "Entity resolution reads an admitted cluster receipt and materializes "
            "its selected groups synchronously; no network."
        ),
        "resolve.substitute": (
            "Substitute rewrites column values through an explicit local "
            "mapping synchronously; no network."
        ),
        "resolve.replace": (
            "Replace applies ordered local match rules over column values "
            "synchronously; no network."
        ),
        "resolve.combine": (
            "Combine rewrites grouped column values to their canonicals "
            "synchronously; local, no network."
        ),
        "resolve.fill_missing": (
            "Fill missing writes locally computed fills for a column's "
            "missing cells synchronously; no network."
        ),
        # -- derive.* locals: in-memory transforms over already-loaded data ---
        "derive.collection_expand": (
            "Collection expand is a local list-to-rows transform over "
            "already-loaded data; no network."
        ),
        "derive.join": (
            "Derive join computes a local in-memory join across already-loaded "
            "sheets; no network."
        ),
        "derive.link_table": (
            "Link table derive builds a local join table from already-loaded "
            "rows; no network."
        ),
        "derive.table_from_list": (
            "Table-from-list derive materializes a local table from an "
            "in-request payload; no network."
        ),
        # -- export.* locals: request returns the rendered file --------------
        "export.column_tables": (
            "Column-table export renders already-loaded sheet data to a local "
            "file synchronously; request returns the file."
        ),
        "export.sheet_csv": (
            "CSV export renders already-loaded sheet data to a local file "
            "synchronously; request returns the file."
        ),
        "export.sheet_jsonl": (
            "JSONL export renders already-loaded sheet data to a local file "
            "synchronously; request returns the file."
        ),
        "export.sheet_parquet": (
            "Parquet export renders already-loaded sheet data to a local file "
            "synchronously; request returns the file."
        ),
        "export.work_log": (
            "Work-log export renders the op log to a local file synchronously; "
            "request returns the file."
        ),
        # -- embedding index metadata ops: no provider call (index_refresh   --
        # -- owns the model work, and is QUEUED_ACTION_JOB above)            --
        "embedding.index_cluster": (
            "Index cluster is local vector-cache math over an already-built "
            "index; no provider call."
        ),
        "embedding.index_create": (
            "Index create owns inline metadata publication and any checkpointed, "
            "metered remote dimension probe. Refresh owns bulk vector work."
        ),
        "embedding.index_delete": (
            "Index delete drops the index (and sidecar vectors) row/metadata "
            "synchronously; no provider call."
        ),
        "embedding.index_export": (
            "Index export reads already-computed vectors/metadata and writes a "
            "local file synchronously; no provider call."
        ),
        "embedding.index_project": (
            "Index projection computes a local 2D layout over already-built "
            "vectors; no provider call."
        ),
        "embedding.index_update_policy": (
            "Index policy update is a local metadata mutation on the index "
            "row; no provider call."
        ),
        # -- import.*: parses an already-uploaded file or in-request payload -
        "import.csv": "CSV import parses an already-uploaded file synchronously; bounded, no network.",
        "import.email": (
            "Email import streams server-issued EML/MBOX source references "
            "through trusted ingress; no network."
        ),
        "import.files": (
            "File import ingests already-uploaded files synchronously; "
            "bounded, no network."
        ),
        "import.geojson": (
            "GeoJSON import parses an already-uploaded file synchronously; "
            "bounded, no network."
        ),
        "import.kml": "KML import parses an already-uploaded file synchronously; bounded, no network.",
        "import.ndjson": (
            "NDJSON import parses an already-uploaded file synchronously; "
            "bounded, no network."
        ),
        "import.pdf": "PDF import parses an already-uploaded file synchronously; bounded, no network.",
        "import.rows": (
            "Row import ingests an in-request row payload synchronously; "
            "bounded, no network."
        ),
        "import.runtime": (
            "Runtime import invokes a project-admitted trusted importer synchronously; "
            "subprocess handlers retain their explicit executable trust and may use network."
        ),
        "import.xlsx": (
            "XLSX import parses an already-uploaded file synchronously; "
            "bounded, no network."
        ),
        "import.urls": (
            "URL import is a whole-project import transport with bounded URL "
            "fan-in and import receipt semantics; not a project.run "
            "map-style worker action in v1 (queue_policy.py tracks it as a "
            "risk-relevant direct action)."
        ),
        # -- map.*: deterministic local map execution, no network/model ------
        "map.template": "Template projection is deterministic local execution; no network or model.",
        "map.columns_from_json": "JSON projection is deterministic local execution; no network or model.",
        "map.clean_column": "Column cleanup is deterministic local execution; no network or model.",
        "map.clean_dates": "Date cleanup is deterministic local execution; no network or model.",
        # -- media.*: bounded per-row local work, not queued -----------------
        "media.enclosure_materialize": (
            "Enclosure materialization is row-local source media handling, "
            "sync/direct through the typed enclosure capability."
        ),
        "web.capture_page": (
            "Page capture writes bounded per-row HTML artifacts/receipt "
            "evidence through the media family; direct until capture-specific "
            "queue ownership is admitted."
        ),
        # -- operation history: bounded transactional cursor steps ----------
        "operation.undo": (
            "Undo applies one local op-log transition and receipt synchronously."
        ),
        "operation.redo": (
            "Redo applies one local op-log transition and receipt synchronously."
        ),
        # join.semantic joined _QUEUED_PROJECT_RUN_KINDS above; it is no
        # longer declared here.
        # -- source.*: source metadata/monitor mutation, not project.run -----
        "source.check": "Source check records source monitor status synchronously.",
        "source.create": "Source create mutates source metadata synchronously.",
        "source.delete": "Source delete mutates source metadata synchronously.",
        "source.poll": (
            "Generic source polling owns receipt semantics and runs via "
            "manual invocation or the generic JobQueue's source.poll worker, "
            "not the project.run MapRunner queue."
        ),
        "source.update": "Source update mutates source metadata synchronously.",
        # -- bounded single-call/single-row direct actions, kept out of the --
        # -- queue for reasons of their own -----------------------------------
        "reduce.group_summary": (
            "Grouped reduce uses reduce-family aggregation/receipt semantics "
            "rather than project.run queue ownership in v1."
        ),
        "run.backfill": (
            "Backfill is tied to existing column run/retry controls; direct "
            "until run-control queue semantics are admitted separately."
        ),
    }
)


def declared_queued_project_run_kinds() -> frozenset[str]:
    """The DECLARED queued-project-run placement set (the source of truth)."""

    return _QUEUED_PROJECT_RUN_KINDS


def declared_queued_action_job_kinds() -> frozenset[str]:
    """The DECLARED queued-action-job placement set (the source of truth)."""

    return _QUEUED_ACTION_JOB_KINDS | frozenset(
        registered.action_id
        for registered in ACTION_REGISTRY.actions
        if isinstance(registered.definition.run, GoogleSheetsExport)
    )


def declared_inline_kinds() -> Mapping[str, str]:
    """The DECLARED inline placement set: kind -> one-line justification.

    Every entry explicitly declares local, bounded, synchronous work with no
    network/model call. CI contract tests require this mapping, the two queued
    sets, and the outlier set to partition first-party typed action IDs exactly.
    """

    return _INLINE_KINDS


def _build_execution_registry() -> dict[str, ActionExecutionSpec]:
    registry: dict[str, ActionExecutionSpec] = {}
    for kind in sorted(
        set(ACTION_REGISTRY.action_ids) | declared_queued_action_job_kinds()
    ):
        if kind in ACCEPTED_EXECUTION_OUTLIERS:
            continue
        params_model = ACTION_REGISTRY.get(kind).definition.run.params_model
        placement = (
            PlacementPolicy.QUEUED_PROJECT_RUN
            if kind in _QUEUED_PROJECT_RUN_KINDS
            else PlacementPolicy.QUEUED_ACTION_JOB
            if kind in declared_queued_action_job_kinds()
            else PlacementPolicy.INLINE
        )
        lifecycle = LifecyclePolicy(placement=placement)
        if kind in ACTION_REGISTRY.action_ids and isinstance(
            ACTION_REGISTRY.get(kind).definition.run, GoogleSheetsExport
        ):
            lifecycle = LifecyclePolicy(
                placement=placement,
                confirmation=ConfirmationPolicy(
                    required=True,
                    reason="irreversible_external",
                ),
                reservation=ReservationPolicy(reserves_receipt=True),
                failure_commit=FailureCommitPolicy.persist(),
                external_claim=ExternalClaimPolicy.google_sheets_export(),
            )
        registry[kind] = ActionExecutionSpec(
            kind=kind,
            params_model=params_model,
            lifecycle=lifecycle,
            run_coordinate=(
                RunCoordinatePolicy.RUN_BOUND
                if kind in _RUN_BOUND_ACTION_KINDS
                else RunCoordinatePolicy.RUNLESS
            ),
        )
    return registry


@lru_cache(maxsize=1)
def execution_registry() -> Mapping[str, ActionExecutionSpec]:
    """The execution spec registry, built lazily and cached as an immutable map.

    Built on first access (not at import) so a future dispatch flip can import
    this module from ``frisket.executor.actions`` without an import cycle: the
    handler map is only pulled in when the registry is first queried, after
    ``frisket.executor.actions`` has finished importing.
    """

    return MappingProxyType(_build_execution_registry())


def execution_spec_for(kind: str) -> ActionExecutionSpec | None:
    return execution_registry().get(kind)


def queued_project_run_kinds() -> set[str]:
    return {
        kind
        for kind, spec in execution_registry().items()
        if spec.lifecycle.placement is PlacementPolicy.QUEUED_PROJECT_RUN
    }


def queued_action_job_kinds() -> set[str]:
    return {
        kind
        for kind, spec in execution_registry().items()
        if spec.lifecycle.placement is PlacementPolicy.QUEUED_ACTION_JOB
    }
