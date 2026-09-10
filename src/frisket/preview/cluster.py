"""Read-only cluster preview: compute duplicate-value groups for a column under
a chosen method, with no receipt and no side effects.

This is the cheap synchronous twin of the receipt-writing cluster commit action:
the group data it returns (``clusters`` + ``value_hash``) is exactly what the
web group-card UI renders before commit, and its ``value_hash`` is fed back as
the commit's ``expected_value_hash`` staleness guard. Preview and commit MUST
agree on the group computation, so both go through the shared helpers here
(``compute_method_clusters`` / ``source_value_hash``) rather than duplicating
the fingerprint/ngram/semantic dispatch.

Methods:
- ``fingerprint``        — token key collision (ops/cluster_fingerprint.fingerprint).
- ``ngram_fingerprint``  — OpenRefine char n-gram key (ops/cluster_fingerprint).
- ``semantic``           — embedding cosine single-linkage (ops/cluster). NO
  silent fingerprint fallback: no embedder -> unavailable error.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from frisket.authoring.templates import (
    KeyTemplateError,
    render_value_key,
    validate_key_template,
)
from frisket.ops.cluster import DEFAULT_THRESHOLD, compute_semantic_clusters
from frisket.ops.cluster_fingerprint import (
    canonical,
    compute_clusters,
    ngram_fingerprint,
)
from frisket.ops.cluster_values import (
    canonical_column_values,  # noqa: F401 - retained preview export
    cluster_snapshot,
    hash_column_values,
)
from frisket.preview.common import (
    ColumnPreviewError,
    require_visible_column,
    require_visible_sheet,
)
from frisket.semantic import Embedder, resolve_embedder
from frisket.engine.store import Project

CLUSTER_PREVIEW_SCHEMA_VERSION = "frisket.cluster_preview.v1"
CLUSTER_METHODS: tuple[str, ...] = ("fingerprint", "ngram_fingerprint", "semantic")
DEFAULT_MIN_SIZE = 2
DEFAULT_NGRAM_SIZE = 2
MIN_NGRAM_SIZE = 1
MAX_NGRAM_SIZE = 6


class ClusterPreviewError(ColumnPreviewError):
    pass


@dataclass(frozen=True)
class ClusterPreview:
    sheet_id: int
    sheet_name: str
    column_id: int
    column: str
    column_type: str
    method: str
    min_size: int
    row_ids: list[int]
    value_hash: str
    clusters: list[dict[str, Any]]
    count: int
    # additive envelope keys (semantic only; None/omitted for deterministic)
    threshold: float | None = None
    ngram_size: int | None = None
    distinct_values: int | None = None
    considered: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def source_value_hash(
    project: Project, sheet_id: int, column_id: int, row_ids: list[int]
) -> str:
    """Deterministic hash of a column's visible values, the staleness anchor
    shared by preview and commit. Reads the column once and hashes it."""
    return hash_column_values(row_ids, project.get_values(sheet_id, column_id))


def _validate_knobs(
    method: str,
    *,
    threshold: float | None,
    ngram_size: int | None,
) -> None:
    """Reject per-method knobs set for a method that does not use them
    (irrelevant per-method knobs are rejected loudly)."""
    if method != "semantic" and threshold is not None:
        raise ClusterPreviewError(
            "invalid_params",
            f"threshold is only valid for method='semantic', not {method!r}",
            field="threshold",
        )
    if method != "ngram_fingerprint" and ngram_size is not None:
        raise ClusterPreviewError(
            "invalid_params",
            f"ngram_size is only valid for method='ngram_fingerprint', not {method!r}",
            field="ngram_size",
        )


def _key_deriver(key_template: str | None) -> Callable[[str], str] | None:
    """Build the surface->collision-input transform for a cluster-key template,
    or ``None`` when no (non-empty) template is given (cluster on the original
    surface). The template is validated up front so an unknown transform raises
    ``invalid_params`` here, at the same layer as the other preview knobs."""
    if key_template is None or not str(key_template).strip():
        return None
    try:
        validate_key_template(key_template)
    except KeyTemplateError as exc:
        raise ClusterPreviewError(
            "invalid_params", str(exc), field="key_template"
        ) from exc
    return lambda surface: render_value_key(key_template, surface)


def compute_method_clusters(
    project: Project,
    sheet_id: int,
    column: str,
    *,
    method: str,
    min_size: int,
    threshold: float | None = None,
    ngram_size: int | None = None,
    key_template: str | None = None,
    embed: Embedder | None = None,
    embed_id: str | None = None,
    values: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute clusters for ``method`` over ``column``, returning
    (clusters, envelope). The clusters are in the canonical panel shape
    ``{key, canonical, size, values:[{value,count}], row_ids}``. ``envelope``
    carries method-specific additive keys. Shared by preview + commit so the
    two ALWAYS agree.

    ``key_template`` (cluster-by-key) transforms each ORIGINAL surface into the
    text the chosen method keys/embeds on, while display + merge stay on the
    original forms. ``None``/empty clusters on the surface directly.

    ``values`` (row_id -> non-empty stripped surface) may be a pre-read
    column snapshot so the caller reads the column exactly once (the same read
    that produced the value-hash), leaving no split read a concurrent edit
    could tear between the hash and the grouping.

    Semantic errors (``embedding_backend_unavailable``) when no embedder is
    available — never a silent fingerprint fallback.
    """
    derive_key = _key_deriver(key_template)
    if method == "fingerprint":
        clusters = compute_clusters(
            project,
            sheet_id,
            column,
            min_size=min_size,
            values=values,
            derive_key=derive_key,
        )
        return clusters, {"method": "fingerprint", "semantic": False}

    if method == "ngram_fingerprint":
        size = DEFAULT_NGRAM_SIZE if ngram_size is None else ngram_size
        clusters = compute_clusters(
            project,
            sheet_id,
            column,
            min_size=min_size,
            key_fn=lambda value: ngram_fingerprint(value, size),
            values=values,
            derive_key=derive_key,
        )
        return clusters, {
            "method": "ngram_fingerprint",
            "semantic": False,
            "ngram_size": size,
        }

    if method == "semantic":
        thr = DEFAULT_THRESHOLD if threshold is None else threshold
        if embed is None:
            # allow_remote=False, because this is a preview. ``resolve_embedder``
            # states the contract for opting in: "an opting-in caller owns
            # surfacing the spend through its cost gate BEFORE any embed call."
            # A preview has no cost gate — by package contract it writes no run,
            # no receipt and no spend line — so it cannot honour that, and
            # opting in here meant a plain POST to /clusters/v1/preview billed
            # OpenAI once per distinct column value with nothing recorded
            # anywhere. Without a local backend
            # this now refuses with the error the route already documents,
            # rather than spending silently.
            backend = resolve_embedder(allow_remote=False)
            if backend is None:
                raise ClusterPreviewError(
                    "embedding_backend_unavailable",
                    "semantic clustering requires a LOCAL embedding backend; "
                    "none is available. Choose fingerprint, reinstall Frisket, "
                    "or run the cost-gated semantic actions "
                    "for remote embedding.",
                    field="method",
                )
            embed, embed_id = backend
        if embed_id is None:
            embed_id = "injected/unknown"
        clusters, distinct, considered = compute_semantic_clusters(
            project,
            sheet_id,
            column,
            embed,
            embed_id,
            threshold=thr,
            min_size=min_size,
            values=values,
            derive_key=derive_key,
        )
        return clusters, {
            "method": "semantic",
            "semantic": True,
            "threshold": thr,
            "distinct_values": distinct,
            "considered": considered,
            "embedder": embed_id,
        }

    raise ClusterPreviewError(
        "invalid_params",
        f"unknown cluster method {method!r}",
        field="method",
    )


def resolve_cluster_preview(
    project: Project,
    *,
    sheet_id: Any,
    input_column: Any,
    method: Any = "fingerprint",
    min_size: Any = DEFAULT_MIN_SIZE,
    threshold: Any = None,
    ngram_size: Any = None,
    key_template: Any = None,
    embed: Embedder | None = None,
    embed_id: str | None = None,
) -> ClusterPreview:
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id < 1:
        raise ClusterPreviewError(
            "invalid_input_ref",
            "cluster preview requires a positive sheet_id",
            field="sheet_id",
        )
    if not isinstance(input_column, str) or not input_column.strip():
        raise ClusterPreviewError(
            "invalid_input_ref",
            "cluster preview requires a non-empty input_column",
            field="input_column",
        )
    column = input_column.strip()
    if method not in CLUSTER_METHODS:
        raise ClusterPreviewError(
            "invalid_params",
            f"method must be one of {CLUSTER_METHODS}",
            field="method",
        )
    if isinstance(min_size, bool) or not isinstance(min_size, int) or min_size < 2:
        raise ClusterPreviewError(
            "invalid_params",
            "min_size must be an integer >= 2",
            field="min_size",
        )
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ClusterPreviewError(
                "invalid_params", "threshold must be a number", field="threshold"
            )
        if not 0.0 <= float(threshold) <= 1.0:
            raise ClusterPreviewError(
                "invalid_params",
                "threshold must be between 0 and 1",
                field="threshold",
            )
    if ngram_size is not None:
        if isinstance(ngram_size, bool) or not isinstance(ngram_size, int):
            raise ClusterPreviewError(
                "invalid_params",
                "ngram_size must be an integer",
                field="ngram_size",
            )
        if not MIN_NGRAM_SIZE <= ngram_size <= MAX_NGRAM_SIZE:
            raise ClusterPreviewError(
                "invalid_params",
                f"ngram_size must be between {MIN_NGRAM_SIZE} and {MAX_NGRAM_SIZE}",
                field="ngram_size",
            )
    _validate_knobs(method, threshold=threshold, ngram_size=ngram_size)

    normalized_key_template: str | None = None
    if key_template is not None:
        if not isinstance(key_template, str):
            raise ClusterPreviewError(
                "invalid_params",
                "key_template must be a string",
                field="key_template",
            )
        if key_template.strip():
            try:
                validate_key_template(key_template)
            except KeyTemplateError as exc:
                raise ClusterPreviewError(
                    "invalid_params", str(exc), field="key_template"
                ) from exc
            normalized_key_template = key_template

    sheet = require_visible_sheet(
        project, sheet_id, error=ClusterPreviewError, label="cluster preview"
    )
    column_row = require_visible_column(
        project, sheet_id, column, error=ClusterPreviewError, label="cluster preview"
    )
    column_id = int(column_row["id"])
    row_ids = [int(rid) for rid in project.visible_row_ids(sheet_id)]
    # SINGLE read of the column: the value-hash AND the clustering both derive
    # from this one snapshot, so a concurrent edit can never tear the preview
    # into a hash of one state and cards of another.
    raw_values = project.get_values(sheet_id, column_id)
    value_hash = hash_column_values(row_ids, raw_values)
    snapshot = cluster_snapshot(raw_values)

    clusters, envelope = compute_method_clusters(
        project,
        sheet_id,
        column,
        method=method,
        min_size=min_size,
        threshold=threshold,
        ngram_size=ngram_size,
        key_template=normalized_key_template,
        embed=embed,
        embed_id=embed_id,
        values=snapshot,
    )
    return ClusterPreview(
        sheet_id=sheet_id,
        sheet_name=sheet["name"],
        column_id=column_id,
        column=column,
        column_type=column_row["type"],
        method=method,
        min_size=min_size,
        row_ids=row_ids,
        value_hash=value_hash,
        clusters=clusters,
        count=len(clusters),
        threshold=envelope.get("threshold"),
        ngram_size=envelope.get("ngram_size"),
        distinct_values=envelope.get("distinct_values"),
        considered=envelope.get("considered"),
        extra={
            k: v
            for k, v in envelope.items()
            if k
            not in {
                "method",
                "threshold",
                "ngram_size",
                "distinct_values",
                "considered",
            }
        },
    )


def apply_group_edits(
    clusters: list[dict[str, Any]],
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    canonical_overrides: dict[str, str] | None = None,
    excluded_members: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply the review-surface group edits to computed clusters, returning the
    adjusted clusters that the commit will canonicalize + record in the receipt.

    - ``excluded_members[key]`` = surface values the user unchecked in that
      group; they are removed from the cluster (their rows revert to their own
      value, un-merged). A cluster with fewer than ``min_size`` distinct
      surviving surfaces no longer meets the run's grouping floor and is dropped
      (so an exclusion cannot smuggle a below-``min_size`` group past the same
      threshold the initial clustering enforced).
    - ``canonical_overrides[key]`` = the edited canonical for that group,
      replacing the computed most-frequent pick.

    Raises ClusterPreviewError on overrides/exclusions that reference an unknown
    cluster key or an unknown surface value (loud, never silently ignored —
    the same fail-loud rule applied to edits).
    """
    overrides = canonical_overrides or {}
    exclusions = excluded_members or {}
    floor = max(2, min_size)
    by_key = {cluster["key"]: cluster for cluster in clusters}

    unknown_override_keys = sorted(set(overrides) - set(by_key))
    unknown_exclusion_keys = sorted(set(exclusions) - set(by_key))
    if unknown_override_keys or unknown_exclusion_keys:
        raise ClusterPreviewError(
            "invalid_params",
            "canonical_overrides / excluded_members reference unknown cluster keys",
            field="canonical_overrides",
            details={
                "unknown_override_keys": unknown_override_keys,
                "unknown_excluded_keys": unknown_exclusion_keys,
            },
        )

    adjusted: list[dict[str, Any]] = []
    for cluster in clusters:
        key = cluster["key"]
        excluded = set(exclusions.get(key, []))
        surfaces = {value["value"] for value in cluster["values"]}
        unknown_values = sorted(excluded - surfaces)
        if unknown_values:
            raise ClusterPreviewError(
                "invalid_params",
                f"excluded_members[{key!r}] lists values not in the cluster",
                field="excluded_members",
                details={"cluster_key": key, "unknown_values": unknown_values},
            )
        kept_values = [v for v in cluster["values"] if v["value"] not in excluded]
        if len(kept_values) < floor:
            # below the run's grouping floor after exclusions — nothing to merge
            continue
        # row_ids of excluded surfaces are dropped from the group's coverage;
        # we cannot recompute per-surface row_ids from the value refs alone, so
        # the executor recomputes coverage from live source values. Here we keep
        # the cluster's edited canonical + surviving surfaces; size/row_ids are
        # recomputed downstream against the source column.
        # A canonical override is applied by KEY PRESENCE, not truthiness: a
        # supplied-but-blank override is rejected loudly upstream (the contract
        # validator strips + drops empty values), never silently swapped for the
        # computed pick.
        if key in overrides:
            canonical = overrides[key]
        else:
            canonical = _recompute_canonical(kept_values)
        adjusted.append(
            {
                **cluster,
                "canonical": canonical,
                "values": kept_values,
                "excluded": sorted(excluded),
            }
        )
    return adjusted


def _recompute_canonical(values: list[dict[str, Any]]) -> str:
    """Most-frequent -> shortest -> lexicographic over the surviving surfaces
    after exclusions. Delegates to ``cluster_fingerprint.canonical`` so the
    review-surface re-pick uses the SAME heuristic as the initial proposal —
    one place decides what "the canonical form" means."""
    return canonical(Counter({item["value"]: item["count"] for item in values}))


def cluster_preview_payload(preview: ClusterPreview) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CLUSTER_PREVIEW_SCHEMA_VERSION,
        "sheet_id": preview.sheet_id,
        "sheet_name": preview.sheet_name,
        "column_id": preview.column_id,
        "column": preview.column,
        "column_type": preview.column_type,
        "method": preview.method,
        "min_size": preview.min_size,
        "row_count": len(preview.row_ids),
        "value_hash": preview.value_hash,
        "count": preview.count,
        "clusters": preview.clusters,
        "semantic": bool(preview.extra.get("semantic", preview.method == "semantic")),
    }
    if preview.threshold is not None:
        payload["threshold"] = preview.threshold
    if preview.ngram_size is not None:
        payload["ngram_size"] = preview.ngram_size
    if preview.distinct_values is not None:
        payload["distinct_values"] = preview.distinct_values
    if preview.considered is not None:
        payload["considered"] = preview.considered
    for key, value in preview.extra.items():
        payload.setdefault(key, value)
    return payload
