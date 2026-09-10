"""Registry-free typed ``map_rows`` planning and durable execution adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
import copy
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Mapping

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import SchemaValidator

from frisket.actions.geospatial_types import Geocoder, CensusDemographics
from frisket.actions.document_types import DocumentConverter
from frisket.actions.media_types import OcrReader, Transcriber, TranscriptText, OcrText
from frisket.actions.temporal_types import TopicSectionsReader, VisualCutsReader
from frisket.actions.pdf_table_types import PdfTablesReader
from frisket.actions.python_types import PythonEvaluator
from frisket.actions.http_types import HttpRequester
from frisket.actions.file_types import FileFetcher
from frisket.actions.media_download_types import MediaDownloader
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.row_media_types import FrameExtractor, FaceExtractor
from frisket.actions.cluster_types import ValueClusterer
from frisket.actions.classify_types import Classifier
from frisket.actions.ner_types import NerExtractor
from frisket.actions.translate_types import Translator
from frisket.actions.research_types import Researcher, WebSearcher
from frisket.actions.mcp_types import McpExtractor
from frisket.actions.opencorporates_types import OpenCorporates
from frisket.actions.core import (
    resolved_engine_options,
    CreateSheet,
    SemanticJoin,
    ROUTED_CAPABILITIES,
    routed_capability,
    MapRows,
    MapBatch,
    ModelRows,
    _DirectModelRows,
    ModelRowsEvaluationContext,
    OutputField,
    RegisteredAction,
    compile_output_schema,
    _value_annotation,
    _publication_return_schema,
)
from frisket.actions.types import (
    InvocationContext,
    PluginSecrets,
    ActionRequest,
    InputReference,
    DynamicOutput,
    MediaMetadataReader,
    ModelPrompt,
    ModelRef,
    Outcome,
    Row,
    RowError,
    RowResult,
    Rows,
    Template,
    discover_references,
    _without_none,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.engine.executor.invocation_context import HostInvocationContext
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.ops.base import OpContext, Recipe, RenderedCall
from frisket.engine.executor.action_lifecycle import (
    _PreparedMapExecution,
    _run_reserved_maprunner_action,
)
from frisket.engine.executor.action_reservations import (
    _finalize_reserved_action_receipt,
    _output_claim_token,
    _receipt_for_idempotency,
    _reserved_receipt_result_from_existing,
    _queued_receipt_job_id,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.action_support import (
    _external_cost_requires_confirmation_error,
    _model_cost_requires_confirmation_error,
)
from frisket.engine.runner.publication import PreparedRowPublication
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import EXPECTED_ROW_ERROR
from frisket.redaction import safe_error
from frisket.sdk.capture import (
    MixedOriginInputProvenanceUnsupported,
    _input_source_run_id,
    capture_maprunner_facts,
    maprunner_output_refs,
)
from frisket.sdk.envelope import model_run_status, row_failure_errors
from frisket.sdk.maprunner import build_receipt_from_provenance
from frisket.sdk.provenance import Provenance
from frisket.sdk.replay import (
    output_columns_replay_error,
    replay_expected_row_ids,
)

_FILE_CAPABILITIES = (
    FileFetcher,
    MediaDownloader,
    Screenshotter,
    FrameExtractor,
    FaceExtractor,
    OcrReader,
)


class TypedMapRowsPlanError(ValueError):
    """A request cannot be materialized against the selected project."""

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
        self.field = field
        self.details = MappingProxyType(dict(details or {}))


@dataclass(frozen=True)
class TypedMapRowsPlan:
    """Validated inputs for one explicit ``MapRunner`` invocation."""

    action: RegisteredAction
    request: ActionRequest
    source_columns: tuple[str, ...]
    source_column_ids: Mapping[str, int]
    source_column_types: Mapping[str, str]
    output_names: Mapping[str, str]
    output_target_preconditions: Mapping[str, int | None]
    output_fields: tuple[dict[str, Any], ...]
    request_identity: Mapping[str, Any]
    evaluation_context: ModelRowsEvaluationContext | None
    spec: Mapping[str, Any]
    program: Recipe

    def spec_dict(self) -> dict[str, Any]:
        return dict(self.spec)


@dataclass(frozen=True)
class TypedProjectReferences:
    """Project-owned facts for the semantic references in typed Params."""

    source_columns: tuple[str, ...]
    source_column_ids: Mapping[str, int]
    source_column_types: Mapping[str, str]
    visible_column_names: frozenset[str]


def _capability_engine(terminal, params: BaseModel, capability: type) -> str | None:
    name = terminal.engine_param
    if name is None:
        return None
    annotation = terminal.params_model.model_fields[name].annotation
    if annotation.__pydantic_generic_metadata__.get("args", ()) != (capability,):
        return None
    return getattr(params, name).root


def _model_backed_terminal(terminal) -> bool:
    return isinstance(terminal, ModelRows) or any(
        cap in getattr(terminal, "capabilities", ())
        for cap in (Researcher, McpExtractor)
    )


@dataclass(frozen=True)
class _MapTerminalPolicy:
    model_backed: bool
    external_confirmation: bool
    external_failure: bool
    captures_row_source_snapshot: bool

    @classmethod
    def for_terminal(cls, terminal: Any) -> _MapTerminalPolicy:
        capabilities = getattr(terminal, "capabilities", ())
        model_backed = _model_backed_terminal(terminal)
        routed = routed_capability(terminal) is not None
        return cls(
            model_backed=model_backed,
            external_confirmation=(
                WebSearcher in capabilities
                or routed
                or any(
                    capability in capabilities
                    for capability in (
                        HttpRequester,
                        FileFetcher,
                        MediaDownloader,
                        Screenshotter,
                        OpenCorporates,
                    )
                )
            ),
            external_failure=(
                WebSearcher in capabilities
                or routed
                or any(
                    capability in capabilities
                    for capability in (
                        HttpRequester,
                        FileFetcher,
                        MediaDownloader,
                        OpenCorporates,
                    )
                )
            ),
            captures_row_source_snapshot=any(
                capability in capabilities
                for capability in (
                    PdfTablesReader,
                    DocumentConverter,
                    OcrReader,
                    Transcriber,
                )
            ),
        )

    @property
    def map_error_code(self) -> str:
        if self.model_backed:
            return "model_run_failed"
        if self.external_failure:
            return "external_rows_failed"
        return "map_rows_failed"


class _TypedMapRowsProgram(Recipe):
    """Generic Recipe protocol adapter; never registered or action-specific."""

    consumes_resolution = False
    cost_class = "free"
    llm = False
    action_lifecycle_only = True
    # Typed Outcomes may intentionally carry low confidence into review. Plain
    # deterministic values still complete normally without inventing a score.
    auto_verify = False
    allow_all_empty_input = True

    def bind_project(self, project: Any) -> None:
        """Bind the project opened by the process that will execute this program."""

        self._project = project
        if PluginSecrets in getattr(self._terminal, "injections", ()):
            from frisket.authoring.workbench.native_plugin_secrets import (
                HostPluginSecrets,
            )

            self._plugin_secrets = HostPluginSecrets.from_binding(
                project, self._runtime_binding
            )

    def __init__(
        self,
        action: RegisteredAction,
        params: BaseModel,
        output_names: Mapping[str, str],
        resolved_output_fields: tuple[OutputField, ...],
        output_fields: tuple[dict[str, Any], ...],
        evaluation_context: ModelRowsEvaluationContext | None = None,
        runtime_binding: Any | None = None,
    ) -> None:
        self.name = action.action_id
        self.description = action.definition.description
        self._terminal = action.definition.run
        from frisket.actions.ner import NerParams
        from frisket.actions.extract import ExtractParams

        self._uses_ner = isinstance(params, NerParams)
        self._uses_extract = isinstance(params, ExtractParams)
        cost_policy = action.catalog_entry()["cost_policy"]
        self.external_cost_is_known_zero = (
            cost_policy["kind"] == "none" and not cost_policy["requires_confirmation"]
        )
        self._file_capabilities = _FILE_CAPABILITIES
        self._uses_row_files = any(
            cap in getattr(self._terminal, "capabilities", ())
            for cap in self._file_capabilities
        )
        if PythonEvaluator in getattr(self._terminal, "capabilities", ()):
            self.allow_all_empty_input = False
        self.capture_source_cells = any(
            cap in getattr(self._terminal, "capabilities", ())
            for cap in (
                VisualCutsReader,
                TopicSectionsReader,
                PdfTablesReader,
                DocumentConverter,
                Transcriber,
                NerExtractor,
                Researcher,
                McpExtractor,
                *self._file_capabilities,
            )
        )
        self.capture_source_cells |= self._uses_ner or self._uses_extract
        if isinstance(self._terminal, ModelRows):
            prompt_inputs = [getattr(params, self._terminal.source_param)]
            if self._terminal.evaluation is not None:
                # Evaluators intentionally send both the source material and
                # the separately declared answer under review.
                prompt_inputs.append(
                    getattr(params, self._terminal.evaluation.subject_param)
                )
            self.prompt_source_columns = tuple(
                reference.column for reference in discover_references(prompt_inputs)
            )
        if PdfTablesReader in getattr(self._terminal, "capabilities", ()):
            from frisket.engine.executor.pdf_tables_read import PdfResultEvidence

            self._pdf_evidence = PdfResultEvidence()
        if self._uses_row_files:
            self.capture_result_evidence = self._capture_file_result_evidence
        self._uses_document_converter = DocumentConverter in getattr(
            self._terminal, "capabilities", ()
        )
        self._uses_transcriber = Transcriber in getattr(
            self._terminal, "capabilities", ()
        )
        self._uses_translator = Translator in getattr(
            self._terminal, "capabilities", ()
        )
        if (
            self._uses_row_files
            or self._uses_document_converter
            or self._uses_transcriber
            or self._uses_translator
            or hasattr(self, "_pdf_evidence")
            or self._uses_ner
            or self._uses_extract
        ):
            self.write_result_evidence = self._write_result_evidence
        if self._uses_ner:
            self.capture_result_evidence = self._capture_ner_result_evidence
        if self._uses_extract:
            self.capture_result_evidence = self._capture_extract_result_evidence
        capability = routed_capability(self._terminal)
        if capability is not None:
            self.consumes_resolution = True
            self.execution_capability = ROUTED_CAPABILITIES[capability]
            self.cost_class = "metered"
        if any(
            cap in getattr(self._terminal, "capabilities", ())
            for cap in (HttpRequester, FileFetcher, MediaDownloader)
        ):
            self.cost_class = "unpriceable"
            self.disable_failure_halt = True
        if (
            WebSearcher in getattr(self._terminal, "capabilities", ())
            and self.cost_class == "free"
        ):
            self.cost_class = "metered"
        if OpenCorporates in getattr(self._terminal, "capabilities", ()):
            if runtime_binding is None:
                raise TypeError("OpenCorporates requires an installed action binding")
            self.cost_class = "unpriceable"
            self.external_capability = "external:opencorporates"
        self._params = params
        self._output_names = dict(output_names)
        self._output_fields = output_fields
        self._resolved_output_fields = tuple(
            replace(field, route=copy.deepcopy(field.route))
            if field.route is not None
            else field
            for field in resolved_output_fields
        )
        self._evaluation_context = evaluation_context
        self._runtime_binding = runtime_binding
        self._plugin_secrets = None
        self._capability_bindings: ContextVar[tuple[Any, ...] | None] = ContextVar(
            "typed_row_capabilities", default=None
        )
        self._output_adapters = {
            field.key: TypeAdapter(field.annotation) for field in resolved_output_fields
        }
        self._output_validator = (
            None
            if self._terminal.output_model is DynamicOutput
            else SchemaValidator(
                _publication_return_schema(
                    self._terminal.output_model.__pydantic_core_schema__
                ),
                _use_prebuilt=False,
            )
        )

    def estimate(self, project, spec, rows, *, resolution=None):
        if OpenCorporates in getattr(self._terminal, "capabilities", ()):
            return {
                "cost": None,
                "cost_source": "external_metered_declared",
                "requires_confirmation": True,
                "remote_capability": "external:opencorporates",
                "rows": len(rows),
            }
        if not self.consumes_resolution:
            return None
        if resolution is None:
            raise RuntimeError(
                "typed routed capability requires its invocation resolution"
            )
        from frisket.ops.cost_source import estimate_from_cost_basis

        estimate = estimate_from_cost_basis(
            resolution,
            zero_cost_source=(
                "free_public_api"
                if resolution.resolution.facts.engine in {"nominatim", "us_census_acs"}
                else "free_local"
            ),
            warning=(
                "billable quantity is unknown before the run; the cost cannot be estimated"
                if resolution.resolution.estimate_basis.quantity_hint is None
                else None
            ),
        )
        if Transcriber in getattr(self._terminal, "capabilities", ()):
            estimate["audio_seconds"] = (
                resolution.resolution.estimate_basis.quantity_hint
            )

        if any(
            cap in getattr(self._terminal, "capabilities", ())
            for cap in (HttpRequester, FileFetcher, MediaDownloader)
        ):
            estimate.update(cost=None, cost_source="unknown")
        return estimate

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [dict(field) for field in self._output_fields]

    def max_row_concurrency(self, spec):
        if OpenCorporates in getattr(self._terminal, "capabilities", ()):
            return 1
        if any(
            cap in getattr(self._terminal, "capabilities", ())
            for cap in (McpExtractor, Classifier)
        ):
            return 1
        if MediaDownloader in getattr(self._terminal, "capabilities", ()):
            from frisket.engine.executor.media_download import download_max_concurrency

            return download_max_concurrency()
        if OcrReader in getattr(self._terminal, "capabilities", ()):
            from frisket.ops.ocr_engines import (
                ENGINE_ALIASES,
                LIGHT_ENGINE,
                RUN_SCOPED_ENGINES,
            )
            from frisket.engine._workers.rapidocr_session import rapidocr_topology

            engine = spec.get("engine") or LIGHT_ENGINE
            if ENGINE_ALIASES.get(engine, engine) in RUN_SCOPED_ENGINES:
                return rapidocr_topology().workers
        if Transcriber in getattr(self._terminal, "capabilities", ()):
            from frisket.sdk.ops.transcribe_engines import (
                transcription_max_row_concurrency,
            )

            return transcription_max_row_concurrency(spec)
        return None

    def requires_row_effect_checkpoint(self, spec):
        return OpenCorporates in getattr(self._terminal, "capabilities", ())

    def run_provenance_model(self, spec):
        if Classifier in getattr(self._terminal, "capabilities", ()):
            from frisket.engine.executor.classify_read import LOCAL_SEMANTIC_MODEL

            return f"fastembed/{LOCAL_SEMANTIC_MODEL}"
        if isinstance(self._terminal, _DirectModelRows):
            return getattr(self._params, self._terminal.engine_param).root
        if routed_capability(self._terminal) is not None:
            return spec.get("engine")
        return super().run_provenance_model(spec)

    @asynccontextmanager
    async def execution_scope(self, spec, ctx, *, expected_rows):
        async with AsyncExitStack() as resources:
            if self._uses_translator:
                from frisket.execution.attempt import attempt_in_scope

                attempt = attempt_in_scope(ctx.extras)
                self._translation_writer = (
                    attempt.attempt_id if attempt is not None else None
                )
            if self._uses_extract:
                from frisket.engine.executor.extract_result_evidence import (
                    ExtractResultEvidence,
                )
                from frisket.execution.attempt import attempt_in_scope

                grounding = self._params.grounding
                policy = self._params.evidence_policy
                names = {field.name for field in self._params.fields}
                self._extract_evidence = ExtractResultEvidence(
                    ctx.project,
                    fields={
                        field.key: field
                        for field in self._resolved_output_fields
                        if field.key in names
                    },
                    output_names=self._output_names,
                    required_fields={
                        field.name for field in self._params.fields if field.required
                    },
                    grounding_enabled=bool(grounding and grounding.enabled),
                    citation_required=bool(
                        (grounding and grounding.citation_required)
                        or (policy and policy.citation_required)
                    ),
                    source_columns=self._params.source_document_columns,
                )
                attempt = attempt_in_scope(ctx.extras)
                self._extract_writer = (
                    attempt.attempt_id if attempt is not None else None
                )
            if self._uses_row_files:
                from frisket.engine.executor.row_file_stage import RowFileStager
                from frisket.execution.attempt import attempt_in_scope

                self._file_stager = RowFileStager(
                    ctx.project, preview=ctx.extras.get("preview") is True
                )
                resources.callback(self._file_stager.close)
                attempt = attempt_in_scope(ctx.extras)
                self._file_writer = attempt.attempt_id if attempt is not None else None
            bindings = []
            for capability in getattr(self._terminal, "capabilities", ()):
                if capability is Classifier:
                    from frisket.engine.executor.classify_read import AdmittedClassifier

                    reader = AdmittedClassifier(cancelled=ctx.extras.get("cancelled"))
                elif capability is NerExtractor:
                    from frisket.engine.executor.ner_read import AdmittedNerExtractor

                    reader = AdmittedNerExtractor(
                        ctx,
                        engine=_capability_engine(
                            self._terminal, self._params, capability
                        ),
                    )
                elif capability is Translator:
                    from frisket.engine.executor.translate_read import (
                        AdmittedTranslator,
                    )

                    reader = AdmittedTranslator(
                        ctx,
                        engine=_capability_engine(
                            self._terminal, self._params, capability
                        ),
                        options=resolved_engine_options(self._terminal, self._params)
                        or {},
                    )
                elif capability in (Researcher, McpExtractor):
                    if capability is Researcher:
                        from frisket.engine.executor.research_read import (
                            AdmittedResearcher,
                        )

                        reader = AdmittedResearcher(ctx, model=spec["model"])
                    else:
                        from frisket.engine.executor.mcp_extract_read import (
                            AdmittedMcpExtractor,
                        )

                        reader = AdmittedMcpExtractor(
                            ctx,
                            model=spec["model"],
                            server_ids=self._params.mcp_server_ids.root,
                            response_schema=self._agent_response_schema(),
                        )
                    resources.push_async_callback(reader.aclose)
                    await reader.start(expected_rows=expected_rows)
                    bindings.append(reader)
                    continue
                elif capability is PythonEvaluator:
                    from frisket.engine.executor.python_evaluator import (
                        AdmittedPythonEvaluator,
                    )

                    from frisket.engine.store.receipts import ReceiptStore
                    from frisket.execution.attempt import attempt_in_scope

                    record = None
                    if ctx.extras.get("preview") is not True:
                        attempt = attempt_in_scope(ctx.extras)
                        if attempt is None:
                            raise RuntimeError(
                                "Python evaluator requires its admitted writer"
                            )
                        record = partial(
                            ReceiptStore(ctx.project).record_python_code_hash,
                            run_id=ctx.extras["run_id"],
                            writer_attempt_id=attempt.attempt_id,
                            claim_token=ctx.extras["claim_token"],
                        )
                    reader = AdmittedPythonEvaluator(
                        cancelled=ctx.extras.get("cancelled"), record_code_hash=record
                    )
                elif capability is WebSearcher:
                    from frisket.engine.executor.web_search_read import (
                        AdmittedWebSearcher,
                    )

                    reader = AdmittedWebSearcher(ctx)
                elif capability is HttpRequester:
                    from frisket.engine.executor.http_request import (
                        AdmittedHttpRequester,
                    )

                    reader = AdmittedHttpRequester(ctx)
                elif capability is MediaDownloader:
                    from frisket.engine.executor.media_download import (
                        AdmittedMediaDownloader,
                    )

                    reader = AdmittedMediaDownloader(
                        ctx.project,
                        self._file_stager,
                        cancelled=ctx.extras.get("cancelled"),
                    )
                elif capability is FileFetcher:
                    from frisket.engine.executor.file_fetch import AdmittedFileFetcher

                    reader = AdmittedFileFetcher(
                        ctx.project,
                        self._file_stager,
                        cancelled=ctx.extras.get("cancelled"),
                    )
                elif capability is Screenshotter:
                    from frisket.engine.executor.screenshot_read import (
                        AdmittedScreenshotter,
                    )

                    reader = AdmittedScreenshotter(
                        self._file_stager,
                        cancelled=ctx.extras.get("cancelled"),
                        browser_renderer=ctx.extras.get("url_capture_browser"),
                    )
                elif capability in (FrameExtractor, FaceExtractor):
                    from frisket.engine.executor.row_media_read import (
                        AdmittedFrameExtractor,
                        AdmittedFaceExtractor,
                    )

                    factory = (
                        AdmittedFrameExtractor
                        if capability is FrameExtractor
                        else AdmittedFaceExtractor
                    )
                    reader = factory(
                        ctx.project,
                        self._file_stager,
                        cancelled=ctx.extras.get("cancelled"),
                        execution_limits=ctx.execution_limits,
                    )
                elif capability is PdfTablesReader:
                    from frisket.engine.executor.pdf_tables_read import (
                        AdmittedPdfTablesReader,
                    )

                    reader = AdmittedPdfTablesReader(
                        ctx.project,
                        cancelled=ctx.extras.get("cancelled"),
                        execution_limits=ctx.execution_limits,
                    )
                elif capability is TopicSectionsReader:
                    from frisket.engine.executor.topic_sections_read import (
                        AdmittedTopicSectionsReader,
                    )

                    reader = AdmittedTopicSectionsReader(
                        ctx.project,
                        cancelled=ctx.extras.get("cancelled"),
                        engine=_capability_engine(
                            self._terminal, self._params, TopicSectionsReader
                        ),
                    )
                elif capability is VisualCutsReader:
                    from frisket.engine.executor.visual_cuts_read import (
                        AdmittedVisualCutsReader,
                    )

                    reader = AdmittedVisualCutsReader(
                        ctx.project,
                        preview=ctx.extras.get("preview") is True,
                        cancelled=ctx.extras.get("cancelled"),
                        execution_limits=ctx.execution_limits,
                    )
                elif capability is MediaMetadataReader:
                    from frisket.engine.executor.media_metadata_read import (
                        AdmittedMediaMetadataReader,
                    )

                    reader = AdmittedMediaMetadataReader(
                        ctx.project,
                        preview=ctx.extras.get("preview") is True,
                        cancelled=ctx.extras.get("cancelled"),
                    )
                elif capability is Geocoder:
                    from frisket.engine.executor.geocode_capability import (
                        AdmittedGeocoder,
                    )

                    reader = AdmittedGeocoder(ctx)
                elif capability is CensusDemographics:
                    from frisket.engine.executor.census_capability import (
                        AdmittedCensusDemographics,
                    )

                    reader = AdmittedCensusDemographics(ctx)
                elif capability is OpenCorporates:
                    from frisket.engine.executor.opencorporates_read import (
                        AdmittedOpenCorporates,
                    )

                    reader = await resources.enter_async_context(
                        AdmittedOpenCorporates(ctx, self._runtime_binding)
                    )
                    bindings.append(reader)
                    continue
                elif capability in (OcrReader, Transcriber):
                    from frisket.actions.media_options import (
                        OcrOptions,
                        TranscriptionOptions,
                    )
                    from frisket.engine.executor.ocr_read import AdmittedOcrReader
                    from frisket.engine.executor.transcription_read import (
                        AdmittedTranscriber,
                    )

                    options_type = (
                        OcrOptions if capability is OcrReader else TranscriptionOptions
                    )
                    options = {
                        key: spec[key]
                        for key in options_type.model_fields
                        if key in spec
                    }
                    reader = (
                        AdmittedOcrReader(
                            ctx,
                            self._file_stager,
                            engine=spec.get("engine"),
                            options=options,
                        )
                        if capability is OcrReader
                        else AdmittedTranscriber(
                            ctx, engine=spec.get("engine"), options=options
                        )
                    )
                    # Register cleanup before start so a partially opened scope
                    # cannot strand a local process or its exclusive lease.
                    resources.push_async_callback(reader.aclose)
                    await reader.start(expected_rows=expected_rows)
                    from frisket.execution.attempt import attempt_in_scope

                    attempt = attempt_in_scope(ctx.extras)
                    self._media_writer = (
                        attempt.attempt_id if attempt is not None else None
                    )
                    bindings.append(reader)
                    continue
                elif capability is DocumentConverter:
                    from frisket.engine.executor.document_convert import (
                        AdmittedDocumentConverter,
                    )

                    reader = AdmittedDocumentConverter(
                        ctx,
                        engine=_capability_engine(
                            self._terminal, self._params, capability
                        ),
                        cancelled=ctx.extras.get("cancelled"),
                    )
                    from frisket.execution.attempt import attempt_in_scope

                    attempt = attempt_in_scope(ctx.extras)
                    self._document_writer = (
                        attempt.attempt_id if attempt is not None else None
                    )
                else:
                    raise TypeError("typed row capability has no host implementation")
                resources.push_async_callback(reader.aclose)
                bindings.append(reader)
            token = self._capability_bindings.set(tuple(bindings))
            primary: BaseException | None = None
            try:
                yield
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    try:
                        await resources.aclose()
                    finally:
                        self._persist_returned_capability_facts(ctx, bindings)
                except BaseException as drain_error:
                    if primary is not None:
                        primary.add_note(
                            "Returned provider accounting could not be finalized."
                        )
                        raise primary from drain_error
                    raise
                finally:
                    self._capability_bindings.reset(token)

    @staticmethod
    def _persist_returned_capability_facts(ctx: OpContext, bindings: list[Any]) -> None:
        """Returned facts outlive author failure and cancelled publication.

        The existing fenced writer deduplicates the capability's stable call
        IDs, so normal cell publication can carry the same facts unchanged.
        """
        from frisket.execution.attempt import attempt_in_scope

        attempt = attempt_in_scope(ctx.extras)
        if ctx.extras.get("preview") is True and attempt is None:
            return
        batch = []
        output_columns = ctx.extras.get("output_columns") or {}
        column_id = next(iter(output_columns.values()), None)
        for binding in bindings:
            request_accounting = getattr(binding, "request_accounting", {})
            if request_accounting.get("model_calls"):
                batch.append(
                    {"row_id": None, "column_id": column_id, **request_accounting}
                )
            for row_id, accounting in getattr(binding, "accounting_by_row", {}).items():
                if accounting.get("model_calls"):
                    batch.append(
                        {
                            "row_id": row_id,
                            "column_id": column_id,
                            **accounting,
                        }
                    )
        if not batch:
            return
        from frisket.engine.store.runs import RunResultStore

        if attempt is None or (column_id is None and attempt.receipt_id is None):
            raise RuntimeError(
                "returned capability facts require their admitted output writer"
            )
        RunResultStore(ctx.project).write_returned_call_accounting(
            attempt.run_id,
            batch,
            writer_attempt_id=attempt.attempt_id,
            authorized_attempt_id=attempt.attempt_id,
            claim_token=ctx.extras.get("claim_token"),
            receipt_id=attempt.receipt_id,
        )

    async def execute(
        self,
        row_values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> PreparedRowPublication:
        bindings = self._capability_bindings.get()
        if getattr(self._terminal, "capabilities", ()) and bindings is None:
            raise RuntimeError("typed row capabilities require an invocation scope")
        try:
            row = Row(row_values)
            row_bindings = [
                binding.bind_row(
                    row,
                    sheet_id=spec["sheet_id"],
                    row_id=ctx.extras.get("row_id"),
                    sources=getattr(row_values, "source_cells", None),
                    ctx=ctx,
                )
                if capability
                in (
                    DocumentConverter,
                    Transcriber,
                    OcrReader,
                    NerExtractor,
                    Translator,
                    Researcher,
                    WebSearcher,
                    McpExtractor,
                )
                else binding.bind_row(
                    row,
                    sheet_id=spec["sheet_id"],
                    row_id=ctx.extras.get("row_id"),
                    sources=getattr(row_values, "source_cells", None),
                )
                if capability
                in (
                    VisualCutsReader,
                    TopicSectionsReader,
                    PdfTablesReader,
                    Classifier,
                    *self._file_capabilities,
                )
                else binding.bind_row(ctx)
                if capability is Geocoder
                else binding.bind_rows((ctx.extras["row_id"],))
                if capability is CensusDemographics
                else binding.bind_row(ctx)
                if capability is OpenCorporates
                else binding
                for capability, binding in zip(
                    getattr(self._terminal, "capabilities", ()),
                    bindings or (),
                    strict=True,
                )
            ]
            produced = self._terminal.handler(
                self._params, row, *self._handler_arguments(row_bindings, ctx)
            )
            if inspect.isawaitable(produced):
                produced = await produced
            topic_reader = next(
                (
                    binding
                    for capability, binding in zip(
                        getattr(self._terminal, "capabilities", ()),
                        row_bindings,
                        strict=True,
                    )
                    if capability is TopicSectionsReader
                ),
                None,
            )
            publication = self._prepare_publication(
                produced,
                topic_reader=topic_reader,
                output_columns=ctx.extras.get("output_columns") or {},
                row_files=self._file_stager.bind_row(ctx.extras["row_id"])
                if self._uses_row_files
                else None,
            )
            for capability, binding in zip(
                getattr(self._terminal, "capabilities", ()), row_bindings, strict=True
            ):
                if capability is PdfTablesReader:
                    self._pdf_evidence.capture(
                        ctx,
                        binding.publication_facts(
                            produced.output,
                            self._resolved_output_fields,
                            self._output_names,
                            ctx.extras.get("output_columns") or {},
                        ),
                    )
            return self._with_accounting(publication, ctx.extras.get("row_id"))
        except RowError as error:
            if self._uses_row_files:
                self._file_stager.discard_row(ctx.extras.get("row_id"))
            return self._with_accounting(
                self._row_error_publication(error), ctx.extras.get("row_id")
            )

    def _with_accounting(self, publication, row_id):
        calls = [
            copy.deepcopy(call)
            for binding in self._capability_bindings.get() or ()
            for call in getattr(binding, "calls_by_row", {}).get(row_id, ())
        ]
        if self._uses_row_files:
            calls.extend(self._file_stager.observations(row_id))
        if calls:
            next(iter(publication.cells.values()))["row_file_calls"] = calls
        if Researcher in getattr(self._terminal, "capabilities", ()) and any(
            call.get("kind") == "research_answer" and call.get("unverified_memory")
            for call in calls
        ):
            answer = publication.cells.get(self._output_names["answer"])
            if answer is not None and answer.get("error") is None:
                answer["outcome"] = "unverified_memory"
        for binding in self._capability_bindings.get() or ():
            accounting = getattr(binding, "accounting_by_row", {}).get(row_id)
            if accounting is not None:
                return publication, accounting
        return publication

    def _handler_arguments(self, bindings, ctx):
        capabilities = iter(bindings)
        return tuple(
            HostInvocationContext(ctx.extras.get("cancelled"))
            if kind is InvocationContext
            else self._required_plugin_secrets()
            if kind is PluginSecrets
            else next(capabilities)
            for kind in self._terminal.injections
        )

    def _required_plugin_secrets(self):
        if self._plugin_secrets is None:
            raise RuntimeError("PluginSecrets requires a bound invocation project")
        return self._plugin_secrets

    def _row_error_publication(self, error: RowError) -> PreparedRowPublication:
        from frisket.engine.executor.http_request import HttpRequestCancelled

        safe = safe_error(error.code, error.message, max_chars=566)
        diagnostic = {
            "value": None,
            "error": safe.detail,
            "error_code": safe.code,
            "outcome": "cancelled"
            if isinstance(error, HttpRequestCancelled)
            else EXPECTED_ROW_ERROR,
        }
        return PreparedRowPublication(
            trace_data={"row_error": {"code": safe.code, "message": safe.detail}},
            cells={name: dict(diagnostic) for name in self._output_names.values()},
        )

    def _prepare_publication(
        self, produced: Any, *, topic_reader=None, output_columns=None, row_files=None
    ) -> PreparedRowPublication:
        if not isinstance(produced, RowResult):
            raise TypeError("typed row handler returned something other than RowResult")

        output = (
            self._terminal.output_model.model_validate(produced.output)
            if self._output_validator is None
            else self._output_validator.validate_python(
                produced.output, strict=True, by_name=True
            )
        )
        authored_names = {
            field.key: self._output_names[field.key]
            for field in self._resolved_output_fields
            if not field.hidden or field.route is not None
        }
        if isinstance(output, DynamicOutput):
            if set(output.root) != set(authored_names):
                raise ValueError("dynamic output keys do not match declared outputs")
            values = {
                key: self._output_adapters[key].validate_python(value)
                for key, value in output.root.items()
            }
            serialized = {
                key: self._output_adapters[key].dump_python(value, mode="json")
                for key, value in values.items()
            }
            from frisket.authoring import column_types

            for field in self._resolved_output_fields:
                if field.key not in serialized:
                    continue
                value = serialized[field.key]
                if isinstance(values[field.key], Outcome):
                    value = value["value"] if value["status"] == "ok" else None
                if value is not None:
                    from jsonschema import Draft202012Validator
                    from referencing import Registry

                    schema_error = next(
                        Draft202012Validator(
                            dict(field.schema), registry=Registry()
                        ).iter_errors(value),
                        None,
                    )
                    if schema_error is not None:
                        raise RowError("return_schema_mismatch", schema_error.message)
                if field.route is not None:
                    from frisket.engine.executor.action_support import (
                        _json_schema_error,
                    )

                    schema_error = _json_schema_error(
                        values[field.key], field.route.schema
                    )
                    if schema_error is not None:
                        raise RowError("return_schema_mismatch", schema_error)
                if field.route is not None and not column_types.validate_value(
                    field.column_type, values[field.key]
                ):
                    raise RowError(
                        "return_schema_mismatch",
                        f"route {field.key!r} value does not match target type {field.column_type!r}",
                    )
        else:
            if getattr(self._terminal, "active_outputs", None) is not None:
                missing = set(authored_names) - output.model_fields_set
                if missing:
                    raise ValueError(
                        f"active output fields are missing: {sorted(missing)}"
                    )
            values = {key: getattr(output, key) for key in authored_names}
            serialized = (
                {
                    field.key: row_files.dump_field(
                        field.annotation, values[field.key], field.key
                    )
                    for field in self._resolved_output_fields
                    if field.key in authored_names
                }
                if row_files is not None
                else output.model_dump(mode="json", include=set(authored_names))
            )
        cells: dict[str, dict[str, Any]] = {}
        for logical_name in authored_names:
            final_name = self._output_names[logical_name]
            raw_value = values[logical_name]
            if not isinstance(raw_value, Outcome):
                cells[final_name] = {"value": serialized[logical_name]}
                if row_files is not None:
                    cells[final_name]["row_files"] = row_files.descriptors(logical_name)
                continue
            if raw_value.status == "failed":
                cells[final_name] = {
                    "value": None,
                    "error": raw_value.message,
                    "error_code": raw_value.code,
                    "outcome": "model_error",
                }
                continue
            cell = {"value": serialized[logical_name]["value"]}
            if row_files is not None:
                cell["row_files"] = row_files.descriptors(logical_name)
            if raw_value.confidence is not None:
                cell["confidence"] = raw_value.confidence
            if raw_value.justification is not None:
                cell["justification"] = raw_value.justification
            if raw_value.evidence:
                cell["evidence"] = [
                    claim.model_dump(mode="json", exclude_none=True)
                    for claim in raw_value.evidence
                ]
            if raw_value.warnings:
                cell["warnings"] = list(raw_value.warnings)
            cells[final_name] = cell
        if topic_reader is not None:
            cells.update(
                topic_reader.publication_cells(
                    output,
                    self._resolved_output_fields,
                    self._output_names,
                    output_columns,
                    authored_output=produced.output,
                )
            )
        return PreparedRowPublication(trace_data={"output": serialized}, cells=cells)

    def _capture_file_result_evidence(
        self, values, capture, results, spec, *, row_id, **kwargs
    ):
        del values, capture, spec, kwargs
        self._file_stager.capture(
            results,
            row_id=row_id,
            fields=self._resolved_output_fields,
            output_names=self._output_names,
        )

    def _capture_ner_result_evidence(self, values, capture, results, spec, **kwargs):
        from frisket.ops.ner_evidence import (
            capture_result_evidence,
            typed_ner_evidence_spec,
        )

        capture_result_evidence(
            values,
            capture,
            results,
            {**spec, **typed_ner_evidence_spec(self._params, self._output_names)},
            **kwargs,
        )

    def _capture_extract_result_evidence(
        self, values, capture, results, spec, **kwargs
    ):
        self._extract_evidence.capture(values, capture, results, spec, **kwargs)

    def _write_result_evidence(
        self, project, spec, *, batch, run_id, claim_token, **kwargs
    ):
        from frisket.engine.executor.row_file_stage import write_row_file_evidence

        if self._uses_translator:
            from frisket.engine.store.receipts import ReceiptStore

            for cell in batch:
                for call in cell.get("row_file_calls", ()):
                    if call.get("kind") != "translate_read":
                        continue
                    for artifact in call.get("artifacts", ()):
                        ReceiptStore(project)._record_writer_evidence(
                            {**artifact, "kind": "map_translate_artifact"},
                            run_id=run_id,
                            writer_attempt_id=self._translation_writer,
                            claim_token=claim_token,
                        )

        if self._uses_extract:
            self._extract_evidence.write(
                project,
                spec,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                writer_attempt_id=self._extract_writer,
                **kwargs,
            )

        if self._uses_ner:
            from frisket.ops.ner_evidence import (
                _write_ner_result_evidence,
                typed_ner_evidence_spec,
            )

            _write_ner_result_evidence(
                project,
                {**spec, **typed_ner_evidence_spec(self._params, self._output_names)},
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                **{
                    key: kwargs[key]
                    for key in (
                        "sheet_id",
                        "op_id",
                        "output_columns",
                        "input_column_ids",
                    )
                },
            )

        if self._uses_row_files:
            write_row_file_evidence(
                project,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                writer_attempt_id=self._file_writer,
                action_kind=self.name,
                output_columns={
                    key: kwargs["output_columns"][name]
                    for key, name in self._output_names.items()
                },
            )
        if self._uses_document_converter:
            from frisket.engine.executor.document_convert import (
                write_document_convert_evidence,
            )

            write_document_convert_evidence(
                project,
                spec,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                writer_attempt_id=self._document_writer,
                **kwargs,
            )
        if hasattr(self, "_pdf_evidence"):
            self._pdf_evidence.write(
                project,
                spec,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                **kwargs,
            )
        if self._uses_transcriber:
            from frisket.engine.executor.transcription_evidence import (
                write_transcription_evidence,
            )

            write_transcription_evidence(
                project,
                spec,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                writer_attempt_id=self._media_writer,
                transcript_columns={
                    kwargs["output_columns"][self._output_names[field.key]]
                    for field in self._resolved_output_fields
                    if _without_none(_value_annotation(field.annotation))
                    == (TranscriptText,)
                },
                **kwargs,
            )
        if OcrReader in getattr(self._terminal, "capabilities", ()):
            from frisket.engine.executor.ocr_evidence import write_ocr_evidence

            write_ocr_evidence(
                project,
                spec,
                batch=batch,
                run_id=run_id,
                claim_token=claim_token,
                writer_attempt_id=self._media_writer,
                ocr_columns={
                    kwargs["output_columns"][self._output_names[field.key]]
                    for field in self._resolved_output_fields
                    if _without_none(_value_annotation(field.annotation)) == (OcrText,)
                },
                **kwargs,
            )


class _TypedMapBatchProgram(_TypedMapRowsProgram):
    """Typed batch adapter over MapRunner's existing whole-scope branch."""

    auto_verify = True

    async def execute_batch(
        self,
        values_by_row: dict[int, dict[str, Any]],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[int, PreparedRowPublication]:
        del spec
        bindings = self._capability_bindings.get()
        if getattr(self._terminal, "capabilities", ()) and bindings is None:
            raise RuntimeError("typed batch capabilities require an invocation scope")
        row_ids = tuple(values_by_row)
        batch_bindings = [
            binding.bind_rows(row_ids) if capability is CensusDemographics else binding
            for capability, binding in zip(
                getattr(self._terminal, "capabilities", ()), bindings or (), strict=True
            )
        ]
        rows = Rows({row_id: Row(values) for row_id, values in values_by_row.items()})
        produced = self._terminal.handler(
            self._params, rows, *self._handler_arguments(batch_bindings, ctx)
        )
        if inspect.isawaitable(produced):
            produced = await produced
        if not isinstance(produced, Mapping):
            raise TypeError("map_batch handler returned something other than a mapping")
        unexpected = set(produced) - set(values_by_row)
        if unexpected:
            raise ValueError(
                f"map_batch returned unexpected row ids: {sorted(unexpected)}"
            )
        return {
            row_id: self._with_accounting(
                self._row_error_publication(row_result)
                if isinstance(row_result, RowError)
                else self._prepare_publication(row_result),
                row_id,
            )
            for row_id, row_result in produced.items()
        }


class _TypedAgentRowsProgram(_TypedMapRowsProgram):
    """Typed row handlers whose capability owns a bounded model/tool loop."""

    llm = True
    cost_class = "unpriceable"

    def requires_row_effect_checkpoint(self, spec):
        return True

    def _agent_response_schema(self):
        return {
            "type": "object",
            "properties": {
                field.key: dict(field.schema) for field in self._resolved_output_fields
            },
            "required": [field.key for field in self._resolved_output_fields],
            "additionalProperties": False,
        }

    def render(self, row_values, spec):
        from frisket.actions.row_research import source_values
        from frisket.ai.message_content import render_input_block

        row = Row(row_values)
        content = render_input_block(source_values(row, self._params.source))
        if Researcher in self._terminal.capabilities:
            instruction = self._params.question.render(row)
        else:
            instruction = self._params.instruction or "Extract the requested fields."
            if self._params.context:
                instruction = (
                    f"Dataset context: {self._params.context}\n\n{instruction}"
                )
        content.append({"type": "text", "text": instruction})
        return RenderedCall(
            messages=[{"role": "user", "content": content}],
            schema=self._agent_response_schema(),
        )

    async def run_agent(self, row_values, spec, ctx, router):
        result = await self.execute(row_values, spec, ctx)
        return result if isinstance(result, tuple) else (result, {})


class _TypedModelRowsProgram(_TypedMapRowsProgram):
    """Private adapter from a pure ``ModelRows`` renderer to ``MapRunner``."""

    cost_class = "metered"
    llm = True
    allow_all_empty_input = False
    empty_input_error_field = "params.source"

    def __init__(
        self,
        action: RegisteredAction,
        params: BaseModel,
        output_names: Mapping[str, str],
        resolved_output_fields: tuple[OutputField, ...],
        output_fields: tuple[dict[str, Any], ...],
        evaluation_context: ModelRowsEvaluationContext | None = None,
        runtime_binding: Any | None = None,
    ) -> None:
        super().__init__(
            action,
            params,
            output_names,
            resolved_output_fields,
            output_fields,
            evaluation_context,
            runtime_binding,
        )
        self._response_model = self._terminal.response_model
        self._response_schema = (
            {
                "type": "object",
                "properties": {
                    field.key: dict(field.schema) for field in resolved_output_fields
                },
                "required": [field.key for field in resolved_output_fields],
                "additionalProperties": False,
            }
            if self._response_model is DynamicOutput
            else compile_output_schema(self._response_model)
        )
        self._logical_output_by_final = {
            final: logical for logical, final in output_names.items()
        }

    def postprocess_value(self, name: str, value: Any, spec: dict[str, Any]) -> Any:
        """Apply the Output model's field type and functional metadata."""

        del spec
        logical = self._logical_output_by_final[name]
        adapter = self._output_adapters[logical]
        return adapter.dump_python(adapter.validate_python(value), mode="json")

    def normalize_model_output(
        self, data: dict[str, Any], spec: dict[str, Any], *, row_values: dict[str, Any]
    ) -> PreparedRowPublication:
        del spec
        response = self._response_model.model_validate(data)
        produced = (
            self._terminal.complete(self._params, Row(row_values), response)
            if self._terminal.complete is not None
            else RowResult(output=response)
        )
        if inspect.isawaitable(produced):
            if inspect.iscoroutine(produced):
                produced.close()
            raise TypeError("model_rows completion must return synchronously")
        return self._prepare_publication(produced)

    def render(
        self,
        row_values: dict[str, Any],
        spec: dict[str, Any],
    ) -> RenderedCall:
        if (
            self._uses_extract
            and self._params.grounding
            and self._params.grounding.enabled
        ):
            from frisket.engine.executor.extract_result_evidence import (
                extract_prompt_values,
            )

            row_values = extract_prompt_values(
                self._project,
                row_values,
                spec,
                prompt_source_columns=self.prompt_source_columns,
            )
        if self._terminal.evaluation is None:
            prompt = self._terminal.renderer(self._params, Row(row_values))
        else:
            if self._evaluation_context is None:
                raise TypeError("evaluation model_rows is missing its frozen context")
            prompt = self._terminal.renderer(
                self._params, Row(row_values), self._evaluation_context
            )
        if not isinstance(prompt, ModelPrompt):
            raise TypeError(
                "model_rows renderer returned something other than ModelPrompt"
            )
        if (
            prompt.response_schema is not None
            and self._response_model is not DynamicOutput
        ):
            raise TypeError(
                "response_schema is only supported with ModelPrompt[DynamicOutput]"
            )
        schema = (
            dict(prompt.response_schema)
            if prompt.response_schema is not None
            else dict(self._response_schema)
        )
        from jsonschema.validators import validator_for

        validator_for(schema).check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("model response schema must describe an object")
        return RenderedCall(
            messages=[dict(message) for message in prompt.messages],
            schema=schema,
            max_tokens=prompt.max_tokens,
        )


def build_typed_map_rows_plan(
    project: Any,
    bound: BoundTypedActionRequest,
    *,
    _allow_existing_outputs: bool = False,
    _admit_output_targets: bool = True,
) -> TypedMapRowsPlan:
    """Resolve a typed request into a registry-free MapRunner program."""

    request = bound.request
    evaluation_context = None
    if isinstance(bound.action.definition.run, ModelRows):
        references = validate_model_rows_project_inputs(
            project,
            action_id=bound.action.action_id,
            terminal=bound.action.definition.run,
            params=bound.params,
            sheet_id=request.scope.sheet_id,
            row_ids=request.scope.row_ids,
        )
        evaluation_context = _resolve_model_rows_evaluation_context(
            project,
            action_id=bound.action.action_id,
            terminal=bound.action.definition.run,
            params=bound.params,
            sheet_id=request.scope.sheet_id,
            row_ids=request.scope.row_ids,
            references=references,
        )
    else:
        references = validate_typed_project_references(
            project, request.scope.sheet_id, bound.params
        )
        if _model_backed_terminal(bound.action.definition.run):
            validate_model_rows_input_provenance(
                project,
                action_id=bound.action.action_id,
                sheet_id=request.scope.sheet_id,
                row_ids=request.scope.row_ids,
                source_column_ids=references.source_column_ids,
            )
    if _admit_output_targets:
        output_names, output_target_preconditions = _resolve_typed_output_targets(
            project,
            bound,
            allow_idempotent_outputs=_allow_existing_outputs,
        )
    else:
        output_names = {
            field.key: field.materialized_name(bound.request.output_names)
            for field in bound.output_fields
        }
        output_target_preconditions = {}
    unresolved = _typed_map_rows_plan(
        bound,
        output_names=output_names,
        output_target_preconditions=output_target_preconditions,
        evaluation_context=evaluation_context,
    )
    unresolved.program.bind_project(project)
    return replace(
        unresolved,
        source_column_ids=references.source_column_ids,
        source_column_types=references.source_column_types,
    )


def _resolve_typed_output_targets(
    project: Any,
    bound: BoundTypedActionRequest,
    *,
    allow_idempotent_outputs: bool,
) -> tuple[dict[str, str], dict[str, int | None]]:
    return resolve_row_output_targets(
        project,
        bound.request,
        bound.output_fields,
        allow_idempotent_outputs=allow_idempotent_outputs,
    )


def resolve_row_output_targets(
    project: Any,
    request: ActionRequest,
    output_fields: tuple[OutputField, ...],
    *,
    allow_idempotent_outputs: bool = False,
) -> tuple[dict[str, str], dict[str, int | None]]:
    """Admit typed output descriptors, regardless of where Params were bound."""

    sheet_id = request.scope.sheet_id
    columns_by_name = {
        str(column["name"]): column
        for column in project.columns(sheet_id, include_hidden=True)
    }
    names = {
        field.key: field.materialized_name(request.output_names)
        for field in output_fields
    }
    preconditions: dict[str, int | None] = {}
    for field in output_fields:
        name = names[field.key]
        column = columns_by_name.get(name)
        if column is None:
            preconditions[name] = None
            continue
        column_id = int(column["id"])
        generation_managed = (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                (column_id,),
            ).fetchone()
            is not None
        )
        if bool(column["ai_generated"]) and not generation_managed:
            raise TypedMapRowsPlanError(
                "output_column_exists",
                "only generation-managed output columns can be replaced",
                details={"columns": [name]},
            )
        if not request.replace_existing:
            if allow_idempotent_outputs or (
                bool(column["hidden"])
                and bool(column["ai_generated"])
                and str(column["type"]) == field.column_type
            ):
                preconditions[name] = None
                continue
            raise TypedMapRowsPlanError(
                "output_column_exists",
                "output columns already exist",
                details={"columns": [name]},
            )
        if not bool(column["ai_generated"]):
            raise TypedMapRowsPlanError(
                "output_column_exists",
                "only generated columns can be replaced",
                details={"columns": [name]},
            )
        descriptor_changed = (
            str(column["type"]) != field.column_type
            or column["format"] != field.format
            or column["semantic_type"] != field.semantic_type
        )
        if (
            (descriptor_changed and request.scope.row_ids is not None)
            or (descriptor_changed and bool(column["hidden"]))
            or (
                descriptor_changed
                and project.db.execute(
                    "SELECT 1 FROM cells WHERE column_id=? LIMIT 1", (column_id,)
                ).fetchone()
                is not None
            )
        ):
            raise TypedMapRowsPlanError(
                "output_column_exists",
                "replacement output has an incompatible descriptor for this scope; "
                "create a new column or replace the whole generated column",
                details={"columns": [name]},
            )
        preconditions[name] = column_id
    return names, preconditions


def bound_typed_program_request_from_runner_spec(
    runner_spec: Mapping[str, Any],
    *,
    project: Any | None = None,
) -> BoundTypedActionRequest | None:
    """Bind the canonical typed request embedded in a durable runner spec."""

    from frisket.actions.registry import ACTION_REGISTRY

    action_id = runner_spec.get("action_kind")
    if not isinstance(action_id, str):
        return None
    try:
        action = ACTION_REGISTRY.get(action_id)
    except KeyError:
        if project is None:
            return None
        from frisket.authoring.workbench.installed_actions import (
            resolve_installed_action,
        )

        resolved = resolve_installed_action(project, action_id)
        if resolved is None:
            return None
        action = resolved[0]
    if not isinstance(
        action.definition.run, (MapRows, MapBatch, ModelRows, SemanticJoin)
    ) and getattr(action.definition.run, "capabilities", ()) != (ValueClusterer,):
        return None
    row_ids = runner_spec.get("row_ids")
    request = ActionRequest.model_validate(
        {
            "action_id": action_id,
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": runner_spec.get("sheet_id"),
                "row_ids": None if row_ids == [] else row_ids,
            },
            "params": runner_spec.get("params"),
            "output_names": {
                field.key: runner_spec.get("output_names", {}).get(field.key, field.key)
                for field in action.definition.run.resolve_output_fields(
                    action.definition.run.params_model.model_validate(
                        runner_spec.get("params")
                    )
                )
                if not field.hidden
            },
            "replace_existing": runner_spec.get("replace_existing", False),
            "idempotency_key": "internal-run-backfill-program",
            **(
                {
                    "sheet_name": runner_spec.get("sheet_name"),
                    "output_names": {
                        **runner_spec.get("output_names", {}),
                        **runner_spec.get("child_output_names", {}),
                    },
                }
                if isinstance(action.definition.run, SemanticJoin)
                else {}
            ),
        }
    )
    if row_ids == []:
        # A host-computed backfill may have no remaining rows. Keep that exact
        # scope without permitting empty selections in the public request API.
        request = request.model_copy(
            update={"scope": request.scope.model_copy(update={"row_ids": ()})}
        )
    bound = BoundTypedActionRequest.bind(action, request)
    if project is not None and runner_spec.get("implementation_identity") is not None:
        from frisket.authoring.workbench.installed_actions import bind_installed_action

        installed = bind_installed_action(project, request)
        if installed is None or (
            normalized_typed_request_identity(installed).get("implementation_identity")
            != runner_spec["implementation_identity"]
        ):
            raise ValueError("stored installed action implementation changed")
        bound = installed
    return bound


def typed_program_from_runner_spec(
    project: Any,
    runner_spec: Mapping[str, Any],
) -> Recipe | None:
    """Rebuild an explicitly supported typed program from its durable spec."""

    bound = bound_typed_program_request_from_runner_spec(runner_spec, project=project)
    if bound is None:
        return None
    if getattr(bound.action.definition.run, "capabilities", ()) == (ValueClusterer,):
        from frisket.engine.executor.cluster_action import cluster_plan_from_state
        from frisket.engine.executor.cluster_program import validate_cluster_source

        state = runner_spec["cluster_values"]
        validate_cluster_source(project, state["source"])
        return cluster_plan_from_state(bound, state).program
    plan = build_typed_map_rows_plan(
        project,
        bound,
        _allow_existing_outputs=True,
    )
    expected = plan.spec_dict()
    if isinstance(plan.action.definition.run, SemanticJoin):
        expected["engine"] = runner_spec.get("semantic_join", {}).get("embedding_model")
    if (
        isinstance(plan.action.definition.run, ModelRows)
        and plan.action.definition.run.evaluation is not None
        and isinstance(runner_spec, dict)
    ):
        # Backfill changes the retry scope, so its subject provenance is resolved
        # for that scope instead of inheriting the original run's frozen snapshot.
        runner_spec["evaluation_context"] = expected.get("evaluation_context")
    for key in (
        "action_kind",
        "action_version",
        "sheet_id",
        "input_columns",
        "params",
        "output_names",
        "replace_existing",
        "model",
        "engine",
        "input_template",
        "output_target_preconditions",
        "overwrite",
        "evaluation_context",
        "implementation_identity",
    ):
        if runner_spec.get(key) != expected.get(key):
            raise ValueError(f"stored typed runner spec has inconsistent {key}")
    return plan.program


def validate_model_rows_source_types(
    terminal: ModelRows[Any, Any],
    params: BaseModel,
    source_column_types: Mapping[str, str],
) -> None:
    references = discover_references(getattr(params, terminal.source_param))
    incompatible = [
        {
            "name": reference.column,
            "actual_type": source_column_types[reference.column],
            "accepted_column_types": list(reference.accepted_column_types),
        }
        for reference in references
        if reference.accepted_column_types is not None
        and source_column_types[reference.column] not in reference.accepted_column_types
    ]
    if incompatible:
        raise TypedMapRowsPlanError(
            "invalid_input_ref",
            "referenced input columns have incompatible types",
            details={"columns": incompatible},
        )


def validate_model_rows_input_provenance(
    project: Any,
    *,
    action_id: str,
    sheet_id: int,
    row_ids: tuple[int, ...] | None,
    source_column_ids: Mapping[str, int],
) -> None:
    """Refuse scopes the current column-level receipt cannot represent."""

    selected_row_ids = (
        project.visible_row_ids(sheet_id) if row_ids is None else list(row_ids)
    )
    for name, column_id in source_column_ids.items():
        column = project.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        if column is None:
            continue
        try:
            _input_source_run_id(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=selected_row_ids,
                legacy_current_run_id=column["current_run_id"],
            )
        except MixedOriginInputProvenanceUnsupported as exc:
            raise TypedMapRowsPlanError(
                "invalid_input_ref",
                f"{action_id} cannot represent mixed-origin input column provenance",
                details=exc.action_error_details(column=name),
            ) from exc


_UPSTREAM_PROMPT_OMIT = frozenset(
    {
        "sheet_id",
        "row_ids",
        "confirmed",
        "output_name",
        "model",
        "evaluation_context",
        "guidelines",
        "include_original_prompt",
        "judged_column",
        "original_prompt",
        "original_prompt_source_run_id",
        "_judged_source_run_id",
    }
)


def _format_upstream_prompt(params_json: Any) -> str | None:
    try:
        stored = json.loads(params_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    # Typed ModelRows runs persist the host execution spec, with author-owned
    # parameters nested under ``params``. Never expose that envelope (model,
    # action/version, IO plumbing, overwrite flags, or evaluation context) to
    # a downstream judge. Legacy producers persist their author params flat,
    # so retain the historical formatting for those records.
    nested = stored.get("params")
    if isinstance(nested, dict) and (
        "action_kind" in stored or "action_version" in stored
    ):
        stored = nested
    payload = {
        key: value
        for key, value in stored.items()
        if key not in _UPSTREAM_PROMPT_OMIT and value not in (None, "", [], {})
    }
    return json.dumps(payload, indent=2, sort_keys=True) if payload else None


def _strict_evaluation_subject_context(
    project: Any,
    *,
    subject_column_id: int,
    sheet_id: int,
    selected_row_ids: list[int],
) -> tuple[int, tuple[Mapping[str, Any], ...], str | None]:
    """Resolve one exact generated head per judged cell, with no fallback."""

    generations = ResultGenerationStore(project)
    ordered_row_ids = list(dict.fromkeys(int(row_id) for row_id in selected_row_ids))
    heads = (
        generations.read_cell_heads(subject_column_id, row_ids=ordered_row_ids)
        if generations.is_generation_managed(subject_column_id)
        else {}
    )
    origin_run_ids = sorted({head.run_id for head in heads.values()}, reverse=True)[:2]
    missing_head_row_ids = [row_id for row_id in ordered_row_ids if row_id not in heads]
    _values, live_refs = project.get_values_with_refs(
        sheet_id,
        subject_column_id,
        row_ids=ordered_row_ids,
        apply_edits=True,
    )
    manual_edit_row_ids = [
        row_id
        for row_id in ordered_row_ids
        if live_refs.get(row_id, {}).get("kind") == "manual_edit"
    ]
    if (
        not ordered_row_ids
        or missing_head_row_ids
        or len(origin_run_ids) != 1
        or manual_edit_row_ids
    ):
        raise MixedOriginInputProvenanceUnsupported(
            subject_column_id,
            origin_run_ids,
            missing_head_row_ids=missing_head_row_ids,
            divergent_edit_row_ids=manual_edit_row_ids,
        )
    source_run_id = origin_run_ids[0]
    value_refs = tuple(
        {
            "kind": "run_result",
            "op_id": heads[row_id].op_id,
            "row_id": row_id,
            "column_id": subject_column_id,
            "run_id": heads[row_id].run_id,
        }
        for row_id in ordered_row_ids
    )
    from frisket.engine.store.receipts import ReceiptStore

    return (
        source_run_id,
        value_refs,
        ReceiptStore(project).latest_id_for_run(source_run_id),
    )


def _resolve_model_rows_evaluation_context(
    project: Any,
    *,
    action_id: str,
    terminal: ModelRows[Any, Any],
    params: BaseModel,
    sheet_id: int,
    row_ids: tuple[int, ...] | None,
    references: TypedProjectReferences,
) -> ModelRowsEvaluationContext | None:
    profile = terminal.evaluation
    if profile is None:
        return None
    subject_column = getattr(params, profile.subject_param).name
    subject_column_id = references.source_column_ids[subject_column]
    selected_row_ids = (
        project.visible_row_ids(sheet_id) if row_ids is None else list(row_ids)
    )
    try:
        source_run_id, subject_value_refs, source_receipt_id = (
            _strict_evaluation_subject_context(
                project,
                subject_column_id=subject_column_id,
                sheet_id=sheet_id,
                selected_row_ids=selected_row_ids,
            )
        )
    except MixedOriginInputProvenanceUnsupported as exc:
        if not exc.origin_run_ids and not exc.divergent_edit_row_ids:
            raise TypedMapRowsPlanError(
                "invalid_input_ref",
                "the judged column has no generated provenance for the selected rows",
                field="params.judged_column",
                details={"columns": [subject_column]},
            ) from exc
        raise TypedMapRowsPlanError(
            "invalid_input_ref",
            f"{action_id} cannot represent judged column provenance",
            field="params.judged_column",
            details=exc.action_error_details(column=subject_column),
        ) from exc
    original_prompt = None
    if getattr(params, profile.upstream_prompt_param):
        run = project.db.execute(
            "SELECT params FROM runs WHERE id=?", (source_run_id,)
        ).fetchone()
        original_prompt = _format_upstream_prompt(
            run["params"] if run is not None else None
        )
    return ModelRowsEvaluationContext(
        subject_column=subject_column,
        subject_column_id=subject_column_id,
        source_run_id=source_run_id,
        original_prompt=original_prompt,
        subject_row_ids=tuple(selected_row_ids),
        subject_value_refs=subject_value_refs,
        source_receipt_id=source_receipt_id,
    )


def validate_model_rows_project_inputs(
    project: Any,
    *,
    action_id: str,
    terminal: ModelRows[Any, Any],
    params: BaseModel,
    sheet_id: int,
    row_ids: tuple[int, ...] | None,
) -> TypedProjectReferences:
    """Validate one model-backed map source against live project facts."""

    terminal.validate_source(params)
    try:
        references = validate_typed_project_references(project, sheet_id, params)
    except TypedMapRowsPlanError as exc:
        if exc.code != "column_not_ai_generated" or terminal.evaluation is None:
            raise
        columns = exc.details.get("columns")
        subject = (
            str(columns[0])
            if isinstance(columns, list) and len(columns) == 1
            else getattr(params, terminal.evaluation.subject_param).name
        )
        raise TypedMapRowsPlanError(
            "column_not_ai_generated",
            f"{action_id} answer to grade must be an AI-generated column",
            field="params.judged_column",
            details={"column": subject},
        ) from exc
    validate_model_rows_source_types(terminal, params, references.source_column_types)
    validate_model_rows_input_provenance(
        project,
        action_id=action_id,
        sheet_id=sheet_id,
        row_ids=row_ids,
        source_column_ids={
            name: column_id
            for name, column_id in references.source_column_ids.items()
            if terminal.evaluation is None
            or name != getattr(params, terminal.evaluation.subject_param).name
        },
    )
    return references


def validate_typed_project_references(
    project: Any,
    sheet_id: int,
    params: BaseModel,
) -> TypedProjectReferences:
    """Validate typed Params references against one sheet's live columns."""

    return validate_row_project_references(
        project, sheet_id, discover_references(params)
    )


def validate_row_project_references(
    project: Any,
    sheet_id: int,
    declared_references: tuple[InputReference, ...],
) -> TypedProjectReferences:
    """Resolve discovered semantic references using the host's project facts."""

    visible_columns = project.columns(sheet_id)
    columns_by_name = {
        str(column["name"]): column
        for column in project.columns(sheet_id, include_hidden=True)
    }
    visible_column_names = frozenset(str(column["name"]) for column in visible_columns)
    source_columns = tuple(reference.column for reference in declared_references)
    # Truly hidden columns are write-only implementation plumbing. They may be
    # used for ids and types, but are not admissible action inputs.
    missing = sorted(set(source_columns) - visible_column_names)
    if missing:
        raise TypedMapRowsPlanError(
            "invalid_input_ref",
            "referenced input columns do not exist",
            details={"columns": missing},
        )
    incompatible = [
        {
            "name": reference.column,
            "actual_type": str(columns_by_name[reference.column]["type"]),
            "accepted_column_types": list(reference.accepted_column_types),
        }
        for reference in declared_references
        if reference.accepted_column_types is not None
        and reference.column in columns_by_name
        and str(columns_by_name[reference.column]["type"])
        not in reference.accepted_column_types
    ]
    if incompatible:
        from frisket.actions.model_rows import FILE_SOURCE_CONVERSION_HINT

        raise TypedMapRowsPlanError(
            "invalid_input_ref",
            FILE_SOURCE_CONVERSION_HINT
            if any(
                column["actual_type"] == "file"
                and "text" in column["accepted_column_types"]
                for column in incompatible
            )
            else "referenced input columns have incompatible types",
            details={"columns": incompatible},
        )
    not_generated = [
        reference.column
        for reference in declared_references
        if reference.ai_generated_only
        and reference.column in columns_by_name
        and not bool(columns_by_name[reference.column]["ai_generated"])
    ]
    if not_generated:
        raise TypedMapRowsPlanError(
            "column_not_ai_generated",
            "the selected column must be AI-generated",
            field="params.judged_column",
            details={"columns": not_generated},
        )
    return TypedProjectReferences(
        source_columns=source_columns,
        source_column_ids=MappingProxyType(
            {name: int(columns_by_name[name]["id"]) for name in source_columns}
        ),
        source_column_types=MappingProxyType(
            {name: str(columns_by_name[name]["type"]) for name in source_columns}
        ),
        visible_column_names=visible_column_names,
    )


def materialized_row_output_fields(
    output_fields: tuple[OutputField, ...], names: Mapping[str, str]
) -> tuple[dict[str, Any], ...]:
    """Project admitted logical outputs onto the runner's publication descriptors."""
    return tuple(
        {
            "name": names[field.key],
            "column_type": field.column_type,
            "schema": dict(field.schema),
            "description": "",
            "publication_required": True,
            "hidden": field.hidden,
            **({"default_hidden": True} if field.default_hidden else {}),
            **({"format": field.format} if field.format is not None else {}),
            **({"semantic_type": field.semantic_type} if field.semantic_type else {}),
        }
        for field in output_fields
    )


def _typed_map_rows_plan(
    bound: BoundTypedActionRequest,
    *,
    output_names: Mapping[str, str] | None = None,
    output_target_preconditions: Mapping[str, int | None] | None = None,
    evaluation_context: ModelRowsEvaluationContext | None = None,
) -> TypedMapRowsPlan:
    """Build the project-independent half of a typed MapRunner invocation.

    Queue workers reconstruct this object from the canonical ``ActionRequest``
    stored in the job. Project-owned source ids and types are deliberately
    filled only by :func:`build_typed_map_rows_plan` at admission.
    """

    action, request = bound.action, bound.request
    terminal_policy = _MapTerminalPolicy.for_terminal(action.definition.run)
    params, output_fields = bound.params, bound.output_fields
    request_identity = normalized_typed_request_identity(bound)
    source_columns = tuple(
        reference.column for reference in discover_references(params)
    )
    final_names = dict(
        output_names
        or {
            field.key: field.materialized_name(request.output_names)
            for field in output_fields
        }
    )
    target_preconditions = dict(output_target_preconditions or {})
    fields = materialized_row_output_fields(output_fields, final_names)
    spec: dict[str, Any] = {
        "action_kind": action.action_id,
        "action_version": "1",
        "sheet_id": request.scope.sheet_id,
        "input_columns": list(source_columns),
        "params": request_identity["params"],
        "output_names": final_names,
    }
    if bound.implementation_identity is not None:
        spec["implementation_identity"] = dict(bound.implementation_identity)
    if isinstance(action.definition.run, SemanticJoin):
        spec["sheet_name"] = request.sheet_name
        spec["child_output_names"] = {
            key: name
            for key, name in request.output_names.items()
            if key in action.definition.run.child_output_keys(params)
        }
        if request.confirmation is not None:
            spec["consented_promise_set_hash"] = request.confirmation
    if TopicSectionsReader in getattr(action.definition.run, "capabilities", ()):
        spec["topic_analysis_sidecars"] = [
            field.key for field in output_fields if field.hidden and field.route is None
        ]
    if target_preconditions:
        spec["output_target_preconditions"] = target_preconditions
    spec["replace_existing"] = request.replace_existing
    if request.replace_existing:
        spec["overwrite"] = True
    if isinstance(action.definition.run, (ModelRows, _DirectModelRows)):
        terminal = action.definition.run
        source = getattr(params, terminal.source_param)
        if isinstance(terminal, ModelRows):
            spec["model"] = getattr(params, terminal.model_param).root
        if isinstance(source, Template):
            spec["input_template"] = source.text
        if evaluation_context is not None:
            spec["evaluation_context"] = evaluation_context.payload()
    agent_capabilities = any(
        cap in getattr(action.definition.run, "capabilities", ())
        for cap in (Researcher, McpExtractor)
    )
    if agent_capabilities:
        models = [
            getattr(params, name)
            for name in type(params).model_fields
            if isinstance(getattr(params, name), ModelRef)
        ]
        if len(models) != 1:
            raise TypeError(
                "model-backed row capabilities require exactly one ModelRef"
            )
        spec["model"] = models[0].root
    capability = routed_capability(action.definition.run)
    if capability is not None:
        engine = _capability_engine(action.definition.run, params, capability)
        if engine is not None:
            spec["engine"] = engine
        spec.update(
            resolved_engine_options(bound.action.definition.run, bound.params) or {}
        )
    if terminal_policy.model_backed or terminal_policy.external_confirmation:
        if request.confirmation is not None:
            spec["consented_promise_set_hash"] = request.confirmation
    if request.scope.row_ids is not None:
        spec["row_ids"] = list(request.scope.row_ids)

    frozen_names = MappingProxyType(final_names)
    program_type = (
        _TypedMapBatchProgram
        if isinstance(action.definition.run, MapBatch)
        else _TypedModelRowsProgram
        if isinstance(action.definition.run, ModelRows)
        else _TypedAgentRowsProgram
        if agent_capabilities
        else _TypedMapRowsProgram
    )
    if isinstance(action.definition.run, SemanticJoin):
        from frisket.engine.executor.semantic_join_program import _SemanticJoinProgram

        program_type = _SemanticJoinProgram
    program = program_type(
        action,
        params,
        frozen_names,
        output_fields,
        fields,
        evaluation_context,
        bound.runtime_binding,
    )
    return TypedMapRowsPlan(
        action=action,
        request=request,
        source_columns=source_columns,
        source_column_ids=MappingProxyType({}),
        source_column_types=MappingProxyType({}),
        output_names=frozen_names,
        output_target_preconditions=MappingProxyType(target_preconditions),
        output_fields=fields,
        request_identity=MappingProxyType(request_identity),
        evaluation_context=evaluation_context,
        spec=MappingProxyType(spec),
        program=program,
    )


@dataclass(frozen=True)
class _TypedExecutionEnvelope:
    """Only the identity the durable executor needs from a typed request."""

    kind: str
    idempotency_key: str
    params: Mapping[str, Any]
    row_scope: None = None


def normalized_typed_request_identity(
    bound: BoundTypedActionRequest,
) -> dict[str, Any]:
    """Return normalized request values without turning omissions into input."""

    action, request = bound.action, bound.request
    output_names = (
        {
            logical: final
            for logical, final in request.output_names.items()
            if logical != final
        }
        if bound.output_fields is None
        else {
            field.key: request.output_names.get(field.key, field.key)
            for field in bound.output_fields
            if not field.hidden or isinstance(action.definition.run, CreateSheet)
        }
    )
    if isinstance(action.definition.run, SemanticJoin):
        # Child-only names affect the same atomic publication and identity.
        output_names.update(
            {
                key: request.output_names.get(key, key)
                for key in action.definition.run.child_output_keys(bound.params)
            }
        )
    payload = {
        "action_id": action.action_id,
        "scope": request.scope.model_dump(mode="json"),
        # Rebinding must preserve supplied-only option semantics, recursively.
        "params": bound.params.model_dump(
            mode="json", by_alias=True, exclude_unset=True
        ),
        # Omitted identity mappings and explicit defaults execute identically.
        "output_names": output_names,
        "replace_existing": request.replace_existing,
    }
    if bound.implementation_identity is not None:
        payload["implementation_identity"] = bound.implementation_identity
    if request.sheet_name is not None:
        payload["sheet_name"] = request.sheet_name
    # Round-tripping once gives receipts only stable JSON values and fails
    # closed if an action Params serializer emits NaN or a non-JSON object.
    return json.loads(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def typed_receipt_request_identity(
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove semantic Params from durable public request evidence."""

    receipt_identity = dict(identity)
    receipt_identity.pop("params", None)
    return receipt_identity


def typed_request_hash(bound: BoundTypedActionRequest) -> str:
    """Hash normalized execution identity, not incidental request spelling."""

    identity = normalized_typed_request_identity(bound)
    # Ordinary defaults are equivalent spellings. Capability option projection
    # additionally captures omission-sensitive meanings (e.g. default diarization)
    # without adding metadata to the executable request or its receipt.
    identity["params"] = bound.params.model_dump(mode="json", by_alias=True)
    options = resolved_engine_options(bound.action.definition.run, bound.params)
    if options is not None:
        identity["engine_options"] = options
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _typed_replay_error(
    project: Any,
    bound: BoundTypedActionRequest,
) -> Callable[[Receipt], ActionError | None]:
    action, request = bound.action, bound.request

    def replay_error(receipt: Receipt) -> ActionError | None:
        if isinstance(action.definition.run, SemanticJoin):
            from frisket.engine.executor.semantic_join_action import _replay_error

            return _replay_error(project, receipt)
        if getattr(action.definition.run, "capabilities", ()) == (ValueClusterer,):
            from frisket.engine.executor.cluster_action import _replay_error

            return _replay_error(project, receipt)
        if receipt.status == "queued":
            return None
        if _model_backed_terminal(action.definition.run):
            terminal = action.definition.run
            evaluation = getattr(terminal, "evaluation", None)
            roles = {
                field.materialized_name(request.output_names): (
                    "judge_verdict"
                    if evaluation is not None and field.key == "verdict"
                    else "judge_note"
                    if evaluation is not None and field.key == "judge_note"
                    else field.key
                )
                for field in bound.output_fields
            }
            if any(
                output.ref.get("role") != roles.get(output.ref.get("name"))
                for output in receipt.outputs
                if output.ref.get("kind") == "map_result_column"
            ):
                return ActionError(
                    code="stale_replay",
                    message="A recorded output role changed.",
                    action_kind=action.action_id,
                )
        try:
            from frisket.engine.executor.row_file_stage import verify_receipt_row_files

            verify_receipt_row_files(project, receipt)
        except (LookupError, TypeError, ValueError, OSError):
            return ActionError(
                code="stale_replay",
                message="A recorded file or its publication evidence is missing or changed.",
                action_kind=action.action_id,
            )
        if receipt.status in {"cancelled", "failed"} and not (
            any(item.ref.get("kind") == "map_result_column" for item in receipt.outputs)
            or any(
                item.ref.get("kind") == "typed_hidden_output"
                for item in receipt.evidence
            )
        ):
            # Queue termination can precede all publication. There is no published
            # output to revalidate, but any published refs still take the normal
            # generation/value checks below, including on unsuccessful runs.
            return None
        return output_columns_replay_error(
            project,
            receipt,
            output_kind="map_result_column",
            action_kind=action.action_id,
            compare_declared_format=True,
            hidden_output_kind="typed_hidden_output",
            scope=(
                "receipt"
                if receipt.status in {"partial", "cancelled", "failed"}
                else "expected"
            ),
            allow_empty_rows=receipt.status == "cancelled",
            expected_row_ids=replay_expected_row_ids(
                project,
                sheet_id=request.scope.sheet_id,
                requested_row_ids=(
                    list(request.scope.row_ids)
                    if request.scope.row_ids is not None
                    else None
                ),
            ),
        )

    return replay_error


def typed_map_rows_replay_result(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
) -> ActionResult | None:
    """Replay an existing typed receipt before consulting mutable project facts."""

    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    if existing is None:
        return None
    action = _TypedExecutionEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=MappingProxyType(dict(bound.request.params)),
    )
    result = _reserved_receipt_result_from_existing(
        project,
        existing,
        params_hash=typed_request_hash(bound),
        project_id=project_id,
        action=action,
        replay_error_fn=_typed_replay_error(project, bound),
    )
    if existing["status"] == "queued" and result.status == "queued":
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        job_id = _queued_receipt_job_id(receipt)
        if job_id is None:
            # The run was prepared but enqueue never finished. Let queue
            # admission recover it and revalidate its frozen source bindings.
            return None
        return result.model_copy(update={"job_id": job_id})
    if result.receipt_id == existing["id"]:
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        job_id = _queued_receipt_job_id(receipt)
        if job_id is not None:
            return result.model_copy(update={"job_id": job_id})
    return result


def _typed_plan_error(action_id: str, exc: Exception) -> ActionError:
    from frisket.authoring.workbench.plugin_runtime_shared import (
        WorkbenchPluginActivationError,
    )

    if isinstance(exc, (TypedMapRowsPlanError, WorkbenchPluginActivationError)):
        return ActionError(
            code=exc.code,
            message=str(exc),
            action_kind=action_id,
            field=exc.field,
            details=dict(exc.details),
        )
    return ActionError(
        code="invalid_params",
        message="action parameters are invalid",
        action_kind=action_id,
        field="params",
        details={
            "errors": (
                exc.errors(include_url=False, include_context=False)
                if isinstance(exc, ValidationError)
                else [{"message": str(exc)}]
            )
        },
    )


def _typed_model_cost_error(action: Any, exc: Any) -> ActionError:
    return _model_cost_requires_confirmation_error(action, exc).model_copy(
        update={"field": "confirmation"}
    )


def _typed_external_cost_error(action: Any, exc: Any) -> ActionError:
    return _external_cost_requires_confirmation_error(action, exc).model_copy(
        update={"field": "confirmation"}
    )


def _typed_receipt(
    project: Any,
    run_id: int,
    action_id: str,
    receipt_id: str,
    *,
    project_id: str,
    plan: TypedMapRowsPlan,
    params_hash: str,
) -> Receipt:
    input_ids = dict(plan.source_column_ids)
    terminal_policy = _MapTerminalPolicy.for_terminal(plan.action.definition.run)
    model_backed = terminal_policy.model_backed
    external = terminal_policy.external_failure
    evaluation = getattr(plan.action.definition.run, "evaluation", None)
    context = plan.evaluation_context
    captured_input_ids = input_ids
    captured_input_types = dict(plan.source_column_types)
    if evaluation is not None:
        if context is None:
            raise RuntimeError("judge receipt is missing its evaluation context")
        captured_input_ids = {
            name: column_id
            for name, column_id in input_ids.items()
            if name != context.subject_column
        }
        captured_input_types = {
            name: column_type
            for name, column_type in plan.source_column_types.items()
            if name != context.subject_column
        }
    facts = capture_maprunner_facts(
        project,
        runner_spec=plan.spec_dict(),
        output_fields=[dict(field) for field in plan.output_fields],
        run_id=run_id,
        params_hash=params_hash,
        input_column_ids=captured_input_ids,
        input_column_types=captured_input_types,
        rich_input_columns=model_backed,
    )
    if facts.missing_outputs:
        raise RuntimeError(
            f"typed map_rows outputs disappeared: {facts.missing_outputs}"
        )
    if facts.run_status == "cancelled":
        status = "cancelled"
    elif evaluation is not None and facts.run_status == "failed":
        status = "failed"
    else:
        status = model_run_status(facts)
        if status == "failed" and evaluation is None:
            from frisket.sdk.media import media_successful_result_row_ids

            # A row may publish successful outputs alongside an Outcome failure.
            # Terminal-row counts alone cannot distinguish that from total failure.
            if any(
                media_successful_result_row_ids(
                    project,
                    run_id=run_id,
                    column_id=output.column_id,
                    row_ids=facts.row_ids,
                )
                for output in facts.output_facts
            ):
                status = "partial"
    errors = row_failure_errors(
        plan.action.action_id,
        facts,
        status,
        code=terminal_policy.map_error_code,
    )
    from frisket.sdk.media import media_halt_error

    halt_error = media_halt_error(plan.action.action_id, facts.run)
    if halt_error is not None:
        status = "failed"
        errors = [halt_error]
    if model_backed:
        rich_by_name = {item["name"]: item for item in facts.input_columns_rich}
        inputs = []
        for name in plan.source_columns:
            is_subject = (
                evaluation is not None
                and plan.evaluation_context is not None
                and name == plan.evaluation_context.subject_column
            )
            if is_subject:
                context = plan.evaluation_context
                assert context is not None
                rich = {
                    "ai_generated": True,
                    "source_run_id": context.source_run_id,
                    "source_receipt_id": context.source_receipt_id,
                }
                row_ids = list(context.subject_row_ids)
            else:
                rich = rich_by_name[name]
                row_ids = facts.row_ids
            ref = {
                "kind": "model_rows_input_column",
                "name": name,
                "sheet_id": facts.sheet_id,
                "column_id": input_ids[name],
                "type": plan.source_column_types[name],
                "row_ids": row_ids,
                "ai_generated": rich["ai_generated"],
                "source_run_id": rich["source_run_id"],
                "source_receipt_id": rich["source_receipt_id"],
                **(
                    {"role": "judged_output" if is_subject else "source"}
                    if evaluation is not None
                    else {}
                ),
            }
            if is_subject:
                ref["included_original_prompt"] = bool(
                    plan.evaluation_context.original_prompt
                )
                ref["value_refs"] = [
                    dict(value_ref)
                    for value_ref in plan.evaluation_context.subject_value_refs
                ]
            inputs.append(ReceiptIO(name=f"column.{name}", kind="column", ref=ref))
        from frisket.engine.executor.action_support import _model_call_provider_use

        provider_use = _model_call_provider_use(
            facts.model_calls, model=facts.model, run=facts.run
        )
    else:
        inputs = [
            ReceiptIO(
                name=f"column.{name}",
                kind="column",
                ref={
                    "kind": "source_column",
                    "name": name,
                    "sheet_id": facts.sheet_id,
                    "column_id": input_ids[name],
                    "type": facts.input_column_types[name],
                    "row_ids": facts.row_ids,
                },
            )
            for name in plan.source_columns
        ]
        provider_use = [
            {
                "provider": "local",
                "service": f"frisket.{plan.action.action_id}",
                "external_api": False,
                "cost_actual": facts.cost_actual,
            }
        ]
    capability = routed_capability(plan.action.definition.run)
    if capability is not None or isinstance(plan.action.definition.run, SemanticJoin):
        from frisket.engine.executor.action_support import _routed_call_provider_use

        provider_use = _routed_call_provider_use(
            facts.model_calls,
            capability=ROUTED_CAPABILITIES[capability]
            if capability is not None
            else "llm.embed",
        )
    if HttpRequester in getattr(plan.action.definition.run, "capabilities", ()):
        if capability is None:
            provider_use = []
        provider_use.append(
            {
                "provider": "external_http",
                "service": "http.request",
                "external_api": True,
                "cost_actual": 0.0,
            }
        )
    output_refs = maprunner_output_refs(facts, output_kind="map_result_column")
    from frisket.engine.executor.python_routes import project_routed_outputs

    output_refs, named_result_refs, route_evidence = project_routed_outputs(
        plan, facts, output_refs, project=project
    )
    if PdfTablesReader in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.executor.pdf_tables_receipts import (
            pdf_table_receipt_projection,
        )

        pdf_named, pdf_evidence, pdf_errors = pdf_table_receipt_projection(
            project, plan, facts, output_refs, receipt_id
        )
        named_result_refs.extend(pdf_named)
        route_evidence.extend(pdf_evidence)
        if pdf_errors and status != "cancelled":
            status, errors = "failed", pdf_errors
    if TopicSectionsReader in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.executor.topic_sections_read import topic_receipt_evidence

        hidden_names = {
            field.key
            for field in plan.program._resolved_output_fields
            if field.hidden and field.route is None
        }
        hidden_refs = [ref for ref in output_refs if ref["name"] in hidden_names]
        output_refs = [ref for ref in output_refs if ref["name"] not in hidden_names]
        route_evidence.extend(
            ReceiptEvidence(ref={**ref, "kind": "typed_hidden_output"})
            for ref in hidden_refs
        )
        route_evidence.extend(
            topic_receipt_evidence(project, facts, output_refs, hidden_refs)
        )
        for ref in output_refs:
            ref.update(
                source_columns=list(plan.source_columns), analysis=plan.action.action_id
            )
    if VisualCutsReader in getattr(plan.action.definition.run, "capabilities", ()):
        for ref in output_refs:
            ref.update(
                source_columns=list(plan.source_columns), analysis=plan.action.action_id
            )
    if model_backed:
        logical_by_final = {
            final: logical for logical, final in plan.output_names.items()
        }
        schemas = {
            field.key: dict(field.schema)
            for field in plan.program._resolved_output_fields
        }
        for ref in output_refs:
            logical = logical_by_final[ref["name"]]
            ref["role"] = (
                "judge_verdict"
                if evaluation is not None and logical == "verdict"
                else "judge_note"
                if evaluation is not None and logical == "judge_note"
                else logical
            )
            ref["schema"] = schemas[logical]
            if evaluation is not None and plan.evaluation_context is not None:
                ref["judged_column"] = plan.evaluation_context.subject_column
    request_evidence = (
        typed_receipt_request_identity(plan.request_identity)
        if model_backed
        else dict(plan.request_identity)
    )
    evidence = [
        ReceiptEvidence(
            ref={
                "kind": "typed_action_request",
                **request_evidence,
                "params_hash": params_hash,
                "op_id": facts.op_id,
                "run_id": facts.run_id,
            }
        ),
        ReceiptEvidence(
            ref={
                "kind": "map_rows_run_counts",
                "total_rows": facts.total_rows,
                "completed_rows": facts.completed_rows,
                "failed_rows": facts.failed_rows,
                "failed_row_ids": facts.failed_row_ids,
                "result_count": len(facts.row_ids),
                "model_call_count": len(facts.model_call_ids),
                "cost_actual": facts.cost_actual,
                **(
                    {"prompt_hash": facts.prompt_hash}
                    if model_backed and facts.prompt_hash is not None
                    else {}
                ),
                "op_id": facts.op_id,
                "run_id": facts.run_id,
            }
        ),
    ]
    evidence.extend(route_evidence)
    if Translator in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        if observed is not None:
            evidence.extend(
                item
                for item in observed.evidence
                if item.ref.get("kind") == "map_translate_artifact"
                and item.ref.get("run_id") == run_id
            )
    if getattr(plan.program, "_uses_extract", False):
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        if observed is not None:
            evidence.extend(
                item
                for item in observed.evidence
                if item.ref.get("kind") == "map_extract_grounding_links"
                and item.ref.get("run_id") == run_id
            )
    if any(
        cap in getattr(plan.action.definition.run, "capabilities", ())
        for cap in (Transcriber, OcrReader)
    ):
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        if observed is not None:
            evidence.extend(
                item
                for item in observed.evidence
                if item.ref.get("kind")
                in {
                    "media_transcribe_temporal_evidence_link",
                    "media_ocr_grounding_evidence_link",
                }
            )
    if DocumentConverter in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.store.receipts import ReceiptStore
        from frisket.contracts.actions.schemas._engines import (
            TO_MARKDOWN_ENGINE_TABLE,
            find_engine,
        )
        from frisket.ai.models.metadata import model_calls_cost_actual

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        recorded = (
            [
                item
                for item in observed.evidence
                if item.ref.get("kind")
                in {"document_convert_read", "document_source_provenance"}
                and item.ref.get("run_id") == run_id
            ]
            if observed is not None
            else []
        )
        evidence.extend(recorded)
        grouped = {}
        for item in recorded:
            if item.ref["kind"] == "document_convert_read":
                engine = item.ref["engine"]
                grouped[engine] = grouped.get(engine, 0) + 1
        # Keep accepted external calls even when no document result returned.
        document_calls = [
            call
            for call in facts.model_calls
            if call["capability"] == ROUTED_CAPABILITIES[DocumentConverter]
        ]
        for call in document_calls:
            grouped.setdefault(call["engine"], 0)
        provider_use = [
            item
            for item in provider_use
            if item.get("service") != ROUTED_CAPABILITIES[DocumentConverter]
        ]
        for engine, count in grouped.items():
            declaration = find_engine(TO_MARKDOWN_ENGINE_TABLE, engine)
            calls = [call for call in document_calls if call["engine"] == engine]
            provider_use.append(
                {
                    "provider": calls[0]["provider"] if calls else declaration.provider,
                    "model": engine,
                    "engine": engine,
                    "service": "document.convert",
                    "external_api": declaration.tier == "hosted",
                    "operation_call_count": count,
                    "model_call_count": len(calls),
                    "cost_actual": model_calls_cost_actual(calls),
                }
            )
    if PythonEvaluator in getattr(plan.action.definition.run, "capabilities", ()):
        if not external:
            provider_use = []
        provider_use.extend(
            [
                {
                    "provider": "local",
                    "service": "frisket.sandbox",
                    "model": "python",
                    "external_api": False,
                    "model_call_count": 0,
                    "cost_actual": 0.0,
                }
            ]
        )
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        if observed is not None:
            evidence.extend(
                item
                for item in observed.evidence
                if item.ref.get("kind") == "map_python_code"
                and item.ref.get("run_id") == run_id
            )
    if any(
        cap in getattr(plan.action.definition.run, "capabilities", ())
        for cap in _FILE_CAPABILITIES
    ):
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        recorded = (
            [
                item
                for item in observed.evidence
                if item.ref.get("kind") in {"row_file_output", "row_file_call"}
                and item.ref.get("run_id") == run_id
            ]
            if observed is not None
            else []
        )
        evidence.extend(recorded)
        provider_use = [
            item
            for item in provider_use
            if not (
                item.get("provider") == "local"
                and item.get("service") == f"frisket.{plan.action.action_id}"
            )
        ]
        calls = [
            item.ref for item in recorded if item.ref.get("kind") == "row_file_call"
        ]
        grouped = {}
        for call in calls:
            key = (
                call.get("provider", "local"),
                call.get("service", "file_acquisition"),
                bool(call.get("external_api", False)),
            )
            grouped[key] = grouped.get(key, 0) + 1
        provider_use.extend(
            {
                "provider": provider,
                "service": service,
                "external_api": remote,
                "operation_call_count": count,
                "cost_actual": 0.0,
            }
            for (provider, service, remote), count in grouped.items()
        )
    if WebSearcher in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.executor.web_search_read import search_provider_use
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        recorded = (
            [
                item
                for item in observed.evidence
                if item.ref.get("kind") == "web_search_call"
                and item.ref.get("run_id") == run_id
            ]
            if observed is not None
            else []
        )
        evidence.extend(recorded)
        provider_use = [
            item
            for item in provider_use
            if not (
                item.get("provider") == "local"
                and item.get("service") == f"frisket.{plan.action.action_id}"
            )
        ]
        provider_use.extend(search_provider_use(recorded, facts))
    if OpenCorporates in getattr(plan.action.definition.run, "capabilities", ()):
        from frisket.engine.store.receipts import ReceiptStore

        observed = ReceiptStore(project).parsed_by_id(receipt_id)
        recorded = (
            [
                item
                for item in observed.evidence
                if item.ref.get("kind") == "opencorporates_request"
                and item.ref.get("run_id") == run_id
            ]
            if observed is not None
            else []
        )
        evidence.extend(recorded)
        provider_use = (
            [
                {
                    "provider": "opencorporates.com",
                    "plugin_id": plan.program._runtime_binding.plugin,
                    "action_kind": plan.action.action_id,
                    "external_api": True,
                    "cost_source": "external_metered_declared",
                    "cost_actual": None,
                    "effect_count": len(recorded),
                    "effect_count_source": "host_http_response",
                    "affected_row_count": len(
                        {item.ref["row_id"] for item in recorded}
                    ),
                }
            ]
            if recorded
            else []
        )
    if model_backed:
        evidence.insert(
            1,
            ReceiptEvidence(
                ref={
                    "kind": "model_rows_model_calls",
                    "model": facts.model,
                    "model_call_ids": facts.model_call_ids,
                    "model_call_count": len(facts.model_call_ids),
                    "tokens_in": sum(
                        int(item.get("tokens_in") or 0) for item in provider_use
                    ),
                    "tokens_out": sum(
                        int(item.get("tokens_out") or 0) for item in provider_use
                    ),
                    "cost_actual": facts.cost_actual,
                    "op_id": facts.op_id,
                    "run_id": facts.run_id,
                },
                retention="pinned" if evaluation is not None else "compactable",
            ),
        )
    if evaluation is not None:
        assert context is not None
        guidelines = str(
            plan.request_identity.get("params", {}).get(evaluation.guidelines_param, "")
        )
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": "map_judge_guidelines",
                    "guidelines": guidelines,
                    "guidelines_hash": "sha256:"
                    + hashlib.sha256(guidelines.encode("utf-8")).hexdigest(),
                    "judged_column": context.subject_column,
                    "op_id": facts.op_id,
                    "run_id": facts.run_id,
                }
            )
        )
    if halt_error is not None:
        # Reserved columns are not published results. Keep any completed rows.
        output_refs = [ref for ref in output_refs if ref.get("row_ids")]
        named_result_refs = [ref for ref in named_result_refs if ref.get("row_ids")]
    provenance = Provenance(
        status=status,
        inputs=inputs,
        output_refs=output_refs,
        provider_use=provider_use,
        evidence=evidence,
        errors=errors,
        named_result_refs=named_result_refs,
    )
    receipt = build_receipt_from_provenance(
        provenance,
        facts,
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=plan.action.action_id,
        idempotency_key=plan.request.idempotency_key,
    )
    if any(
        cap in getattr(plan.action.definition.run, "capabilities", ())
        for cap in (FrameExtractor, FaceExtractor)
    ):
        from frisket.engine.executor.row_media_receipts import project_row_media_receipt

        receipt = project_row_media_receipt(project, plan, facts, receipt)
    if getattr(plan.program, "_uses_extract", False):
        from frisket.engine.executor.extract_result_evidence import (
            finalize_extract_receipt,
        )

        receipt = finalize_extract_receipt(receipt)
    return receipt


def typed_queued_map_spec(
    bound: BoundTypedActionRequest,
    *,
    initial_plan: TypedMapRowsPlan | None = None,
) -> tuple[_TypedExecutionEnvelope, Any, Recipe]:
    """Build one generic queued-project adapter for a typed map request.

    The returned internal envelope is structural execution identity, not a
    legacy ``ActionSpec``. The queue stores ``bound.request`` itself and calls
    this function again in the worker process to reconstruct the program.
    """

    from frisket.engine.executor.action_inventory import (
        _QueuedActionSpec,
        _queued_payload_codecs,
    )
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    unresolved = initial_plan or _typed_map_rows_plan(bound)
    terminal_policy = _MapTerminalPolicy.for_terminal(bound.action.definition.run)
    action = _TypedExecutionEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=MappingProxyType(dict(bound.request.params)),
    )

    def params_hash_fn(_envelope: Any) -> str:
        return typed_request_hash(bound)

    def resolve(
        project: Any,
        _params: BaseModel,
        _runner_spec: dict[str, Any],
    ) -> dict[str, Any] | ActionError:
        try:
            existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
            plan = build_typed_map_rows_plan(
                project,
                bound,
                _allow_existing_outputs=bool(
                    existing is not None and existing["status"] == "queued"
                ),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return _typed_plan_error(bound.action.action_id, exc)
        if (
            initial_plan is not None
            and initial_plan.evaluation_context is not None
            and plan.evaluation_context != initial_plan.evaluation_context
        ):
            return ActionError(
                code="stale_input",
                message="the judged column provenance changed before queue reservation",
                action_kind=bound.action.action_id,
                field="params.judged_column",
            )
        _runner_spec.clear()
        _runner_spec.update(plan.spec_dict())
        unresolved.program.bind_project(project)
        resolved = {
            "input_column_ids": dict(plan.source_column_ids),
            "input_column_types": dict(plan.source_column_types),
            "output_names": dict(plan.output_names),
            "output_target_preconditions": dict(plan.output_target_preconditions),
        }
        if terminal_policy.captures_row_source_snapshot:
            from frisket.engine.runner.row_inputs import row_source_snapshot

            resolved["row_source_snapshot"] = row_source_snapshot(
                project,
                bound.request.scope.sheet_id,
                plan.source_column_ids,
                row_ids=bound.request.scope.row_ids,
            )
        return resolved

    def pre_run_guard(
        project: Any,
        payload: Mapping[str, Any],
        _params: BaseModel,
    ) -> ActionError | None:
        expected_ids = payload.get("v1_input_column_ids")
        expected_types = payload.get("v1_input_column_types")
        expected_output_names = payload.get("v1_output_names")
        expected_output_preconditions = payload.get("v1_output_target_preconditions")
        expected_evaluation_context = payload["spec"].get("evaluation_context")
        if not isinstance(expected_ids, Mapping) or not isinstance(
            expected_types, Mapping
        ):
            return ActionError(
                code="stale_input",
                message="queued typed action is missing its source-column snapshot",
                action_kind=bound.action.action_id,
            )
        if not isinstance(expected_output_names, Mapping) or not isinstance(
            expected_output_preconditions, Mapping
        ):
            return ActionError(
                code="stale_input",
                message="queued typed action is missing its output-target snapshot",
                action_kind=bound.action.action_id,
            )
        try:
            current_plan = build_typed_map_rows_plan(
                project,
                bound,
                _allow_existing_outputs=True,
            )
        except TypedMapRowsPlanError as exc:
            input_error = exc.code in {"invalid_input_ref", "column_not_ai_generated"}
            return ActionError(
                code="stale_input",
                message=(
                    "queued action inputs are no longer valid"
                    if input_error
                    else "output targets changed after this action was queued"
                ),
                action_kind=bound.action.action_id,
                field=(exc.field or "scope") if input_error else "output_names",
                details=dict(exc.details),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return ActionError(
                code="stale_input",
                message="output targets changed after this action was queued",
                action_kind=bound.action.action_id,
                field="output_names",
                details={"reason": str(exc)},
            )
        if dict(current_plan.source_column_ids) != dict(expected_ids) or dict(
            current_plan.source_column_types
        ) != dict(expected_types):
            return ActionError(
                code="stale_input",
                message="source columns changed after this action was queued",
                action_kind=bound.action.action_id,
                field="scope",
            )
        if terminal_policy.captures_row_source_snapshot:
            from frisket.engine.runner.row_inputs import row_source_snapshot

            if payload.get("v1_row_source_snapshot") != row_source_snapshot(
                project,
                bound.request.scope.sheet_id,
                current_plan.source_column_ids,
                row_ids=bound.request.scope.row_ids,
            ):
                return ActionError(
                    code="stale_media_extract_pdf_tables_input"
                    if PdfTablesReader in bound.action.definition.run.capabilities
                    else "stale_input",
                    message="media sources changed since queue reservation",
                    action_kind=bound.action.action_id,
                    field="params",
                )
        current_columns = {
            str(column["name"]): int(column["id"])
            for column in project.columns(
                bound.request.scope.sheet_id, include_hidden=True
            )
        }
        run_id = payload.get("run_id")
        prepared_targets = (
            {
                binding.output_role: binding.column_id
                for binding in ResultGenerationStore(project).bindings_for_run(run_id)
            }
            if type(run_id) is int
            else {}
        )
        target_drift = any(
            (
                current_columns.get(str(name)) != int(expected_id)
                if type(expected_id) is int
                else current_columns.get(str(name))
                not in {None, prepared_targets.get(str(name))}
            )
            for name, expected_id in expected_output_preconditions.items()
        )
        if (
            dict(current_plan.output_names) != dict(expected_output_names)
            or target_drift
        ):
            return ActionError(
                code="stale_input",
                message="output targets changed after this action was queued",
                action_kind=bound.action.action_id,
                field="output_names",
            )
        current_evaluation_context = (
            current_plan.evaluation_context.payload()
            if current_plan.evaluation_context is not None
            else None
        )
        if current_evaluation_context != expected_evaluation_context:
            return ActionError(
                code="stale_input",
                message="the judged column provenance changed after this action was queued",
                action_kind=bound.action.action_id,
                field="params.judged_column",
            )
        unresolved.program.bind_project(project)
        return None

    def replay_completed(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: Any,
        **_kwargs: Any,
    ) -> ActionResult:
        return _reserved_receipt_result_from_existing(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            replay_error_fn=_typed_replay_error(project, bound),
        )

    def finalize(
        project: Any,
        action: Any,
        _params: BaseModel,
        **kwargs: Any,
    ) -> ActionResult:
        resolved = kwargs["resolved"]
        facts = resolved.facts
        plan = replace(
            unresolved,
            source_column_ids=MappingProxyType(
                dict(facts.get("input_column_ids") or {})
            ),
            source_column_types=MappingProxyType(
                dict(facts.get("input_column_types") or {})
            ),
        )
        receipt = _typed_receipt(
            project,
            int(kwargs["run_id"]),
            str(kwargs["action_id"]),
            str(kwargs["receipt_id"]),
            project_id=str(kwargs["project_id"]),
            plan=plan,
            params_hash=str(kwargs["params_hash"]),
        )
        finalized = _finalize_reserved_action_receipt(
            project,
            action,
            params_hash=str(kwargs["params_hash"]),
            project_id=str(kwargs["project_id"]),
            receipt=receipt,
            reservation_lost_message=f"{action.kind} reservation was lost",
            update_failed_message=f"{action.kind} receipt update failed",
            replay_error_fn=_typed_replay_error(project, bound),
            require_running_status=False,
        )
        result = finalized or _result_from_receipt(receipt)
        OutputColumnClaimStore(project).release(
            claim_token=_output_claim_token(str(kwargs["receipt_id"])),
            status="failed" if result.status == "failed" else "released",
        )
        return result

    spec = _QueuedActionSpec(
        kind=bound.action.action_id,
        params_model=bound.action.definition.run.params_model,
        completed_spec=None,
        runner_spec_fn=lambda _params: unresolved.spec_dict(),
        resolve_fn=resolve,
        precheck_fn=lambda _project, _params, _runner_spec, **_kwargs: None,
        reservation_kind="typed_map_rows_queue_reservation",
        queue_job_kind="typed_map_rows_queue_job",
        payload_codecs=_queued_payload_codecs(
            "input_column_ids",
            "input_column_types",
            "output_names",
            "output_target_preconditions",
            *(
                ("row_source_snapshot",)
                if terminal_policy.captures_row_source_snapshot
                else ()
            ),
        ),
        finalize_action=finalize,
        pre_run_guard=pre_run_guard,
        params_hash_fn=params_hash_fn,
        confirmed_fn=lambda _params: bound.request.confirmation is not None,
        cost_gate_error_fn=(
            _typed_model_cost_error
            if terminal_policy.model_backed
            else _typed_external_cost_error
            if terminal_policy.external_confirmation
            else None
        ),
        map_error_code=terminal_policy.map_error_code,
        output_claim_error_field="output_names",
        completed_result_from_existing_fn=replay_completed,
    )
    return action, spec, unresolved.program


def run_typed_map_rows_action(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    router: Any | None,
    map_runner_factory: Callable[[Any, Any | None], Any],
) -> ActionResult:
    """Run a direct typed request through the shared reserved-map lifecycle."""

    action, request = bound.action, bound.request
    terminal_policy = _MapTerminalPolicy.for_terminal(action.definition.run)
    replay = typed_map_rows_replay_result(project, project_id, bound)
    if replay is not None:
        return replay
    envelope = _TypedExecutionEnvelope(
        kind=action.action_id,
        idempotency_key=request.idempotency_key,
        params=MappingProxyType(dict(request.params)),
    )
    params_hash = typed_request_hash(bound)

    replay_error = _typed_replay_error(project, bound)

    def authorized_runner_factory(current_project: Any, current_router: Any) -> Any:
        runner = map_runner_factory(current_project, current_router)
        runner.allow_action_lifecycle_only_recipes = True
        return runner

    try:
        plan = build_typed_map_rows_plan(project, bound)
    except (TypeError, ValueError, ValidationError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.action_id,
            error=_typed_plan_error(action.action_id, exc),
        )

    return _run_reserved_maprunner_action(
        project,
        envelope,
        bound.params,
        project_id=project_id,
        router=router,
        map_runner_factory=authorized_runner_factory,
        cost_gate_error_fn=(
            partial(_typed_model_cost_error, envelope)
            if terminal_policy.model_backed
            else partial(_typed_external_cost_error, envelope)
            if terminal_policy.external_confirmation
            else None
        ),
        map_error_code=terminal_policy.map_error_code,
        resolved_execution=_PreparedMapExecution(
            runner_spec=plan.spec,
            output_fields=plan.output_fields,
            program=plan.program,
            params_hash=params_hash,
            receipt_fn=partial(
                _typed_receipt,
                project_id=project_id,
                plan=plan,
                params_hash=params_hash,
            ),
            replay_error_fn=replay_error,
            reservation_kind="typed_map_rows_idempotency_reservation",
        ),
        confirmed_fn=lambda _params: request.confirmation is not None,
        output_claim_error_field="output_names",
    )
