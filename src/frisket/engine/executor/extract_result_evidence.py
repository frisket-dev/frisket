"""Ground typed extraction outcomes inside the existing result-publication savepoint."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy

from frisket.actions.grounding_types import EvidenceClaim
from frisket.contracts.action import ActionError
from frisket.engine.executor.extract_evidence import (
    _CONFIDENT_ALIGNMENTS,
    _first_grounding_method,
    _is_missing_value,
    _resolve_evidence_entry_spans,
    _source_artifact,
    _text_hash,
)
from frisket.engine.store.evidence import record_evidence_link
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


def _captured_transcript(project, sources, *, sheet_id, row_id):
    """Use only temporal spans belonging to a captured source result, never latest."""
    from frisket.engine.store.grounding_contract import AnchorStream, SegmentStream
    from frisket.engine.store.transcript_segment_stream import (
        ResolvedSegmentStream,
        _segments_for_artifact,
    )

    for source in sources.values():
        ref = source.get("value_ref") or {}
        if ref.get("kind") != "run_result" or ref.get("run_id") is None:
            continue
        spans = project.db.execute(
            "SELECT s.id, s.artifact_id FROM evidence_links l "
            "JOIN evidence_link_spans ls ON ls.link_id=l.id "
            "JOIN source_spans s ON s.id=ls.span_id "
            # A transcription's segment-list output shares the text output's
            # evidence. Resolve through the captured producing run, not through
            # the selected sibling column (and never through the latest row).
            "WHERE l.run_id=? AND l.row_id=? AND l.sheet_id=? "
            "AND l.link_role='media_transcribe_temporal' AND s.span_kind='temporal' "
            "ORDER BY l.id DESC,ls.rank",
            (ref["run_id"], row_id, sheet_id),
        ).fetchall()
        if not spans:
            continue
        artifact_id = spans[0]["artifact_id"]
        units, tokens, indices = _segments_for_artifact(
            project,
            artifact_id,
            span_ids=[
                span["id"] for span in spans if span["artifact_id"] == artifact_id
            ],
        )
        if units:
            return ResolvedSegmentStream(
                anchors=AnchorStream(units=units),
                segments=SegmentStream(tokens=tokens),
                artifact_id=artifact_id,
                span_ids_by_segment_index=indices,
            )
    return None


def extract_prompt_values(project, values, spec, *, prompt_source_columns):
    """Add numbered transcript anchors from the selected, captured input version."""
    from frisket.engine.runner.grounding import looks_like_segment_list

    if any(looks_like_segment_list(value) for value in values.values()):
        return values
    # All admitted sources remain captured for evidence publication, but only
    # selected prompt sources may contribute model-visible transcript anchors.
    sources = {
        name: source
        for name, source in (getattr(values, "source_cells", None) or {}).items()
        if name in prompt_source_columns
    }
    row_id = next(
        (
            source["value_ref"]["row_id"]
            for source in sources.values()
            if isinstance(source.get("value_ref"), dict)
            and source["value_ref"].get("kind") == "run_result"
            and source["value_ref"].get("row_id") is not None
        ),
        None,
    )
    if row_id is None:
        return values
    transcript = _captured_transcript(
        project, sources, sheet_id=spec["sheet_id"], row_id=row_id
    )
    if transcript is None:
        return values
    segments = []
    for unit in transcript.anchors.units:
        span = unit.spans[0]
        segments.append(
            {
                "segment_index": unit.unit_id,
                "text": unit.text,
                "start": span.start_ms / 1000 if span.start_ms is not None else None,
                "end": span.end_ms / 1000 if span.end_ms is not None else None,
                **({"speaker": unit.speaker} if unit.speaker else {}),
            }
        )
    key = "transcript_segments"
    while key in values:
        key = f"_{key}"
    return {**values, key: segments}


class ExtractResultEvidence:
    """Frozen output policy plus captured input facts, shared by all extract rows."""

    def __init__(
        self,
        project,
        *,
        fields,
        output_names,
        required_fields=frozenset(),
        grounding_enabled=False,
        citation_required=False,
        source_columns=(),
    ):
        self.project = project
        self.fields = dict(fields)
        self.output_names = dict(output_names)
        self.required_fields = frozenset(required_fields)
        self.grounding_enabled = grounding_enabled
        self.citation_required = citation_required
        self.source_columns = tuple(
            getattr(column, "name", column) for column in source_columns
        )

    def capture(self, values, capture, results, spec, *, row_id, **kwargs):
        del values, spec, row_id, kwargs
        sources = {
            name: deepcopy(source)
            for name, source in capture.items()
            if not self.source_columns or name in self.source_columns
        }
        for cell in results.values():
            # Returned checkpoints retain the source captured before their call.
            cell.setdefault("extraction_sources", sources)

    def write(
        self,
        project,
        spec,
        *,
        batch,
        sheet_id,
        run_id,
        op_id,
        output_columns,
        claim_token,
        writer_attempt_id,
        **kwargs,
    ):
        del kwargs
        OutputColumnClaimStore.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )
        receipt_id = project.db.execute(
            "SELECT DISTINCT receipt_id FROM output_column_claims "
            "WHERE run_id=? AND claim_token=? AND status='active'",
            (run_id, claim_token),
        ).fetchone()[0]
        fields = {
            int(output_columns[physical]): logical
            for logical, physical in self.output_names.items()
            if logical in self.fields
        }
        store = ReceiptStore(project)
        run_store = RunResultStore(project)
        artifact_cache = {}
        for cell in batch:
            column_id, row_id = int(cell["column_id"]), int(cell["row_id"])
            logical = fields.get(column_id)
            if logical is None or cell.get("error") is not None:
                continue
            field = self.fields[logical]
            value = cell.get("value")
            warnings = list(cell.get("warnings") or ())
            failures, links, unsupported = [], [], []
            if logical in self.required_fields and _is_missing_value(value):
                failures.append("required_field_missing")
            if self.grounding_enabled and value is not None and not failures:
                sources = cell.get("extraction_sources") or {}
                transcript = _captured_transcript(
                    project, sources, sheet_id=sheet_id, row_id=row_id
                )
                artifact = _source_artifact(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    source_columns=list(sources),
                    input_column_ids={
                        key: source["column_id"] for key, source in sources.items()
                    },
                    artifact_cache=artifact_cache,
                    captured_sources=sources,
                )
                groups = defaultdict(list)
                for raw in cell.get("evidence") or ():
                    claim = EvidenceClaim.model_validate(raw)
                    groups[claim.item_index].append(
                        claim.model_dump(
                            mode="json", exclude_none=True, exclude={"item_index"}
                        )
                    )
                is_list = field.schema.get("type") == "array" and isinstance(
                    value, list
                )
                members = enumerate(value) if is_list else [(None, value)]
                for item_index, member in members:
                    entries = groups[item_index]
                    spans = []
                    for rank, entry in enumerate(entries):
                        resolved = _resolve_evidence_entry_spans(
                            project,
                            artifact=artifact,
                            sheet_id=sheet_id,
                            row_id=row_id,
                            entry=entry,
                            rank=rank,
                            transcript_stream=transcript,
                        )
                        if resolved:
                            spans.extend(resolved)
                        else:
                            warnings.append("evidence_span_unsupported")
                    if not spans:
                        unsupported.append(
                            {
                                "row_id": row_id,
                                "column_id": column_id,
                                "field": logical,
                                "item_index": item_index,
                                "reason": "evidence_span_unsupported"
                                if entries
                                else "evidence_missing",
                                "citation_required": self.citation_required,
                            }
                        )
                        continue
                    warnings.extend(
                        str(span["metadata"]["alignment"])
                        for span in spans
                        if (span.get("metadata") or {}).get("alignment")
                        and span["metadata"]["alignment"] not in _CONFIDENT_ALIGNMENTS
                    )
                    metadata = {
                        "schema_version": "frisket.map_extract_item_evidence_link.v1"
                        if is_list
                        else "frisket.map_extract_evidence_link.v1",
                        "field": logical,
                        "output_role": logical,
                        "source_action_kind": spec["action_kind"],
                        "grounding_method": _first_grounding_method(entries),
                        "warnings": list(dict.fromkeys(warnings)),
                        "value_hash": _text_hash(json.dumps(member, sort_keys=True)),
                        **({"item_index": item_index} if is_list else {}),
                    }
                    link = record_evidence_link(
                        project,
                        subject_kind="cell_value",
                        subject_ref={
                            "kind": "run_result",
                            "run_id": run_id,
                            "op_id": op_id,
                            "row_id": row_id,
                            "column_id": column_id,
                        },
                        spans=[
                            {
                                "span_id": span["id"],
                                "rank": index,
                                "span_role": "support",
                                "required": True,
                            }
                            for index, span in enumerate(spans)
                        ],
                        sheet_id=sheet_id,
                        row_id=row_id,
                        column_id=column_id,
                        run_id=run_id,
                        op_id=op_id,
                        receipt_id=receipt_id,
                        link_role="primary_support",
                        producer=metadata,
                        metadata=metadata,
                    )
                    links.append(
                        {
                            "id": link["id"],
                            "stable_id": link["stable_id"],
                            "row_id": row_id,
                            "column_id": column_id,
                            "field": logical,
                            **({"item_index": item_index} if is_list else {}),
                        }
                    )
                # A partially grounded list stays whole; a truly empty list needs no citation.
                if self.citation_required and not links and not (is_list and not value):
                    failures.append("evidence_required")
            if failures:
                run_store.withhold_result_value(
                    run_id,
                    row_id,
                    column_id,
                    f"{spec['action_kind']}: {failures[0]}",
                    commit=False,
                )
            if links or unsupported or failures or warnings:
                store._record_writer_evidence(
                    {
                        "kind": "map_extract_grounding_links",
                        "link_refs": links,
                        "unsupported_values": unsupported,
                        "warnings": list(dict.fromkeys(warnings)),
                        "errors": [
                            {
                                "code": code,
                                "row_id": row_id,
                                "column_id": column_id,
                                "field": logical,
                            }
                            for code in failures
                        ],
                    },
                    run_id=run_id,
                    writer_attempt_id=writer_attempt_id,
                    claim_token=claim_token,
                )


def finalize_extract_receipt(receipt):
    """Surface persisted publication decisions without rerunning any grounding."""
    for evidence in receipt.evidence:
        fact = evidence.ref
        if fact.get("kind") != "map_extract_grounding_links":
            continue
        for warning in fact.get("warnings", []):
            if warning not in receipt.warnings:
                receipt.warnings.append(warning)
        for failure in fact.get("errors", []):
            code = failure["code"]
            message = (
                "Required field was null or missing"
                if code == "required_field_missing"
                else "Value has no required supporting evidence"
            )
            error = ActionError(
                code=code,
                message=message,
                action_kind=receipt.action_kind,
                field=f"params.fields.{failure['field']}",
                details=failure,
            )
            if error not in receipt.errors:
                receipt.errors.append(error)
    return receipt
