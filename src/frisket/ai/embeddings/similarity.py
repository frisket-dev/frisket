"""Compatibility resolver for named-index similarity on unified retrieval."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from frisket.contracts.query_spec import (
    normalize_manual_text_excludes,
    normalize_manual_text_terms,
)
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.redaction import redact_text
from frisket.retrieval.contracts import (
    BindingRevision,
    RetrievalScope,
    ScopedBinding,
    SearchRequest,
    SearchResponse,
    SemanticExclusion,
    SheetScope,
    SourceAnchor,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.encoding import EncodingPurpose, TextEncodingInput
from frisket.retrieval.query import (
    RetrievalQueryError,
    RetrievalQueryService,
    SemanticQueryVectors,
    source_anchor_vectors,
)
from frisket.retrieval.ranking import RankingError
from frisket.retrieval.store import RetrievalStore
from frisket.project_service_access import remote_service_refusal

from .facade import EmbeddingFacade
from .gateway import (
    EmbeddingBackendUnavailable,
    EmbeddingGateway,
    EmbeddingProviderError,
)
from .source_payload import build_source_payloads
from .spaces import DescriptorEmbeddingSpace
from .store import REMOTE_PROVIDER_KINDS, EmbeddingStore
from .unified_index import (
    dataset_binding_hash,
    dataset_rows,
    descriptor_space,
    plan_named_index_items,
)

EMBEDDING_SIMILARITY_QUERY_KIND = "embedding_similarity"
DEFAULT_SIMILARITY_LIMIT = 50
_EMBEDDABLE_MODALITIES = frozenset({"text", "row"})
_ROW_ANCHOR_KINDS = frozenset({"row", "row_cell", "vector_ref"})


class SimilarityError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = dict(details or {})


@dataclass(frozen=True)
class SimilarityHit:
    row_id: int
    source_key: str
    sheet_id: int | None
    distance: float
    score: float | None


@dataclass(frozen=True)
class QueryEmbeddingUse:
    provider_id: str
    provider_kind: str
    actual_model_id: str


@dataclass(frozen=True)
class EmbeddingQueryBinding:
    index_id: str
    index_metadata: tuple[Any, ...]
    space_metadata: tuple[Any, ...]
    sidecar_hash: str


@dataclass(frozen=True)
class SimilarityResult:
    index_id: str
    space_id: str
    sheet_id: int | None
    distance_metric: str
    anchor: dict[str, Any]
    limit: int
    hits: list[SimilarityHit]
    query_embedding_use: QueryEmbeddingUse | None
    binding: EmbeddingQueryBinding


def _require_semantic_available(response: SearchResponse) -> None:
    # Maintained coverage can be unavailable for an empty named dataset and has
    # no reason. The query service supplies a reason only when it deliberately
    # falls back because the semantic runtime itself cannot run.
    for channel in response.coverage.channels:
        reason = channel.reason
        if channel.channel == "semantic" and channel.state == "unavailable" and reason:
            raise SimilarityError("embedding_backend_unavailable", reason)


_INDEX_BINDING_COLUMNS = (
    "id",
    "space_id",
    "sheet_id",
    "source_query_json",
    "source_columns_json",
    "source_policy_hash",
    "provider_policy_json",
    "status",
    "last_refreshed_at",
    "total_items",
    "ready_items",
    "stale_items",
    "error_items",
)
_SPACE_BINDING_COLUMNS = (
    "id",
    "descriptor_hash",
    "provider_id",
    "provider_kind",
    "actual_model_id",
    "modality",
    "dimension",
    "distance_metric",
)


def _embedding_query_binding(
    index: Any, space: Any, retrieval: RetrievalStore
) -> EmbeddingQueryBinding:
    return EmbeddingQueryBinding(
        index_id=str(index["id"]),
        index_metadata=tuple(index[column] for column in _INDEX_BINDING_COLUMNS),
        space_metadata=tuple(space[column] for column in _SPACE_BINDING_COLUMNS),
        sidecar_hash=dataset_binding_hash(retrieval, str(index["id"])),
    )


def current_embedding_query_binding(
    project: Project | ProjectReadSnapshot, index_id: str
) -> EmbeddingQueryBinding | None:
    metadata = EmbeddingStore(project)
    index = metadata.get_index(index_id)
    if index is None:
        return None
    space = metadata.get_space(index["space_id"])
    if space is None:
        return None
    try:
        with RetrievalStore(project) as retrieval:
            return _embedding_query_binding(index, space, retrieval)
    except Exception:
        return None


def _requested_limit(query: dict[str, Any]) -> int:
    value = query.get("limit", DEFAULT_SIMILARITY_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SimilarityError(
            "invalid_query_spec", "limit must be a positive integer", field="limit"
        )
    return value


def _threshold(query: dict[str, Any]) -> float | None:
    value = query.get("threshold")
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise SimilarityError(
            "invalid_query_spec",
            "threshold must be a finite maximum cosine distance",
            field="threshold",
        )
    return float(value)


def _load_index(
    project: Project | ProjectReadSnapshot, query: dict[str, Any]
) -> tuple[Any, Any, dict[str, Any], DescriptorEmbeddingSpace]:
    index_id = query.get("embedding_index_id")
    if not isinstance(index_id, str) or not index_id.strip():
        raise SimilarityError(
            "invalid_query_spec",
            "embedding_index_id is required",
            field="embedding_index_id",
        )
    metadata = EmbeddingStore(project)
    index = metadata.get_index(index_id)
    if index is None:
        raise SimilarityError(
            "embedding_index_not_found",
            f"no embedding index {index_id!r}",
            field="embedding_index_id",
        )
    space = metadata.get_space(index["space_id"])
    if space is None:
        raise SimilarityError(
            "embedding_index_not_found",
            f"embedding index {index_id!r} references a missing space",
            field="embedding_index_id",
        )
    requested_space = query.get("space_id")
    if requested_space is not None and requested_space != space["id"]:
        raise SimilarityError(
            "embedding_space_mismatch",
            f"query space_id {requested_space!r} is not the index space {space['id']!r}",
            field="space_id",
        )
    try:
        descriptor, facade_space = descriptor_space(space)
    except ValueError as exc:
        raise SimilarityError("embedding_space_mismatch", str(exc)) from exc
    if descriptor["distance_metric"] != "cosine":
        raise SimilarityError(
            "embedding_metric_unsupported",
            "named-index retrieval supports cosine spaces only",
            field="space_id",
            details={"distance_metric": descriptor["distance_metric"]},
        )
    if descriptor["dtype"] != "float32":
        raise SimilarityError(
            "embedding_space_mismatch",
            "named-index retrieval requires float32 vectors",
            field="space_id",
        )
    return index, space, descriptor, facade_space


def prepared_similarity_request(
    project: Any, query: dict[str, Any], *, scope: RetrievalScope
) -> tuple[SearchRequest, SemanticQueryVectors | None]:
    """Bind an existing row or cached query vector; never perform model work."""
    index, space, descriptor, facade_space = _load_index(project, query)
    if index["sheet_id"] is None:
        raise SimilarityError(
            "embedding_index_scope_invalid", "The similarity index has no sheet."
        )
    if scope.sheets is None or any(
        item.sheet_id != int(index["sheet_id"]) for item in scope.sheets
    ):
        raise SimilarityError(
            "embedding_index_sheet_mismatch",
            "The view and similarity index must use the same sheet.",
        )
    anchor = query.get("anchor", {})
    vectors = None
    with RetrievalStore(project) as retrieval:
        if anchor.get("kind") in _ROW_ANCHOR_KINDS:
            bound_query = _row_anchor(
                project, retrieval, index=index, space=space, anchor=anchor
            )
            vectors = source_anchor_vectors(
                project,
                SearchRequest(
                    scope=RetrievalScope(
                        (
                            SheetScope(
                                int(index["sheet_id"]), row_ids=(int(anchor["row_id"]),)
                            ),
                        )
                    ),
                    query=bound_query,
                    mode="semantic",
                    group_by="row",
                    limit=1,
                    named_index_id=str(index["id"]),
                    space_id=str(space["id"]),
                ),
            )
        elif anchor.get("kind") == "manual_text_query":
            bound_query, vectors, _ = _manual_query(
                project,
                retrieval,
                index=index,
                space=space,
                descriptor=descriptor,
                facade_space=facade_space,
                anchor=anchor,
                maximum_distance=_threshold(query),
                gateway=None,
                effect_project=None,
                allow_prepare=False,
            )
        else:
            raise SimilarityError(
                "embedding_source_unsupported", "Unsupported similarity anchor."
            )
    return SearchRequest(
        scope=scope,
        query=bound_query,
        mode="semantic",
        group_by="row",
        limit=9223372036854775807,
        named_index_id=str(index["id"]),
        space_id=str(space["id"]),
    ), vectors


def resolve_embedding_similarity(
    project: Project | ProjectReadSnapshot,
    query: dict[str, Any],
    *,
    gateway: Any = None,
    effect_project: Project | None = None,
) -> SimilarityResult:
    if not isinstance(query, dict):
        raise SimilarityError("invalid_query_spec", "query must be an object")
    if str(query.get("kind") or "").strip().lower() != EMBEDDING_SIMILARITY_QUERY_KIND:
        raise SimilarityError(
            "invalid_query_spec",
            f"query kind must be {EMBEDDING_SIMILARITY_QUERY_KIND!r}",
            field="kind",
        )
    index, space, descriptor, facade_space = _load_index(project, query)
    anchor = query.get("anchor")
    if not isinstance(anchor, dict):
        raise SimilarityError(
            "invalid_query_spec", "anchor must be an object", field="anchor"
        )
    limit = _requested_limit(query)
    maximum_distance = _threshold(query)
    scope = RetrievalScope((SheetScope(sheet_id=int(index["sheet_id"])),))
    anchor_kind = str(anchor.get("kind") or "").strip().lower()
    query_use: QueryEmbeddingUse | None = None
    semantic_vectors: SemanticQueryVectors | None = None

    with RetrievalStore(project) as retrieval:
        if anchor_kind in _ROW_ANCHOR_KINDS:
            request_query = _row_anchor(
                project, retrieval, index=index, space=space, anchor=anchor
            )
        elif anchor_kind == "manual_text_query":
            request_query, semantic_vectors, query_use = _manual_query(
                project,
                retrieval,
                index=index,
                space=space,
                descriptor=descriptor,
                facade_space=facade_space,
                anchor=anchor,
                maximum_distance=maximum_distance,
                gateway=gateway,
                effect_project=effect_project,
            )
        else:
            raise SimilarityError(
                "embedding_source_unsupported",
                f"unsupported anchor kind {anchor_kind!r}",
                field="anchor.kind",
            )
        request = SearchRequest(
            scope=scope,
            query=request_query,
            mode="semantic",
            group_by="row",
            limit=limit,
            named_index_id=str(index["id"]),
            space_id=str(space["id"]),
        )
        try:
            response = RetrievalQueryService(project).search(
                request, semantic_vectors=semantic_vectors
            )
            _require_semantic_available(response)
        except RetrievalQueryError as exc:
            code = (
                "embedding_source_stale"
                if exc.code == "source_anchor_not_found"
                else exc.code
            )
            raise SimilarityError(code, str(exc), field="anchor.row_id") from exc
        except RankingError as exc:
            raise SimilarityError(
                "embedding_composed_query_degenerate",
                "the composed query vector cancels to zero; adjust the terms or weights",
                field="anchor.terms",
            ) from exc
        binding = _embedding_query_binding(index, space, retrieval)

    hits: list[SimilarityHit] = []
    for group in response.results:
        hit = group.primary
        score = next(
            (
                contribution.score
                for contribution in hit.contributions
                if contribution.method == "semantic"
            ),
            None,
        )
        if score is None:
            continue
        distance = max(0.0, min(2.0, 1.0 - float(score)))
        if maximum_distance is not None and distance > maximum_distance:
            continue
        hits.append(
            SimilarityHit(
                row_id=hit.binding.row_id,
                source_key=str(hit.binding.row_id),
                sheet_id=hit.binding.sheet_id,
                distance=round(distance, 6),
                score=round(float(score), 6),
            )
        )
    return SimilarityResult(
        index_id=str(index["id"]),
        space_id=str(space["id"]),
        sheet_id=int(index["sheet_id"]),
        distance_metric="cosine",
        anchor=dict(anchor),
        limit=limit,
        hits=hits,
        query_embedding_use=query_use,
        binding=binding,
    )


def validate_row_cell_anchor(project: Any, index: Any, anchor: dict[str, Any]) -> None:
    """A prepared row vector is a cell vector only for a single-column index."""
    if anchor.get("kind") != "row_cell":
        return
    source_columns = json.loads(index["source_columns_json"])
    selected = anchor.get("column_id")
    columns = project.columns(int(index["sheet_id"]), include_hidden=True)
    source = next(
        (
            col
            for col in columns
            if len(source_columns) == 1
            and col["name"] == source_columns[0]
            and col["active"]
        ),
        None,
    )
    if (
        source is None
        or (selected is not None and selected != source["id"])
        or (anchor.get("column") is not None and anchor["column"] != source["name"])
    ):
        raise SimilarityError(
            "embedding_cell_anchor_unavailable",
            "This index combines columns; use a single-column index for cell similarity.",
        )


def _row_anchor(
    project: Project | ProjectReadSnapshot,
    retrieval: RetrievalStore,
    *,
    index: Any,
    space: Any,
    anchor: dict[str, Any],
) -> SourceAnchor:
    validate_row_cell_anchor(project, index, anchor)
    row_id = anchor.get("row_id")
    if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
        raise SimilarityError(
            "invalid_query_spec",
            "row anchor requires a positive integer row_id",
            field="anchor.row_id",
        )
    source_columns = json.loads(index["source_columns_json"])
    payloads = build_source_payloads(project, index, source_columns, [row_id])
    if len(payloads) != 1:
        raise SimilarityError(
            "embedding_anchor_not_found",
            f"row {row_id} has no embeddable source in this index",
            field="anchor.row_id",
        )
    _recipe, items = plan_named_index_items(
        project, index=index, space=space, payloads=payloads
    )
    item = items[0]
    stored = dataset_rows(retrieval, str(index["id"]), row_ids=(row_id,))
    if row_id not in stored:
        raise SimilarityError(
            "embedding_anchor_not_found",
            f"row {row_id} has no embedding in this index; refresh it first",
            field="anchor.row_id",
        )
    if stored[row_id] != (item.part_id, item.representation_id):
        raise SimilarityError(
            "embedding_source_stale",
            f"row {row_id}'s source changed since it was embedded; refresh the index",
            field="anchor.row_id",
        )
    representation = retrieval.db.execute(
        "SELECT dimension,length(vector) AS vector_bytes FROM representations "
        "WHERE representation_id=?",
        (item.representation_id,),
    ).fetchone()
    expected_dimension = int(space["dimension"])
    if (
        representation is None
        or int(representation["dimension"]) != expected_dimension
        or int(representation["vector_bytes"]) != expected_dimension * 4
    ):
        raise SimilarityError(
            "embedding_space_mismatch",
            "the stored anchor vector does not match the index space",
            field="anchor.row_id",
        )
    dependencies = tuple(
        BindingRevision(
            row_id=dependency.row_id,
            column_id=dependency.column_id,
            retrieval_revision=dependency.retrieval_revision,
        )
        for dependency in item.binding.dependencies
    )
    return SourceAnchor(
        source=item.source,
        locator=item.locator,
        binding=ScopedBinding(
            binding_id=item.binding.binding_id,
            sheet_id=item.binding.sheet_id,
            row_id=item.binding.row_id,
            column_id=None,
            dependencies=dependencies,
        ),
        exclude_exact=True,
    )


def _manual_query(
    project: Project | ProjectReadSnapshot,
    retrieval: RetrievalStore,
    *,
    index: Any,
    space: Any,
    descriptor: dict[str, Any],
    facade_space: DescriptorEmbeddingSpace,
    anchor: dict[str, Any],
    maximum_distance: float | None,
    gateway: Any,
    effect_project: Project | None,
    allow_prepare: bool = True,
) -> tuple[WeightedSemanticQuery, SemanticQueryVectors, QueryEmbeddingUse | None]:
    if descriptor["modality"] not in _EMBEDDABLE_MODALITIES:
        raise SimilarityError(
            "embedding_source_unsupported",
            f"manual text query needs a text space, not {descriptor['modality']!r}",
            field="anchor.kind",
        )
    try:
        terms = normalize_manual_text_terms(anchor)
        excludes = normalize_manual_text_excludes(anchor)
    except ValueError as exc:
        raise SimilarityError(
            "invalid_query_spec", str(exc), field="anchor.terms"
        ) from exc
    provider_kind = str(space["provider_kind"])
    try:
        provider_policy = json.loads(index["provider_policy_json"] or "{}")
    except (TypeError, ValueError):
        provider_policy = {}
    accounting_project = effect_project if effect_project is not None else project

    def admit_fresh(_fresh_inputs):
        if not allow_prepare:
            raise SimilarityError(
                "selection_needs_preparation",
                "Prepare the saved similarity query before using this view or alert.",
            )
        if provider_kind not in REMOTE_PROVIDER_KINDS:
            return
        if gateway is None:
            raise SimilarityError(
                "embedding_remote_preview_unsupported",
                "this preview path does not send query text to remote embedding providers",
                field="anchor.text",
            )
        if not provider_policy.get("allow_remote"):
            raise SimilarityError(
                "embedding_remote_confirmation_required",
                "the index provider policy must allow remote query embedding",
                field="anchor.text",
            )
        assert isinstance(accounting_project, Project)
        refusal = remote_service_refusal(
            accounting_project, f"provider:{provider_kind}"
        )
        if refusal is not None:
            raise SimilarityError(
                refusal.code,
                str(refusal),
                field="anchor.text",
                details={"capability": f"provider:{provider_kind}"},
            )
        from frisket.engine.runner.validation import (
            ProviderKeyRefusal,
            assert_provider_spend_cap,
        )

        try:
            assert_provider_spend_cap(accounting_project, str(space["provider_id"]))
        except ProviderKeyRefusal as exc:
            raise SimilarityError(
                exc.error_code,
                exc.action_message(),
                field="anchor.text",
                details=exc.details,
            ) from exc

    text_values = [term["text"] for term in terms] + [
        exclude["text"] for exclude in excludes
    ]
    encoder_inputs = tuple(TextEncodingInput(text) for text in text_values)

    def encode(fresh_inputs, **_kwargs):
        selected_gateway = gateway or EmbeddingGateway()
        try:
            return selected_gateway.embed(
                [value.text for value in fresh_inputs],
                provider=space["provider_id"],
                model=space["actual_model_id"],
                modality=descriptor["modality"],
            )
        except EmbeddingBackendUnavailable as exc:
            raise SimilarityError("embedding_backend_unavailable", str(exc)) from exc
        except EmbeddingProviderError as exc:
            raise SimilarityError(
                "embedding_provider_error", redact_text(str(exc), max_chars=200)
            ) from None

    fresh_use: QueryEmbeddingUse | None = None

    def account(result, fresh_inputs):
        nonlocal fresh_use
        try:
            fresh_use = QueryEmbeddingUse(
                provider_id=str(result["provider_id"]),
                provider_kind=str(result["provider_kind"]),
                actual_model_id=str(result["actual_model_id"]),
            )
        except (KeyError, TypeError) as exc:
            raise SimilarityError(
                "embedding_provider_error",
                "the embedding provider returned incomplete identity facts",
                field="anchor.text",
            ) from exc
        if provider_kind in REMOTE_PROVIDER_KINDS:
            assert isinstance(accounting_project, Project)
            _persist_manual_text_provider_call(
                accounting_project, result, input_count=len(fresh_inputs)
            )
        vectors = result.get("vectors") if isinstance(result, dict) else None
        if isinstance(vectors, list) and any(
            isinstance(vector, list | tuple) and len(vector) != facade_space.dimensions
            for vector in vectors
        ):
            raise SimilarityError(
                "embedding_space_mismatch",
                "the embedding provider returned a vector with the wrong dimension",
                field="anchor.text",
            )

    try:
        vectors = EmbeddingFacade(retrieval).encode(
            encoder_inputs,
            encoder=encode,
            purpose=EncodingPurpose.QUERY,
            space=facade_space,
            space_id=str(space["id"]),
            cache_payloads=text_values,
            before_fresh_batch=admit_fresh,
            on_fresh_batch=account,
        )
    except SimilarityError:
        raise
    except Exception as exc:
        raise SimilarityError(
            "embedding_provider_error", redact_text(str(exc), max_chars=200)
        ) from None
    term_count = len(terms)
    request_query = WeightedSemanticQuery(
        terms=tuple(WeightedTerm(term["text"], term["weight"]) for term in terms),
        exclusions=tuple(
            SemanticExclusion(exclude["text"], exclude["threshold"])
            for exclude in excludes
        ),
        minimum_similarity=(
            None
            if maximum_distance is None or not 0.0 <= maximum_distance <= 2.0
            else 1.0 - maximum_distance
        ),
    )
    return (
        request_query,
        SemanticQueryVectors(
            term_vectors=tuple(vectors[:term_count]),
            exclusion_vectors=tuple(vectors[term_count:]),
        ),
        fresh_use,
    )


def _persist_manual_text_provider_call(
    project: Project, result: dict[str, Any], *, input_count: int
) -> None:
    from frisket.ai.models.metadata import ModelCallMeta
    from frisket.engine.store.runs import RunResultStore

    units = dict(result.get("usage") or {})
    units.setdefault("input_count", input_count)
    fact = ModelCallMeta.provider_call(
        capability="llm.embed",
        engine=f"{result['provider_id']}/{result['actual_model_id']}",
        provider=result["provider_id"],
        provider_kind=result["provider_kind"],
        model_ids=[result["actual_model_id"]],
        credential_source=result.get("credential_source", "none"),
        provider_reported_cost_usd=result.get("provider_reported_cost_usd"),
        provider_cost_usd=result.get("provider_cost_usd"),
        units=units,
        cost_source=result.get("cost_source", "unknown"),
        request_id=result.get("provider_request_id"),
        duration_ms=None,
    ).as_dict()
    try:
        RunResultStore(project).write_unscoped_model_calls(
            [fact], row_id=None, column_id=None
        )
    except Exception as exc:
        raise SimilarityError(
            "embedding_accounting_failed",
            "the embedding provider returned a result, but its usage facts could not be recorded",
            field="anchor.text",
        ) from exc
