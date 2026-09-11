"""One host for typed table values, admitted contributors and explicit parents."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from itertools import chain
import hashlib
import inspect
import json
import logging
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import SchemaValidator

from frisket.actions.core import CreateSheet, OutputField, _publication_return_schema
from frisket.actions.url_import_types import UrlImporter
from frisket.actions.entity_types import ClusterReceiptReader
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionRequest,
    DynamicOutput,
    DynamicTableResult,
    EmbeddingIndexReader,
    EmailSourceReader,
    RuntimeImporter,
    ListTableReader,
    SheetRowsReader,
    CollectionReader,
    LocalFileReader,
    ImportBlobStager,
    PdfPageRenderer,
    PluginSecrets,
    StagedFile,
    TableResult,
    TableRow,
    RowSource,
    TableError,
    _without_none,
    _table_warnings,
)
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    ImportRowsParams,
    ImportRowsSource,
    Receipt,
    ReceiptIO,
    ReceiptEvidence,
)
from frisket.contracts.actions.schemas.imports import validate_import_value
from frisket.contracts.actions.schemas._base import canonical_column_type
from frisket.engine.executor.action_families.imports import (
    _perform_import_rows_in_txn,
    _preflight_table_rows,
    _resolve_import_rows_project_types,
)
from frisket.engine.executor.action_inventory import (
    ExecutorDeps,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_in_txn_idempotent_perform,
)
from frisket.engine.executor.action_receipts import (
    _receipt_ref,
    _result_from_receipt,
    _positive_ref_int,
)
from frisket.engine.executor.action_families._errors import receipt_stale_replay_error
from frisket.engine.executor.action_support import _failed_result, _new_id
from frisket.engine.executor.action_jobs import ActionJobTerminalizationError
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.executor.action_reservations import _receipt_for_idempotency
from frisket.engine.executor.map_rows_action import (
    normalized_typed_request_identity,
    typed_request_hash,
)
from frisket.engine.executor.embedding_read import (
    AdmittedEmbeddingIndexReader,
    TableReadRefused,
)
from frisket.engine.executor.import_sources import open_local_file_reader
from frisket.engine.executor.collection_read import (
    AdmittedCollectionReader,
    validate_collection_source,
)
from frisket.actions.semantic_match_types import SemanticMatchReader
from frisket.actions.join_types import JoinedTablesReader
from frisket.engine.executor.joined_tables_read import (
    AdmittedJoinedTablesReader,
    JoinRefreshAdmission,
    validate_joined_tables_source,
)
from frisket.actions.transcript_types import (
    TranscriptReader,
    TranscriptProjectionValue,
    TranscriptAnnotationValue,
)
from frisket.engine.executor.transcript_read import AdmittedTranscriptReader
from frisket.actions.temporal_types import TemporalMediaReader, TemporalMediaValue
from frisket.engine.executor.temporal_media_read import AdmittedTemporalMediaReader
from frisket.engine.executor.semantic_match_read import (
    AdmittedSemanticMatchReader,
    validate_semantic_match_source,
)
from frisket.engine.executor.email_source_read import AdmittedEmailSourceReader
from frisket.engine.executor.cluster_receipt_read import (
    AdmittedClusterReceiptReader,
    validate_cluster_receipt_fence,
)
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.table_producer import TableProducer
from frisket.engine.store.import_blobs import publish_import_blobs
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobStoreError
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
    ContributorMaterializedRow,
    ContributorTablePlan,
    write_contributor_table,
    load_materialized_row_sources_for_op,
    materialized_row_sources_ref_matches,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import (
    StreamingSheetWriter,
    DuplicateStreamingPublication,
)

logger = logging.getLogger("frisket.executor")


def _duplicate_sheet_error(
    project: Project, action_kind: str, name: str
) -> ActionError | None:
    if (
        project.db.execute("SELECT 1 FROM sheets WHERE name=?", (name,)).fetchone()
        is None
    ):
        return None
    return ActionError(
        code="duplicate_sheet_name",
        message=f"A sheet named {name!r} already exists",
        action_kind=action_kind,
        field="sheet_name",
    )


def _table_replay_error(project: Project, receipt: Receipt) -> ActionError | None:
    def stale() -> ActionError:
        return ActionError(
            code="stale_replay",
            message="The published table or its lineage has changed",
            action_kind=receipt.action_kind,
        )

    try:
        for entry in receipt.inputs:
            if entry.ref.get("kind") == "cluster_receipt_source":
                validate_cluster_receipt_fence(project, entry.ref)
            elif entry.ref.get("kind") == "collection_expand_source_fingerprint":
                validate_collection_source(project, entry.ref)
            elif entry.ref.get("kind") == "semantic_join_link_source":
                validate_semantic_match_source(project, entry.ref)
            elif entry.ref.get("kind") == "joined_tables_source":
                validate_joined_tables_source(project, entry.ref)
    except TableError:
        return stale()

    sheet_ref = _receipt_ref(receipt, "materialized_sheet")
    if sheet_ref is None:
        return stale()
    sheet_id = _positive_ref_int(sheet_ref, "sheet_id")
    op_id = _positive_ref_int(sheet_ref, "op_id")
    if sheet_id is None or op_id is None:
        return stale()
    sheet = project.db.execute(
        "SELECT parent_sheet_id, parent_op_id FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    op = project.db.execute(
        "SELECT id FROM ops WHERE id=? AND status='applied'", (op_id,)
    ).fetchone()
    if sheet is None or op is None or op_id not in receipt.op_ids:
        return stale()
    if sheet["parent_sheet_id"] != sheet_ref.get("parent_sheet_id"):
        return stale()
    for evidence in receipt.evidence:
        ref = evidence.ref
        if ref.get("kind") not in {
            "imported_blob",
            "imported_pdf_document",
            "imported_pdf_page_image",
        }:
            continue
        digest = ref.get("hash")
        if not isinstance(digest, str):
            return stale()
        metadata = project.db.execute(
            "SELECT size FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        if metadata is None or metadata["size"] != ref.get("size"):
            return stale()
        try:
            with project.blob_store.materialize(digest):
                pass  # The blob-store contract verifies content before yielding.
        except (BlobStoreError, OSError, ValueError):
            return stale()
    if sheet["parent_sheet_id"] is not None:
        return _parented_table_replay_error(project, receipt)
    if (
        _receipt_ref(receipt, "materialized_rows") is not None
        or _receipt_ref(receipt, "lineage_parent_rows") is not None
    ):
        return stale()
    return None


_TABLE_BATCH_SIZE = 500
_IMPORT_ACTIONS = {
    "import.rows",
    "import.ndjson",
    "import.csv",
    "import.xlsx",
    "import.geojson",
    "import.kml",
    "import.files",
    "import.pdf",
    "import.email",
    "import.runtime",
    "import.urls",
}


def _table_columns(
    project: Project,
    fields,
    *,
    action_kind,
    sheet_name,
    output_names,
    check_sheet_name=True,
) -> ImportRowsParams:
    if check_sheet_name:
        duplicate = _duplicate_sheet_error(project, action_kind, sheet_name)
        if duplicate is not None:
            raise TableReadRefused(duplicate)
    ActionRequest._valid_output_names(dict(output_names))
    keys = {field.key for field in fields}
    if output_names.keys() - keys:
        raise ValueError("unknown output names")
    names = [output_names.get(field.key, field.key) for field in fields]
    if len(names) != len(set(names)):
        raise ValueError("final output names must be unique")
    table = ImportRowsParams.model_validate(
        {
            "sheet_name": sheet_name,
            "mode": "create_sheet",
            "columns": [
                {
                    "name": output_names.get(field.key, field.key),
                    "type": canonical_column_type(field.column_type),
                    "format": field.format,
                    "hidden": field.hidden,
                }
                for field in fields
            ],
            "rows": [],
        }
    )
    error = _resolve_import_rows_project_types(project, table, action_kind=action_kind)
    if error is not None:
        raise TableReadRefused(error.model_copy(update={"field": "params"}))
    return table


def _contains_staged_file(value):
    if isinstance(value, StagedFile):
        return True
    if isinstance(value, BaseModel):
        return any(
            _contains_staged_file(getattr(value, key))
            for key in type(value).model_fields
        )
    if isinstance(value, dict):
        return any(_contains_staged_file(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_staged_file(item) for item in value)
    return False


@dataclass(frozen=True)
class _TableLineage:
    sources: tuple[RowSource, ...]
    parent: RowSource | None


def _admitted_parent_sheet(readers):
    parents = {
        reader.parent_sheet_id
        for reader in readers.values()
        if getattr(reader, "parent_sheet_id", None) is not None
    }
    parents.update(
        source.sheet_id
        for reader in readers.values()
        for source in (getattr(reader, "sources", ()) or ())
        if isinstance(source, RowSource)
        and not (
            isinstance(
                reader, (AdmittedSemanticMatchReader, AdmittedJoinedTablesReader)
            )
            and reader.source_roles.get(source) in {"edge_target", "join_right"}
        )
    )
    if len(parents) > 1:
        raise ValueError("table readers must agree on their admitted parent sheet")
    return next(iter(parents), None)


def _validated_table_rows(bound, fields, iterator, readers, *, max_rows):
    terminal = bound.action.definition.run
    logical_names = tuple(field.key for field in fields)
    final_names = {
        name: bound.request.output_names.get(name, name) for name in logical_names
    }
    validator = SchemaValidator(
        _publication_return_schema(terminal.output_model.__pydantic_core_schema__),
        _use_prebuilt=False,
    )
    for count, raw in enumerate(iterator, 1):
        if max_rows is not None and count > max_rows:
            raise TableError(
                "import_workload_limit_exceeded",
                f"{bound.action.action_id} row count exceeds the deployment limit of {max_rows}",
                details={"row_count": count, "max_rows": max_rows},
            )
        if not isinstance(raw, TableRow):
            raise TypeError("table rows must be TableRow(output=..., sources=...)")
        if SheetRowsReader in readers and not raw.sources:
            raise ValueError("sheet-row tables must retain an admitted source row")
        if (
            not isinstance(raw.sources, tuple)
            or len({id(source) for source in raw.sources}) != len(raw.sources)
            or any(
                not isinstance(source, RowSource)
                or not any(
                    source in (getattr(reader, "sources", ()) or ())
                    for reader in readers.values()
                )
                for source in raw.sources
            )
        ):
            raise ValueError("table sources must be unique admitted tokens")
        if raw.parent is not None and not any(
            raw.parent is source for source in raw.sources
        ):
            raise ValueError("table parent must be one of its admitted source tokens")
        output = validator.validate_python(raw.output, strict=True, by_name=True)
        bindings = terminal.output_bindings()
        values = (
            output.root
            if isinstance(output, DynamicOutput)
            else {key: getattr(output, name) for key, name in bindings.items()}
        )
        if set(values) != set(logical_names):
            raise ValueError("row_shape_mismatch")
        for capability in (TranscriptReader, TemporalMediaReader):
            if capability in readers:
                values = readers[capability].lower_row(
                    values, fields, raw.sources, raw.parent, bound.request.output_names
                )
        if any(
            isinstance(
                value,
                (
                    TranscriptProjectionValue,
                    TranscriptAnnotationValue,
                    TemporalMediaValue,
                ),
            )
            for value in values.values()
        ):
            raise ValueError("temporal projections require their admitted reader")
        files = {}
        lowered = {}
        for field in fields:
            value = values[field.key]
            annotation = field.annotation
            while get_origin(annotation) is Annotated:
                annotation = get_args(annotation)[0]
            annotation = _without_none(annotation)
            file_list = (
                len(annotation) == 1
                and get_origin(annotation[0]) is list
                and get_args(annotation[0]) == (StagedFile,)
            )
            if isinstance(value, StagedFile):
                if field.column_type not in {"file", "image", "audio", "video"}:
                    raise ValueError("staged files require a media output column")
                if ImportBlobStager not in readers:
                    raise ValueError("staged file was not admitted by this invocation")
                files[field.key] = (value,)
                lowered[field.key] = readers[ImportBlobStager].lower(value)
            elif file_list and value is not None:
                if value and ImportBlobStager not in readers:
                    raise ValueError(
                        "staged files were not admitted by this invocation"
                    )
                files[field.key] = tuple(value)
                lowered[field.key] = [
                    readers[ImportBlobStager].lower(handle) for handle in value
                ]
            elif _contains_staged_file(value):
                raise ValueError(
                    "staged files require media or typed file-list outputs"
                )
            elif (
                ImportBlobStager in readers
                and field.column_type in {"file", "image", "audio", "video"}
                and value is not None
            ):
                raise ValueError(
                    "staged media outputs must carry their admitted file handle"
                )
        row = (
            dict(values)
            if isinstance(output, DynamicOutput)
            else output.model_dump(
                mode="json", by_alias=True, exclude={bindings[key] for key in files}
            )
        )
        row.update(lowered)
        for field in fields:
            temporal = readers.get(TemporalMediaReader)
            if temporal is not None and temporal.owns_preview_value(
                row[field.key], field.column_type
            ):
                continue
            validate_import_value(field.column_type, row[field.key])
        yield (
            {final_names[name]: row[name] for name in logical_names},
            _TableLineage(raw.sources, raw.parent),
            tuple(
                (final_names[key], handle)
                for key, handles in files.items()
                for handle in handles
            ),
        )


@dataclass
class _PreparedTable:
    table: ImportRowsParams
    rows: Any
    produced: Any
    readers: dict[type, Any]
    parent_sheet_id: int | None
    output_fields: tuple[OutputField, ...] = ()
    buffered: bool = False
    row_count: int = 0
    warnings: tuple[str, ...] = ()
    source: dict[str, Any] | None = None
    reads: tuple[dict[str, Any], ...] = ()
    identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class TableSource:
    """Host identity and one invocation-owned prepared table, not author code."""

    envelope: _TypedProjectEnvelope
    params_hash: str
    capabilities: tuple[type, ...]
    prepare: Callable


def builtin_table_source(project, project_id, bound, *, deps=None) -> TableSource:
    terminal = bound.action.definition.run
    if not isinstance(terminal, CreateSheet):
        raise TypeError("table source requires CreateSheet")

    @contextmanager
    def prepare(**controls):
        controls.setdefault("cancelled", (deps or ExecutorDeps()).cancelled)
        with prepare_table_producer(
            project, bound, project_id=project_id, deps=deps, **controls
        ) as prepared:
            prepared.buffered = isinstance(prepared.produced.rows, Sequence)
            yield prepared
        # Readers and iterators have closed before their final facts are frozen.
        prepared.warnings = _table_warnings(prepared.produced)
        prepared.source, reads, prepared.identity = _table_read_metadata(
            bound, prepared.produced, prepared.readers, prepared.row_count
        )
        prepared.reads = tuple(reads)

    return TableSource(
        _TypedProjectEnvelope(
            kind=bound.action.action_id,
            idempotency_key=bound.request.idempotency_key,
            params=bound.params.model_dump(mode="json"),
        ),
        typed_request_hash(bound),
        terminal.capabilities,
        prepare,
    )


@contextmanager
def prepare_table_producer(
    project: Project,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
    check_sheet_name: bool = True,
    project_id: str | None = None,
    blob_stager: AdmittedImportBlobStager | None = None,
    publication_resources: ExitStack | None = None,
    join_refresh: JoinRefreshAdmission | None = None,
    schema_only: bool = False,
    row_limit: int | None = None,
    cancelled: Callable[[], bool] | None = None,
):
    """Prepare one table; schema discovery never opens or consumes its row iterator.

    ``row_limit`` bounds admitted PDF/temporal rendering and runtime importer
    collection; PDF also respects the deployment limit. Callers own row
    consumption and overflow refusal. These host controls never become authored
    parameters.
    """

    from frisket.engine.executor.list_table_read import AdmittedListTableReader
    from frisket.engine.executor.sheet_rows_read import AdmittedSheetRowsReader

    terminal = bound.action.definition.run
    if not isinstance(terminal, CreateSheet):
        raise TypeError("table preparation requires CreateSheet")
    deps = deps or ExecutorDeps()
    limits = (
        deps.import_workload_limits
        if bound.action.action_id in _IMPORT_ACTIONS
        else None
    )
    fields = bound.output_fields
    if check_sheet_name and fields is None:
        duplicate = _duplicate_sheet_error(
            project, bound.action.action_id, bound.request.sheet_name
        )
        if duplicate is not None:
            raise TableReadRefused(duplicate)
    table = (
        _table_columns(
            project,
            fields,
            action_kind=bound.action.action_id,
            sheet_name=bound.request.sheet_name,
            output_names=bound.request.output_names,
            check_sheet_name=check_sheet_name,
        )
        if fields is not None
        else None
    )
    with ExitStack() as resources:
        readers = {}
        for capability in terminal.capabilities:
            if capability is EmbeddingIndexReader:
                reader = AdmittedEmbeddingIndexReader(project, bound.action.action_id)
            elif capability is LocalFileReader:
                reader = resources.enter_context(
                    open_local_file_reader(deps.local_file_sources)
                )
            elif capability is EmailSourceReader:
                reader = AdmittedEmailSourceReader(deps.email_sources)
                resources.callback(reader.close)
            elif capability is RuntimeImporter:
                from frisket.engine.executor.runtime_import_read import (
                    AdmittedRuntimeImporter,
                )

                if project_id is None:
                    raise ValueError(
                        "runtime importer requires invocation project identity"
                    )
                reader = AdmittedRuntimeImporter(
                    project,
                    project_id=project_id,
                    sheet_name=bound.request.sheet_name,
                    action_id=bound.action.action_id,
                    row_limit=row_limit,
                    cancelled=cancelled,
                )
                resources.callback(reader.close)
            elif capability is UrlImporter and blob_stager is not None:
                from frisket.engine.executor.url_import_read import AdmittedUrlImporter
                from frisket.authoring.workbench.plugin_runtime_capabilities import (
                    enabled_workbench_plugin_ids,
                )

                reader = AdmittedUrlImporter(
                    blob_stager,
                    max_urls=deps.url_import_limits.max_urls
                    if deps.url_import_limits
                    else None,
                    enabled_plugin_ids=enabled_workbench_plugin_ids(project),
                )
                resources.callback(reader.close)
            elif capability is ListTableReader:
                reader = AdmittedListTableReader(project, bound.action.action_id)
            elif capability is SheetRowsReader:
                reader = AdmittedSheetRowsReader(
                    project, scope=bound.request.scope, params=bound.params
                )
                resources.callback(reader.close)
            elif capability is CollectionReader:
                reader = AdmittedCollectionReader(
                    project,
                    action_kind=bound.action.action_id,
                    request_identity=normalized_typed_request_identity(bound),
                    confirmation=bound.request.confirmation,
                )
            elif capability is SemanticMatchReader:
                reader = AdmittedSemanticMatchReader(
                    project,
                    scope=bound.request.scope,
                    action_kind=bound.action.action_id,
                )
            elif capability is JoinedTablesReader:
                reader = AdmittedJoinedTablesReader(
                    project,
                    scope=bound.request.scope,
                    action_kind=bound.action.action_id,
                    request_identity=normalized_typed_request_identity(bound),
                    confirmation=bound.request.confirmation,
                    refresh_admission=join_refresh,
                )
            elif capability is TranscriptReader:
                reader = AdmittedTranscriptReader(
                    project,
                    scope=bound.request.scope,
                    action_kind=bound.action.action_id,
                )
                resources.callback(reader.close)
            elif capability is TemporalMediaReader:
                reader = AdmittedTemporalMediaReader(
                    project,
                    scope=bound.request.scope,
                    action_kind=bound.action.action_id,
                    preview_stager=blob_stager
                    if publication_resources is None
                    else None,
                    row_limit=row_limit,
                    cancelled=cancelled,
                )
                resources.callback(reader.close)
                (
                    publication_resources
                    if publication_resources is not None
                    else resources
                ).callback(reader.cleanup)
            elif capability is ClusterReceiptReader:
                reader = AdmittedClusterReceiptReader(project)
            elif capability is ImportBlobStager and blob_stager is not None:
                reader = blob_stager
            elif capability is PdfPageRenderer and blob_stager is not None:
                from frisket.engine.executor.pdf_page_read import (
                    AdmittedPdfPageRenderer,
                )

                reader = AdmittedPdfPageRenderer(
                    blob_stager,
                    page_limit=min(
                        (
                            limit
                            for limit in (
                                row_limit,
                                limits.max_rows if limits else None,
                            )
                            if limit is not None
                        ),
                        default=None,
                    ),
                    cancelled=cancelled,
                )
                resources.callback(reader.close)
            else:
                raise TypeError("table reader capability has no host implementation")
            readers[capability] = reader
        if UrlImporter in readers or (
            TemporalMediaReader in readers and blob_stager is not None
        ):
            # Host-owned acquisitions use the same invocation-owned file manifest.
            readers[ImportBlobStager] = blob_stager
        producer = resources.enter_context(TableProducer(cancelled=cancelled))
        producer.check_cancelled()
        plugin_secrets = None
        if PluginSecrets in terminal.injections:
            from frisket.authoring.workbench.native_plugin_secrets import (
                HostPluginSecrets,
            )

            plugin_secrets = HostPluginSecrets.from_binding(
                project, bound.runtime_binding
            )
        pending = terminal.handler(
            bound.params,
            *(
                plugin_secrets if injection is PluginSecrets else readers[injection]
                for injection in terminal.injections
            ),
        )
        produced = producer.resolve(pending)
        producer.own_rows(getattr(produced, "rows", None))
        if inspect.isawaitable(pending):
            producer.check_cancelled()
        if fields is None:
            if not isinstance(produced, DynamicTableResult):
                raise TypeError("runtime table producer must return DynamicTableResult")
            fields = terminal.fields_from_columns(produced.schema)
            table = _table_columns(
                project,
                fields,
                action_kind=bound.action.action_id,
                sheet_name=bound.request.sheet_name,
                output_names=bound.request.output_names,
                check_sheet_name=check_sheet_name,
            )
        elif not isinstance(produced, TableResult) or isinstance(
            produced, DynamicTableResult
        ):
            raise TypeError("known-schema table producer must return TableResult")
        if schema_only:
            assert table is not None
            yield _PreparedTable(
                table,
                (),
                produced,
                readers,
                _admitted_parent_sheet(readers),
                output_fields=fields,
            )
            return
        original = producer.rows(produced.rows)
        validated = iter(
            _validated_table_rows(
                bound,
                fields,
                original,
                readers,
                max_rows=limits.max_rows if limits else None,
            )
        )
        first = next(validated, None)
        parent = _admitted_parent_sheet(readers)

        def rows():
            for item in chain((first,), validated) if first is not None else ():
                if SemanticMatchReader in readers:
                    readers[SemanticMatchReader].validate_lineage(
                        item[1].sources, item[1].parent
                    )
                elif JoinedTablesReader in readers:
                    readers[JoinedTablesReader].validate_lineage(
                        item[1].sources, item[1].parent
                    )
                elif any(source.sheet_id != parent for source in item[1].sources):
                    raise ValueError(
                        "table sources must match the admitted parent sheet"
                    )
                yield item

        assert table is not None
        yield _PreparedTable(
            table,
            rows(),
            produced,
            readers,
            parent,
            output_fields=fields,
        )
        # A lazy producer can admit another source even after its last emitted row.
        if _admitted_parent_sheet(readers) != parent:
            raise ValueError("table readers changed their admitted parent sheet")


def _table_read_metadata(bound, produced, readers, row_count):
    source = dict(produced.source) if produced.source is not None else None
    if (
        ListTableReader in readers
        or SheetRowsReader in readers
        or CollectionReader in readers
        or SemanticMatchReader in readers
        or JoinedTablesReader in readers
        or TranscriptReader in readers
        or TemporalMediaReader in readers
        or ClusterReceiptReader in readers
        or RuntimeImporter in readers
        or UrlImporter in readers
    ):
        # Admitted source facts, not producer-authored metadata, own this identity.
        source = None
    runtime_reader = readers.get(RuntimeImporter)
    if runtime_reader is not None:
        source = runtime_reader.source_summary
    file_reader = readers.get(LocalFileReader)
    if file_reader is not None:
        single = file_reader.facts[0] if len(file_reader.facts) == 1 else {}
        source = (
            {
                "kind": "file",
                "label": source.get("label") if source else None,
                "importer": source.get("importer") if source else None,
                "skipped_features": source.get("skipped_features", [])
                if source
                else [],
                "path": single.get("path"),
                "fingerprint": single.get("sha256"),
                "line_count": row_count,
                "request_hash": typed_request_hash(bound),
            }
            if file_reader.facts
            else None
        )
    reads = [
        fact
        for capability, reader in readers.items()
        if capability is not ImportBlobStager
        for fact in reader.facts
    ]
    identity = normalized_typed_request_identity(bound)
    if bound.action.action_id == "import.rows":
        identity["params"].pop("rows", None)
    elif bound.action.action_id == "import.email":
        identity["params"].pop("sources", None)
    if runtime_reader is not None:
        # Runtime importer arguments may contain plugin secrets. Actual-call
        # hashes live in read facts; the request hash binds the full saved intent.
        identity.pop("params", None)
    if bound.action.action_id in _IMPORT_ACTIONS:
        identity["import_row_count"] = row_count
    identity.update(
        params_hash=typed_request_hash(bound),
        reads=reads,
        idempotency_key=bound.request.idempotency_key,
    )
    return source, reads, identity


def run_typed_create_sheet_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
    reserved_action_id: str | None = None,
    reserved_receipt_id: str | None = None,
) -> ActionResult:
    return run_table_source(
        project,
        project_id,
        builtin_table_source(project, project_id, bound, deps=deps),
        reserved_action_id=reserved_action_id,
        reserved_receipt_id=reserved_receipt_id,
    )


def run_table_source(
    project: Project,
    project_id: str,
    source: TableSource,
    *,
    reserved_action_id: str | None = None,
    reserved_receipt_id: str | None = None,
) -> ActionResult:
    envelope = source.envelope
    params_hash = source.params_hash
    replay = _child_sheet_deterministic_result_from_existing(_table_replay_error)

    def replay_existing(existing):
        if any(
            c in source.capabilities for c in (TranscriptReader, TemporalMediaReader)
        ):
            from frisket.engine.executor.action_reservations import (
                _reserved_receipt_result_from_existing,
            )

            result = _reserved_receipt_result_from_existing(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=envelope,
            )
            if result.status == "completed":
                error = _table_replay_error(project, existing.parsed())
                if error is not None:
                    return _failed_result(
                        project_id=project_id, action_kind=envelope.kind, error=error
                    )
            return result
        return replay(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=envelope,
        )

    if bool(reserved_action_id) != bool(reserved_receipt_id):
        raise ValueError("reservation IDs must be paired")
    if reserved_receipt_id is not None:
        existing = ReceiptStore(project).find_by_id(reserved_receipt_id)
        if existing is None or (
            existing.action_id != reserved_action_id
            or existing.params_hash != params_hash
            or existing.parsed().action_kind != envelope.kind
            or existing.parsed().idempotency_key != envelope.idempotency_key
            or existing.parsed().project_id != project_id
        ):
            raise ValueError("table reservation does not match its canonical request")
        if existing.status != "running":
            return replay_existing(existing)
    else:
        existing = _receipt_for_idempotency(project, envelope.idempotency_key)
        if existing is not None:
            return replay_existing(existing)
    writer = None
    stager = None
    publication_resources = ExitStack()
    try:
        if (
            ImportBlobStager in source.capabilities
            or UrlImporter in source.capabilities
        ):
            stager = AdmittedImportBlobStager()
        occurrences = []
        with source.prepare(
            blob_stager=stager,
            publication_resources=publication_resources,
        ) as prepared:
            table = prepared.table
            parent = prepared.parent_sheet_id
            if prepared.buffered or parent is not None:
                buffered, sources = [], []
                for ordinal, (row, lineage, files) in enumerate(prepared.rows):
                    prepared.row_count += 1
                    buffered.append(row)
                    sources.append(lineage)
                    occurrences.extend(
                        (ordinal, name, handle) for name, handle in files
                    )
            else:
                writer = StreamingSheetWriter.start(
                    project,
                    sheet_name=table.sheet_name,
                    columns=[
                        column.model_dump(mode="json") for column in table.columns
                    ],
                    project_id=project_id,
                    action_kind=envelope.kind,
                    idempotency_key=envelope.idempotency_key,
                    params_hash=params_hash,
                    action_id=_new_id("act"),
                    receipt_id=_new_id("receipt"),
                    source_ref={},
                )
                batch = []
                batch_files = []
                for row, lineage, files in prepared.rows:
                    prepared.row_count += 1
                    if lineage.sources or lineage.parent is not None:
                        raise ValueError(
                            "table rows cannot mix source-free and parented rows"
                        )
                    batch.append(row)
                    batch_files.append(files)
                    if len(batch) == _TABLE_BATCH_SIZE:
                        occurrences.extend(
                            _append_table_batch(
                                project,
                                writer,
                                table,
                                batch,
                                batch_files,
                                envelope.kind,
                            )
                        )
                        batch = []
                        batch_files = []
                if batch:
                    occurrences.extend(
                        _append_table_batch(
                            project, writer, table, batch, batch_files, envelope.kind
                        )
                    )
        # Every source/iterator has closed and read verification completed.
        warnings = prepared.warnings
        source_ref, reads, identity = (
            prepared.source,
            list(prepared.reads),
            prepared.identity,
        )
        if stager is not None:
            # Publication removes invocation-owned scratch files. Windows,
            # unlike POSIX, refuses that removal while readers remain open.
            stager.finish_reads()
        blob_plan = stager.publication_plan(occurrences) if stager is not None else None
        if writer is None:
            list_reader = prepared.readers.get(ListTableReader)
            resolved = {
                "materialization": table.model_copy(
                    update={
                        "rows": buffered,
                        "source": ImportRowsSource.model_validate(source_ref)
                        if source_ref
                        else None,
                    }
                ),
                "parent_sheet_id": parent,
                "sources": sources,
                "reads": reads,
                "op_spec": identity,
                "list_reader": list_reader,
                "sheet_rows_reader": prepared.readers.get(SheetRowsReader),
                "collection_reader": prepared.readers.get(CollectionReader),
                "semantic_match_reader": prepared.readers.get(SemanticMatchReader),
                "joined_tables_reader": prepared.readers.get(JoinedTablesReader),
                "projection_readers": [
                    prepared.readers[c]
                    for c in (TranscriptReader, TemporalMediaReader)
                    if c in prepared.readers
                ],
                "source_roles": {
                    source: role
                    for reader in prepared.readers.values()
                    for source, role in getattr(reader, "source_roles", {}).items()
                },
                "blob_plan": blob_plan,
                "warnings": warnings,
            }
            if any(
                c in source.capabilities
                for c in (TranscriptReader, TemporalMediaReader)
            ):
                return _run_reserved_table(
                    project,
                    envelope,
                    resolved,
                    project_id=project_id,
                    params_hash=params_hash,
                    reserved_action_id=reserved_action_id,
                    reserved_receipt_id=reserved_receipt_id,
                )
            return _run_in_txn_idempotent_perform(
                project,
                envelope,
                project_id=project_id,
                params_hash=params_hash,
                result_from_existing_fn=lambda _project, existing, **_kwargs: (
                    replay_existing(existing)
                ),
                perform_fn=lambda cur, **ids: _perform_table_in_txn(
                    project,
                    cur,
                    envelope,
                    project_id=project_id,
                    params_hash=params_hash,
                    resolved=resolved,
                    **ids,
                ),
            )
        writer.set_warnings(warnings)
        publication = writer.publish(
            request_spec=identity,
            reads=reads,
            source_ref=source_ref or {},
            blob_plan=blob_plan,
        )
        return _result_from_receipt(publication.receipt)
    except (ActionJobTerminalizationError, SandboxTeardownError):
        raise
    except DuplicateStreamingPublication:
        existing = _receipt_for_idempotency(project, envelope.idempotency_key)
        if existing is None:
            raise
        return replay_existing(existing)
    except TableReadRefused as exc:
        error = exc.error
    except TableError as exc:
        error = ActionError(
            code=exc.code,
            message=str(exc),
            action_kind=envelope.kind,
            field="params",
            details=exc.details or {},
        )
    except (TypeError, ValueError, ValidationError) as exc:
        duplicate = str(exc).startswith("duplicate_sheet_name:")
        error = ActionError(
            code="duplicate_sheet_name" if duplicate else "invalid_params",
            message=str(exc)
            if duplicate
            else "create_sheet parameters or produced rows are invalid",
            action_kind=envelope.kind,
            field="sheet_name" if duplicate else "params",
            details={"reason": str(exc)},
        )
    except Exception:
        logger.debug("table_publication_failed", exc_info=True)
        error = ActionError(
            code="project_write_failed",
            message="project write failed",
            action_kind=envelope.kind,
        )
    finally:
        try:
            if writer is not None:
                writer.abort()  # A committed writer's abort is deliberately a no-op.
        finally:
            try:
                if stager is not None:
                    stager.close()
            finally:
                publication_resources.close()
    result = _failed_result(
        project_id=project_id, action_kind=envelope.kind, error=error
    )
    if error.code == "action_cancelled":
        return result.model_copy(update={"status": "cancelled"})
    return result


def _run_reserved_table(
    project,
    action,
    resolved,
    *,
    project_id,
    params_hash,
    reserved_action_id,
    reserved_receipt_id,
):
    """Publish a buffered table using the existing receipt reservation and CAS."""
    from frisket.engine.executor.action_reservations import (
        _reserve_running_action_receipt,
        _reserved_receipt_result_from_existing,
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.artifact_timeline import TimelineError

    owns_receipt = reserved_receipt_id is None
    if owns_receipt:
        reservation = _reserve_running_action_receipt(
            project,
            action,
            project_id=project_id,
            params_hash=params_hash,
            reservation_kind="typed_table",
            result_from_existing_fn=_reserved_receipt_result_from_existing,
        )
        if isinstance(reservation, ActionResult):
            return reservation
        reserved_action_id = reservation["action_id"]
        reserved_receipt_id = reservation["receipt_id"]
    try:
        project.db.execute("BEGIN IMMEDIATE")
        stored = ReceiptStore(project).find_by_id(reserved_receipt_id)
        if stored is None or (
            stored.action_id != reserved_action_id
            or stored.params_hash != params_hash
            or stored.parsed().action_kind != action.kind
            or stored.parsed().idempotency_key != action.idempotency_key
            or stored.parsed().project_id != project_id
        ):
            raise ValueError("table reservation identity changed")
        if stored.status != "running":
            project.db.rollback()
            return _reserved_receipt_result_from_existing(
                project,
                stored,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
            )
        resolved["reserved_receipt"] = stored.parsed()
        result = _perform_table_in_txn(
            project,
            project.db.cursor(),
            action,
            project_id=project_id,
            action_id=reserved_action_id,
            receipt_id=reserved_receipt_id,
            params_hash=params_hash,
            resolved=resolved,
        )
        if result.status == "completed":
            project.db.commit()
            return result
        project.db.rollback()
        error = result.errors[0]
    except BaseException as exc:
        project.db.rollback()
        if not isinstance(exc, Exception) or isinstance(
            exc, ActionJobTerminalizationError
        ):
            raise
        logger.debug("reserved_table_publication_failed", exc_info=True)
        error = ActionError(
            code=exc.code
            if isinstance(exc, (TimelineError, TableError))
            else "project_write_failed",
            message=str(exc)
            if isinstance(exc, (TimelineError, TableError))
            else "Transcript segments could not be materialized",
            action_kind=action.kind,
        )
    if not owns_receipt:
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=error
        )
    return _terminalize_claimless_direct_failure(
        project,
        project_id=project_id,
        action_kind=action.kind,
        stored_receipt=ReceiptStore(project).find_by_id(reserved_receipt_id),
        error=error,
        project_write_failed_message="Transcript segments receipt could not be finalized.",
    )


def run_typed_table_action_job(project, envelope):
    from frisket.engine.executor.action_jobs import bind_typed_action_job

    bound = bind_typed_action_job(envelope, project=project)
    if isinstance(bound, ActionResult):
        return bound
    return run_typed_create_sheet_action(
        project,
        envelope.project_id,
        bound,
        reserved_action_id=envelope.action_id,
        reserved_receipt_id=envelope.receipt_id,
    )


def _append_table_batch(project, writer, table, batch, batch_files, action_kind):
    checked = _preflight_table_rows(
        project,
        table.model_copy(update={"rows": batch}),
        action_kind=action_kind,
    )
    if isinstance(checked, ActionError):
        raise TableReadRefused(checked)
    row_ids = writer.append_rows(checked)
    return [
        (row_id, name, handle)
        for row_id, files in zip(row_ids, batch_files, strict=True)
        for name, handle in files
    ]


def _projection_initial_values(readers, row, ordinal):
    for reader in readers:
        row = reader.initial_cell_values(row, ordinal)
    return row


def _perform_table_in_txn(
    project: Project,
    cur: Any,
    action: Any,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: dict[str, Any],
) -> ActionResult:
    projections = resolved.get("projection_readers", ())
    for reader in projections:
        reader.revalidate()
    try:
        for fact in resolved["reads"]:
            if fact.get("kind") == "cluster_receipt_source":
                validate_cluster_receipt_fence(project, fact)
            elif fact.get("kind") == "collection_expand_source_fingerprint":
                validate_collection_source(project, fact)
            elif fact.get("kind") == "semantic_join_link_source":
                validate_semantic_match_source(project, fact)
            elif fact.get("kind") == "joined_tables_source":
                validate_joined_tables_source(project, fact)
    except TableError as error:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=error.code, message=str(error), action_kind=action.kind
            ),
        )
    table = resolved["materialization"]
    duplicate = _duplicate_sheet_error(project, action.kind, table.sheet_name)
    if duplicate is not None:
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=duplicate
        )
    sources = resolved["sources"]
    parent_sheet_id = resolved["parent_sheet_id"]
    if parent_sheet_id is None:
        return _perform_import_rows_in_txn(
            project,
            cur,
            action,
            table,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            resolved=resolved,
        )
    rows = _preflight_table_rows(project, table, action_kind=action.kind)
    if isinstance(rows, ActionError):
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=rows
        )
    list_reader = resolved.get("list_reader")
    roles = resolved.get("source_roles", {})
    write = write_contributor_table(
        cur,
        ContributorTablePlan(
            action_kind=action.kind,
            label=f"{action.kind} {table.sheet_name}",
            target_sheet_name=table.sheet_name,
            parent_sheet_id=parent_sheet_id,
            op_spec={**resolved["op_spec"], "reads": resolved["reads"]},
            columns=[
                MaterializedColumnSpec(
                    name=column.name,
                    type=column.type,
                    format=column.format,
                    hidden=column.hidden,
                    ai_generated=any(
                        column.name in reader.generated_columns()
                        for reader in projections
                    )
                    if projections
                    else list_reader.source_ai_generated
                    if list_reader is not None
                    else resolved.get("collection_reader") is None
                    and resolved.get("joined_tables_reader") is None
                    and resolved.get("sheet_rows_reader") is None,
                )
                for column in table.columns
            ],
            rows=[
                ContributorMaterializedRow(
                    parent_row_id=lineage.parent.row_id
                    if lineage.parent is not None
                    else None,
                    values=_projection_initial_values(projections, row, ordinal),
                    sources=tuple(
                        dict.fromkeys(
                            (source.row_id, roles.get(source, "aggregate_source"))
                            for source in lineage.sources
                            if source is not lineage.parent or source in roles
                        )
                    ),
                )
                for ordinal, (lineage, row) in enumerate(
                    zip(sources, rows, strict=True)
                )
            ],
        ),
    )
    if list_reader is not None:
        from frisket.engine.executor.list_table_grounding import (
            propagate_list_item_evidence,
        )

        propagate_list_item_evidence(
            project,
            sheet_id=write.sheet_id,
            op_id=write.op_id,
            child_row_ids=[
                row_id
                for row_id, lineage in zip(write.row_ids, sources, strict=True)
                for source in lineage.sources
                if source in list_reader.item_associations
            ],
            sources=[
                source
                for lineage in sources
                for source in lineage.sources
                if source in list_reader.item_associations
            ],
            item_associations=list_reader.item_associations,
        )
    sheet_ref = {
        **write.materialized_sheet_ref,
        "name": table.sheet_name,
        "row_count": len(rows),
        "columns": write.column_ids,
        "reads": resolved["reads"],
    }
    projection_evidence = [
        evidence
        for reader in projections
        for evidence in reader.publication_evidence(write, rows, receipt_id=receipt_id)
    ]
    rows_ref = {
        **write.materialized_rows_ref,
        "values_sha256": _table_values_hash(rows),
    }
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[write.op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        warnings=list(resolved.get("warnings", ())),
        provider_use=list(resolved["collection_reader"].provider_use)
        if resolved.get("collection_reader") is not None
        else [],
        inputs=[
            ReceiptIO(name=f"read.{index}", ref=fact)
            for index, fact in enumerate(resolved["reads"])
        ],
        outputs=[
            ReceiptIO(name=table.sheet_name, ref=sheet_ref),
            *[
                ReceiptIO(
                    name=f"column.{column.name}",
                    ref={
                        **write.materialized_column_refs[column.name],
                        "name": column.name,
                        "type": column.type,
                        "hidden": column.hidden,
                    },
                )
                for column in table.columns
            ],
            ReceiptIO(name="materialized_rows", ref=rows_ref),
        ],
        evidence=[
            ReceiptEvidence(ref=write.lineage_parent_rows_ref),
            ReceiptEvidence(ref=write.materialized_row_sources_ref),
            *projection_evidence,
        ],
    )
    if (blob_plan := resolved.get("blob_plan")) is not None:
        published = publish_import_blobs(
            project,
            blob_plan.bind_row_ordinals(write.row_ids),
            sheet_id=write.sheet_id,
            column_ids=write.column_ids,
            op_id=write.op_id,
            receipt_id=receipt_id,
        )
        receipt.evidence.extend(ReceiptEvidence(ref=ref) for ref in published)
    if (matches := resolved.get("semantic_match_reader")) is not None:
        receipt.evidence.extend(matches.publication_evidence(write, sources))
    if (prior := resolved.get("reserved_receipt")) is not None:
        receipt.evidence[:0] = prior.evidence
        if not ReceiptStore(project).update_body_status(
            receipt, require_status="running", commit=False
        ):
            raise ActionJobTerminalizationError("table receipt completion did not land")
    else:
        ReceiptStore(project).insert_completed(receipt, commit=False)
    return _result_from_receipt(receipt)


def _table_values_hash(rows):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                rows, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )


def _parented_table_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    sheet_ref = _receipt_ref(receipt, "materialized_sheet")
    rows_ref = _receipt_ref(receipt, "materialized_rows")
    lineage_ref = _receipt_ref(receipt, "lineage_parent_rows")
    refs = (sheet_ref, rows_ref, lineage_ref)
    if any(ref is None for ref in refs):
        return receipt_stale_replay_error(
            receipt,
            "table replay receipt lacks materialized lineage refs",
        )

    assert sheet_ref is not None
    assert rows_ref is not None
    assert lineage_ref is not None
    sheet_id = _positive_ref_int(sheet_ref, "sheet_id")
    sheet_op_id = _positive_ref_int(sheet_ref, "op_id")
    rows_sheet_id = _positive_ref_int(rows_ref, "sheet_id")
    rows_op_id = _positive_ref_int(rows_ref, "op_id")
    lineage_child_sheet_id = _positive_ref_int(lineage_ref, "child_sheet_id")
    lineage_op_id = _positive_ref_int(lineage_ref, "op_id")
    row_ids = _positive_int_list(rows_ref.get("row_ids"))
    parent_row_ids = rows_ref.get("parent_row_ids")
    if not isinstance(parent_row_ids, list) or any(
        item is not None and (type(item) is not int or item <= 0)
        for item in parent_row_ids
    ):
        parent_row_ids = None
    pairs = lineage_ref.get("pairs")
    if (
        sheet_id is None
        or sheet_op_id is None
        or rows_sheet_id != sheet_id
        or rows_op_id != sheet_op_id
        or lineage_child_sheet_id != sheet_id
        or lineage_op_id != sheet_op_id
        or row_ids is None
        or parent_row_ids is None
        or len(row_ids) != len(parent_row_ids)
        or not isinstance(pairs, list)
    ):
        return receipt_stale_replay_error(
            receipt,
            "table replay materialized refs are invalid",
            details={"receipt_id": receipt.receipt_id},
        )

    expected_pairs = [
        {"child_row_id": row_id, "parent_row_id": parent_row_id}
        for row_id, parent_row_id in zip(row_ids, parent_row_ids, strict=True)
    ]
    if pairs != expected_pairs:
        return receipt_stale_replay_error(
            receipt,
            "table replay lineage evidence changed",
            details={"receipt_id": receipt.receipt_id, "sheet_id": sheet_id},
        )

    membership = _receipt_ref(receipt, "materialized_row_sources")
    if (
        membership is None
        or membership.get("op_id") != sheet_op_id
        or not materialized_row_sources_ref_matches(
            membership,
            load_materialized_row_sources_for_op(project.db, op_id=sheet_op_id),
        )
    ):
        return receipt_stale_replay_error(
            receipt, "table replay source membership changed"
        )

    sheet = project.db.execute(
        "SELECT id, name, parent_op_id FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    op = project.db.execute(
        "SELECT id, spec FROM ops WHERE id=? AND status='applied'",
        (sheet_op_id,),
    ).fetchone()
    parent_op_id = sheet["parent_op_id"] if sheet is not None else None
    if (
        sheet is None
        or parent_op_id is None
        or int(parent_op_id) != sheet_op_id
        or op is None
        or sheet["name"] != sheet_ref.get("name")
    ):
        return receipt_stale_replay_error(
            receipt,
            "table replay materialized sheet or op is missing",
            details={
                "receipt_id": receipt.receipt_id,
                "sheet_id": sheet_id,
                "op_id": sheet_op_id,
            },
        )

    try:
        stored_reads = json.loads(op["spec"])["reads"]
    except (TypeError, ValueError, KeyError):
        return receipt_stale_replay_error(
            receipt, "table replay source facts are missing"
        )
    if stored_reads != [
        item.ref for item in receipt.inputs
    ] or stored_reads != sheet_ref.get("reads"):
        return receipt_stale_replay_error(receipt, "table replay source facts changed")

    column_refs = {
        item.ref["name"]: item.ref
        for item in receipt.outputs
        if item.ref.get("kind") == "materialized_column"
        and isinstance(item.ref.get("name"), str)
    }
    live_columns = project.db.execute(
        "SELECT id, name, type, hidden FROM columns WHERE sheet_id=? ORDER BY position, id",
        (sheet_id,),
    ).fetchall()
    if (
        not column_refs
        or len(live_columns) != len(column_refs)
        or project.visible_row_ids(sheet_id) != row_ids
        or any(
            (ref := column_refs.get(column["name"])) is None
            or ref.get("column_id") != column["id"]
            or ref.get("type") != column["type"]
            or not isinstance(ref.get("hidden"), bool)
            or ref["hidden"] != bool(column["hidden"])
            for column in live_columns
        )
    ):
        return receipt_stale_replay_error(
            receipt, "table replay materialized columns or row membership changed"
        )
    by_column = {
        column["name"]: project.get_values(sheet_id, column["id"], row_ids=row_ids)
        for column in live_columns
    }
    actual_rows = [
        {name: values.get(row_id) for name, values in by_column.items()}
        for row_id in row_ids
    ]
    if _table_values_hash(actual_rows) != rows_ref.get("values_sha256"):
        return receipt_stale_replay_error(
            receipt, "table replay materialized values changed"
        )

    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows "
            f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
            [sheet_id, *row_ids],
        ).fetchall()
        by_id = {int(row["id"]): row["parent_row_id"] for row in rows}
        if len(by_id) != len(row_ids):
            return receipt_stale_replay_error(
                receipt,
                "table replay materialized rows are missing or hidden",
                details={"receipt_id": receipt.receipt_id, "row_ids": row_ids},
            )
        for row_id, parent_row_id in zip(row_ids, parent_row_ids, strict=True):
            if by_id.get(row_id) != parent_row_id:
                return receipt_stale_replay_error(
                    receipt,
                    "table replay materialized row lineage changed",
                    details={"receipt_id": receipt.receipt_id, "row_id": row_id},
                )
    return None


def _positive_int_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0
        for item in value
    ):
        return None
    return list(value)
