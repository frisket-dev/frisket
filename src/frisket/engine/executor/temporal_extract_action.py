"""Invocation-owned temporal preparation and all-or-nothing clip publication."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_extract_types import (
    PreparedTemporalExtract,
    TemporalExtractor,
    TemporalExtractSelection,
)
from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    validate_transcript_selection_scope,
)
from frisket.actions.types import SheetRows
from frisket.contracts.action import ActionError, ActionResult
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_reservations import (
    _reserve_running_action_receipt,
    _reserved_receipt_result_from_existing,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.temporal_extract_publication import (
    StagedExtractClip,
    _extract_plan_annotations,
    _reserved_extract_preflight,
    _terminalize_extract_failure,
    _write_extract_in_transaction,
)
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
    ResolvedTemporalMediaSource,
    StagedTemporalClip,
    prepare_bound_sources,
    resolve_temporal_media_source,
    revalidate_action_sources,
    revalidate_temporal_media_source,
    temporal_inherited_output_keys,
    validate_staged_temporal_clip,
)
from frisket.engine.executor.temporal_transcripts import project_transcript
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.engine.store.receipts import ReceiptStore


LOG = logging.getLogger("frisket.executor")


class _ExtractRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(error.message)


@dataclass(frozen=True)
class _ExtractPlan:
    sources: tuple[ResolvedTemporalMediaSource, ...]
    transcript_keys: tuple[tuple[int, str], ...]
    annotation_keys: tuple[tuple[int, str], ...]

    def revalidate(self, project):
        revalidate_action_sources(project, tuple(plan.source for plan in self.sources))
        for source in self.sources:
            revalidate_temporal_media_source(project, source)

    def logical_outputs(self):
        annotations = {
            annotation.column_id: annotation.type_name
            for source in self.sources
            for annotation in source.annotations
        }
        return [
            {"key": "clip", "column_type": self.sources[0].source.column_type},
            *(
                {"key": key, "column_type": "timestamped_transcript"}
                for _, key in self.transcript_keys
            ),
            *(
                {"key": key, "column_type": annotations[column_id]}
                for column_id, key in self.annotation_keys
            ),
        ]


@dataclass(frozen=True)
class ExtractPublication:
    """Private resolved names; none are passed to the action handler."""

    sheet_id: int
    output_name: str
    transcript_names: dict[int, str]
    annotation_names: dict[int, str]

    @property
    def names(self):
        return (
            self.output_name,
            *self.transcript_names.values(),
            *self.annotation_names.values(),
        )


class AdmittedTemporalExtractor:
    def __init__(self, project, bound):
        self._project = project
        self._bound = bound
        self.plans: dict[PreparedTemporalExtract, _ExtractPlan] = {}

    def prepare(self, source, selection):
        source = TemporalMediaColumn.model_validate(source)
        selection = TypeAdapter(TemporalExtractSelection).validate_python(selection)
        scope = self._bound.request.scope
        if not isinstance(scope, SheetRows):
            raise ValueError("temporal extraction requires a sheet_rows scope")
        validate_transcript_selection_scope(selection, scope)
        sources = prepare_bound_sources(
            self._project,
            scope=scope,
            source=source,
            selection=selection,
            action_kind=self._bound.action.action_id,
            purpose="extract",
        )
        if isinstance(sources, ActionError):
            raise _ExtractRefused(sources)
        if any(len(item.ranges) != 1 for item in sources):
            raise TimelineError(
                "invalid_range", "Extract requires exactly one range per source row"
            )
        plans = tuple(
            resolve_temporal_media_source(self._project, item) for item in sources
        )
        transcript_keys, annotation_keys = temporal_inherited_output_keys(
            (transcript for plan in plans for transcript in plan.transcripts),
            (annotation for plan in plans for annotation in plan.annotations),
            prefix="clip_",
        )
        plan = _ExtractPlan(
            plans, tuple(transcript_keys.items()), tuple(annotation_keys.items())
        )
        token = PreparedTemporalExtract(selected_row_count=len(plans))
        self.plans[token] = plan
        return token


def supports_typed_temporal_extract_action(terminal):
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        TemporalExtractor,
    )


def _prepare_temporal_extract_plan(project, bound):
    terminal = bound.action.definition.run
    if not supports_typed_temporal_extract_action(terminal):
        raise TypeError("temporal extraction requires its admitted capability")
    extractor = AdmittedTemporalExtractor(project, bound)
    returned = terminal.handler(bound.params, extractor)
    if (
        not isinstance(returned, PreparedTemporalExtract)
        or returned not in extractor.plans
    ):
        raise ValueError(
            "temporal extraction must return its invocation-owned preparation"
        )
    plan = extractor.plans[returned]
    plan.revalidate(project)
    return plan


def resolve_temporal_extract_outputs(project, bound):
    """Discover actual logical outputs without final names or publication effects."""
    try:
        return _prepare_temporal_extract_plan(project, bound).logical_outputs()
    except Exception as error:
        return _extract_failure(error, bound.action.action_id, validation=True)


def prepare_temporal_extract_action(project, bound):
    plan = _prepare_temporal_extract_plan(project, bound)
    keys = {output["key"] for output in plan.logical_outputs()}
    unknown = set(bound.request.output_names) - keys
    if unknown:
        raise ValueError("unknown output names: " + ", ".join(sorted(unknown)))
    names = {key: bound.request.output_names.get(key, key) for key in keys}
    if len(set(names.values())) != len(names):
        raise ValueError("final output names must be unique")
    publication = ExtractPublication(
        sheet_id=bound.request.scope.sheet_id,
        output_name=names["clip"],
        transcript_names={
            column_id: names[key] for column_id, key in plan.transcript_keys
        },
        annotation_names={
            column_id: names[key] for column_id, key in plan.annotation_keys
        },
    )
    # Check knowable collisions early; the writer checks the actual rendered
    # intersection again. A renderer may legitimately expand the requested cut.
    requested_annotations = {
        annotation.column_id for annotation in _extract_plan_annotations(plan.sources)
    }
    expected_names = (
        publication.output_name,
        *publication.transcript_names.values(),
        *(
            name
            for column_id, name in publication.annotation_names.items()
            if column_id in requested_annotations
        ),
    )
    placeholders = ",".join("?" for _ in expected_names)
    duplicate = project.db.execute(
        f"SELECT name FROM columns WHERE sheet_id=? AND name IN ({placeholders}) LIMIT 1",
        (publication.sheet_id, *expected_names),
    ).fetchone()
    if duplicate is not None:
        raise TimelineError(
            "invalid_input_ref", f"output column {duplicate['name']!r} already exists"
        )
    return plan, publication


def _extract_failure(error, kind, *, validation=False):
    if isinstance(error, _ExtractRefused):
        return error.error
    if isinstance(error, TimelineError):
        return ActionError(code=error.code, message=error.message, action_kind=kind)
    if validation and isinstance(error, (ValueError, TypeError)):
        return ActionError(code="invalid_params", message=str(error), action_kind=kind)
    raw_code = str(getattr(error, "code", "project_write_failed"))
    if raw_code == "source_missing":
        code, message = (
            "stale_input",
            "Source media disappeared during temporal extraction",
        )
    elif raw_code in {"invalid_range", "cut_alignment_failed"}:
        code, message = (
            raw_code,
            str(getattr(error, "message", "Temporal extraction failed")),
        )
    elif raw_code == "project_write_failed":
        code, message = (
            raw_code,
            "Temporal clips and their evidence could not be published",
        )
    else:
        code, message = (
            "temporal_materializer_unavailable",
            "The exact media renderer could not produce the clip",
        )
    details = getattr(error, "details", None)
    return ActionError(
        code=code,
        message=message,
        action_kind=kind,
        details=dict(details) if isinstance(details, dict) else {},
    )


def run_typed_temporal_extract_action(
    project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    reserved_action_id: str | None = None,
    reserved_receipt_id: str | None = None,
) -> ActionResult:
    kind = bound.action.action_id
    queued = reserved_receipt_id is not None
    params_hash = typed_request_hash(bound)
    envelope = _TypedProjectEnvelope(
        kind, bound.request.idempotency_key, bound.params.model_dump(mode="json")
    )
    if (reserved_action_id is None) != (reserved_receipt_id is None):
        return _failed_result(
            project_id=project_id,
            action_kind=kind,
            error=ActionError(
                code="project_write_failed",
                message="Reservation ids must be supplied together",
                action_kind=kind,
            ),
        )
    if reserved_receipt_id is not None:
        prior = _reserved_extract_preflight(
            project,
            receipt_id=reserved_receipt_id,
            action_id=reserved_action_id,
            params_hash=params_hash,
            action_kind=kind,
        )
        if isinstance(prior, ActionResult):
            return prior
        if isinstance(prior, ActionError):
            return _failed_result(project_id=project_id, action_kind=kind, error=prior)
    else:
        stored = ReceiptStore(project).find_by_idempotency_key(
            bound.request.idempotency_key
        )
        if stored is not None:
            return _reserved_receipt_result_from_existing(
                project,
                stored,
                params_hash=params_hash,
                project_id=project_id,
                action=envelope,
            )
    try:
        plan, publication = prepare_temporal_extract_action(project, bound)
    except Exception as error:
        failure = _extract_failure(error, kind, validation=True)
        if reserved_receipt_id is not None:
            return _terminalize_extract_failure(
                project,
                project_id=project_id,
                receipt_id=reserved_receipt_id,
                error=failure,
                action_kind=kind,
            )
        return _failed_result(project_id=project_id, action_kind=kind, error=failure)
    if reserved_receipt_id is None:
        reservation = _reserve_running_action_receipt(
            project,
            envelope,
            params_hash=params_hash,
            project_id=project_id,
            reservation_kind="temporal_extract_range",
            result_from_existing_fn=_reserved_receipt_result_from_existing,
        )
        if isinstance(reservation, ActionResult):
            return reservation
        reserved_action_id, reserved_receipt_id = (
            reservation["action_id"],
            reservation["receipt_id"],
        )
    materializer = CoreTemporalMediaMaterializer()
    try:
        with tempfile.TemporaryDirectory(prefix="frisket-temporal-extract-") as scratch:
            staged = []
            plan.revalidate(project)
            for ordinal, source_plan in enumerate(plan.sources):
                source = source_plan.source
                suffix = ".flac" if source.lease.media_kind == "audio" else ".mp4"
                path = Path(scratch) / f"clip-{ordinal:04d}{suffix}"
                rendered = materializer.stage(project, source, source.ranges[0], path)
                validate_staged_temporal_clip(source, source.ranges[0], rendered, path)
                clip = StagedTemporalClip(
                    source=source, requested=source.ranges[0], payload=rendered
                )
                staged.append(
                    StagedExtractClip(
                        clip=clip,
                        transcripts=tuple(
                            (
                                transcript,
                                project_transcript(
                                    transcript,
                                    source_start_ms=clip.resolved_start_ms,
                                    source_end_ms=clip.resolved_end_ms,
                                ),
                            )
                            for transcript in source_plan.transcripts
                        ),
                        annotations=source_plan.annotations,
                        topic_analysis=source_plan.topic_analysis,
                        plan_warnings=source_plan.warnings,
                    )
                )
            project.db.execute("BEGIN IMMEDIATE")
            try:
                result = _write_extract_in_transaction(
                    project,
                    bound,
                    publication,
                    tuple(staged),
                    project_id=project_id,
                    action_id=reserved_action_id,
                    receipt_id=reserved_receipt_id,
                    params_hash=params_hash,
                    materializer=materializer,
                )
                project.db.commit()
                return result
            except BaseException:
                project.db.rollback()
                raise
    except Exception as error:
        LOG.debug("Temporal extraction failed", exc_info=True)
        failure = _extract_failure(error, kind)
        if queued and failure.code == "temporal_materializer_unavailable":
            return _failed_result(
                project_id=project_id,
                action_kind=kind,
                error=failure.model_copy(
                    update={"details": {**failure.details, "retryable": True}}
                ),
            )
        return _terminalize_extract_failure(
            project,
            project_id=project_id,
            receipt_id=reserved_receipt_id,
            error=failure,
            action_kind=kind,
        )


def run_typed_temporal_extract_job(project, envelope):
    from frisket.engine.executor.action_jobs import bind_typed_action_job

    bound = bind_typed_action_job(envelope)
    if isinstance(bound, ActionResult):
        return bound
    return run_typed_temporal_extract_action(
        project,
        envelope.project_id,
        bound,
        reserved_action_id=envelope.action_id,
        reserved_receipt_id=envelope.receipt_id,
    )
