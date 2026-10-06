"""Ground typed extraction outcomes inside the existing result-publication savepoint."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from typing import Any

from frisket.actions.grounding_types import EvidenceClaim
from frisket.contracts.action import ActionError
from frisket.engine.executor.extract_evidence import (
    _CONFIDENT_ALIGNMENTS,
    _first_grounding_method,
    _is_missing_value,
    _prepared_ref_id,
    _resolve_evidence_entry_spans,
    _source_artifact,
    _span_is_usable,
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


def _resolve_claim_source(
    sources: dict[str, dict[str, Any]], entry: dict[str, Any]
) -> str | None:
    """Resolve a model label only within the inputs visible for this request."""

    from frisket.engine.store.text_quote_match import quote_ranges

    eligible = {
        name: source for name, source in sources.items() if source.get("model_visible")
    }
    requested = entry.get("source")
    if requested is not None:
        label = str(requested).strip()
        return label if label in eligible else None
    canonical = {
        str(source.get("alias_of") or name) for name, source in eligible.items()
    }
    if len(canonical) == 1:
        label = next(iter(canonical))
        return label if label in eligible else next(iter(eligible))
    quote = entry.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return None
    matched = [
        name
        for name, source in eligible.items()
        if isinstance(source.get("captured_text"), str)
        and quote_ranges(source["captured_text"], quote.strip())
    ]
    matched_canonical = {
        str(eligible[name].get("alias_of") or name) for name in matched
    }
    if len(matched_canonical) != 1:
        return None
    label = next(iter(matched_canonical))
    return label if label in eligible else matched[0]


def _grounding_artifact_source(
    sources: dict[str, dict[str, Any]],
    source_label: str,
    entry: dict[str, Any],
    auxiliary_names: tuple[str, ...],
) -> str | None:
    """Keep established PDF/OCR grounding without making the file model-visible."""

    source = sources[source_label]
    if _prepared_ref_id(source) is not None:
        return source_label
    is_ocr_source = source.get("producer_action_kind") == "media.ocr"
    has_document_locator = any(
        entry.get(key) is not None for key in ("bbox", "page", "page_start", "page_end")
    )
    if not is_ocr_source and not has_document_locator:
        return source_label
    source_value = source.get("value")
    if isinstance(source_value, dict) and source_value.get("blob"):
        return source_label
    candidates = []
    for name in auxiliary_names:
        candidate = sources.get(name) or {}
        value = candidate.get("value")
        if (
            not candidate.get("model_visible")
            and isinstance(value, dict)
            and value.get("blob")
        ):
            candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    if is_ocr_source and not has_document_locator and not candidates:
        return source_label
    return None


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
        prompt_source_columns=(),
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
        self.prompt_source_columns = tuple(prompt_source_columns)

    def capture(self, values, capture, results, spec, *, row_id, **kwargs):
        del kwargs
        from frisket.engine.runner.grounding import looks_like_segment_list
        from frisket.ops.extraction import _numbered_segments_text

        sources = {
            name: {**deepcopy(source), "model_visible": False}
            for name, source in capture.items()
        }
        for source in sources.values():
            ref = source.get("value_ref") or {}
            run_id = ref.get("run_id")
            if run_id is None:
                continue
            run = self.project.db.execute(
                "SELECT action_kind FROM runs WHERE id=?", (int(run_id),)
            ).fetchone()
            if run is not None:
                source["producer_action_kind"] = run["action_kind"]
        prompt_values = extract_prompt_values(
            self.project,
            values,
            spec,
            prompt_source_columns=self.prompt_source_columns,
        )
        for name, value in prompt_values.items():
            inherited_name = None
            if name not in sources and looks_like_segment_list(value):
                inherited_name = next(
                    (
                        candidate
                        for candidate in self.prompt_source_columns
                        if candidate in sources
                        and _captured_transcript(
                            self.project,
                            {candidate: sources[candidate]},
                            sheet_id=spec["sheet_id"],
                            row_id=row_id,
                        )
                        is not None
                    ),
                    None,
                )
            inherited = sources.get(inherited_name) if inherited_name else None
            source = sources.setdefault(
                name,
                (
                    {**deepcopy(inherited), "composite": True}
                    if inherited is not None
                    else {
                        "column_id": None,
                        "column_type": "text",
                        "value_ref": None,
                        "composite": True,
                    }
                ),
            )
            source["model_visible"] = True
            if inherited_name is not None:
                source["alias_of"] = inherited_name
            source["value"] = deepcopy(value)
            if isinstance(value, str):
                source["captured_text"] = value
            elif looks_like_segment_list(value):
                source["captured_text"] = _numbered_segments_text(value)
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
                missing_required_item = False
                for item_index, member in members:
                    entries = groups[item_index]
                    item_links = []
                    for rank, entry in enumerate(entries):
                        source_label = _resolve_claim_source(sources, entry)
                        if source_label is None:
                            warnings.append("evidence_source_unresolved")
                            unsupported.append(
                                {
                                    "row_id": row_id,
                                    "column_id": column_id,
                                    "field": logical,
                                    "item_index": item_index,
                                    "reason": "evidence_source_unresolved",
                                    "citation_required": self.citation_required,
                                }
                            )
                            continue
                        source = sources[source_label]
                        artifact_source_label = _grounding_artifact_source(
                            sources, source_label, entry, self.source_columns
                        )
                        if artifact_source_label is None:
                            warnings.append("evidence_source_unresolved")
                            unsupported.append(
                                {
                                    "row_id": row_id,
                                    "column_id": column_id,
                                    "field": logical,
                                    "item_index": item_index,
                                    "reason": "evidence_source_unresolved",
                                    "citation_required": self.citation_required,
                                }
                            )
                            continue
                        artifact_source = sources[artifact_source_label]
                        artifact = _source_artifact(
                            project,
                            sheet_id=sheet_id,
                            row_id=row_id,
                            source_columns=[artifact_source_label],
                            input_column_ids={
                                artifact_source_label: artifact_source.get("column_id")
                            },
                            artifact_cache=artifact_cache,
                            captured_sources=sources,
                            source_label=source_label,
                        )
                        transcript = _captured_transcript(
                            project,
                            {source_label: source},
                            sheet_id=sheet_id,
                            row_id=row_id,
                        )
                        resolved = _resolve_evidence_entry_spans(
                            project,
                            artifact=artifact,
                            sheet_id=sheet_id,
                            row_id=row_id,
                            entry=entry,
                            rank=rank,
                            captured_source=source,
                            source_label=source_label,
                            transcript_stream=transcript,
                        )
                        if not resolved or not any(
                            _span_is_usable(span) for span in resolved
                        ):
                            warnings.append("evidence_span_unsupported")
                            unsupported.append(
                                {
                                    "row_id": row_id,
                                    "column_id": column_id,
                                    "field": logical,
                                    "item_index": item_index,
                                    "reason": "evidence_span_unsupported",
                                    "citation_required": self.citation_required,
                                }
                            )
                            continue
                        warnings.extend(
                            str(span["metadata"]["alignment"])
                            for span in resolved
                            if (span.get("metadata") or {}).get("alignment")
                            and span["metadata"]["alignment"]
                            not in _CONFIDENT_ALIGNMENTS
                        )
                        metadata = {
                            "schema_version": "frisket.map_extract_item_evidence_link.v1"
                            if is_list
                            else "frisket.map_extract_evidence_link.v1",
                            "field": logical,
                            "output_role": logical,
                            "source_action_kind": spec["action_kind"],
                            "source": source_label,
                            "grounding_method": _first_grounding_method([entry]),
                            "warnings": list(dict.fromkeys(warnings)),
                            "value_hash": _text_hash(
                                json.dumps(member, sort_keys=True)
                            ),
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
                                for index, span in enumerate(resolved)
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
                        link_ref = {
                            "id": link["id"],
                            "stable_id": link["stable_id"],
                            "row_id": row_id,
                            "column_id": column_id,
                            "field": logical,
                            **({"item_index": item_index} if is_list else {}),
                        }
                        links.append(link_ref)
                        item_links.append(link_ref)
                    if not entries:
                        unsupported.append(
                            {
                                "row_id": row_id,
                                "column_id": column_id,
                                "field": logical,
                                "item_index": item_index,
                                "reason": "evidence_missing",
                                "citation_required": self.citation_required,
                            }
                        )
                    if self.citation_required and not item_links:
                        missing_required_item = True
                # Empty lists need no evidence; every member of a nonempty list does.
                if self.citation_required and missing_required_item:
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
