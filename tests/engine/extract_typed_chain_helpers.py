"""Typed ``map.extract`` / ``map.ner`` request producers for the extract chain tests.

Every producer builds the real typed boundary shape (``action_id`` / ``scope`` /
``params`` / ``output_names`` / ``idempotency_key``); consent is the top-level
``confirmation`` echo that ``run_action_with_exact_confirmation`` supplies from
the quote, never a bare boolean.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MODEL = "anthropic/claude-haiku-4-5"


def extract_output_names(
    fields: list[dict[str, Any]], *, include_confidence: bool = False
) -> dict[str, str]:
    names = {str(field["name"]): str(field["name"]) for field in fields}
    if include_confidence and fields:
        confidence = f"{fields[0]['name']}_confidence"
        names[confidence] = confidence
    return names


def typed_extract_request(
    sheet_id: int,
    *,
    source: list[str] | dict[str, str],
    fields: list[dict[str, Any]],
    idempotency_key: str,
    model: str = DEFAULT_MODEL,
    instruction: str = "",
    context: str = "",
    grounding: dict[str, Any] | None = None,
    source_document_columns: list[str] | None = None,
    evidence_policy: dict[str, Any] | None = None,
    include_confidence: bool = False,
    row_ids: list[int] | None = None,
    output_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = list(row_ids)
    params: dict[str, Any] = {
        "source": source,
        "model": model,
        "instruction": instruction,
        "fields": fields,
    }
    if context:
        params["context"] = context
    if grounding is not None:
        params["grounding"] = grounding
    if source_document_columns is not None:
        params["source_document_columns"] = source_document_columns
    if evidence_policy is not None:
        params["evidence_policy"] = evidence_policy
    if include_confidence:
        params["include_confidence"] = True
    return {
        "action_id": "map.extract",
        "scope": scope,
        "params": params,
        "output_names": (
            dict(output_names)
            if output_names is not None
            else extract_output_names(fields, include_confidence=include_confidence)
        ),
        "idempotency_key": idempotency_key,
    }


def typed_ner_request(
    sheet_id: int,
    *,
    labels: list[str],
    idempotency_key: str,
    source: list[str] | None = None,
    engine: str = "gliner",
    threshold: float | None = None,
    output_name: str = "entities",
    row_ids: list[int] | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = list(row_ids)
    params: dict[str, Any] = {
        "source": list(source) if source is not None else ["body"],
        "labels": list(labels),
        "engine": engine,
    }
    if threshold is not None:
        params["threshold"] = threshold
    return {
        "action_id": "map.ner",
        "scope": scope,
        "params": params,
        "output_names": {"entities": output_name},
        "idempotency_key": idempotency_key,
    }
