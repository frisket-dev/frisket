"""Build deterministic FollowTheMoney export packages from resolved rowsets."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from frisket.features.followthemoney.adapter import (
    parse_entities_jsonl,
    serialize_entities_jsonl,
)
from frisket.features.followthemoney.mapping import map_rowset_to_entities

FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION = (
    "frisket.followthemoney.export_package.v1"
)
FOLLOWTHEMONEY_VALIDATION_REPORT_SCHEMA_VERSION = (
    "frisket.followthemoney.validation_report.v1"
)
FOLLOWTHEMONEY_SOURCE_REFS_SCHEMA_VERSION = "frisket.followthemoney.source_refs.v1"

FOLLOWTHEMONEY_EXPORT_FILENAMES = {
    "entities": "entities.ftm.jsonl",
    "validation_report": "validation_report.json",
    "mapping": "mapping.json",
    "source_refs": "source_refs.json",
    "manifest": "manifest.json",
}

FOLLOWTHEMONEY_EXPORT_CONTENT_TYPES = {
    "entities": "application/x-ndjson; charset=utf-8",
    "validation_report": "application/json; charset=utf-8",
    "mapping": "application/json; charset=utf-8",
    "source_refs": "application/json; charset=utf-8",
    "manifest": "application/json; charset=utf-8",
}


def build_followthemoney_export_package(
    *,
    source: Mapping[str, Any],
    mappings: list[Mapping[str, Any]],
    rowsets_by_key: Mapping[str, Mapping[str, Any]],
    project_id: str,
    media_policy: str = "refs",
    validate: bool = True,
) -> dict[str, Any]:
    """Build package artifacts without writing them anywhere.

    The executor owns delivery and receipts. This helper owns deterministic
    entity mapping, validation sidecars, and source-reference sidecars.
    """

    mapping_results: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    for index, mapping_spec in enumerate(mappings):
        rowset_key = followthemoney_rowset_key(mapping_spec.get("rowset"))
        rowset_payload = rowsets_by_key.get(rowset_key)
        if rowset_payload is None:
            diagnostic = {
                "code": "invalid_rowset_spec",
                "severity": "error",
                "message": "mapping rowset was not resolved",
                "mapping_index": index,
                "rowset": mapping_spec.get("rowset"),
            }
            diagnostics.append(diagnostic)
            mapping_results.append(
                {
                    "mapping_index": index,
                    "schema": mapping_spec.get("schema"),
                    "rowset": mapping_spec.get("rowset"),
                    "entity_count": 0,
                    "skipped_count": 0,
                    "diagnostics": [diagnostic],
                    "coverage": _zero_coverage(),
                }
            )
            continue

        result = map_rowset_to_entities(
            rowset_payload,
            mapping_spec,
            project_id=project_id,
        )
        result["mapping_index"] = index
        mapping_results.append(result)
        entities.extend(result["entities"])
        diagnostics.extend(result["diagnostics"])
        source_entries.extend(
            _source_entries_for_mapping(
                rowset_payload=rowset_payload,
                mapping_spec=mapping_spec,
                mapping_result=result,
            )
        )

    entity_stream = serialize_entities_jsonl(entities)
    parsed = parse_entities_jsonl(entity_stream) if validate else _unvalidated_parse()
    schemas = dict(sorted(Counter(entity["schema"] for entity in entities).items()))
    skipped_count = sum(
        int(result.get("skipped_count") or 0) for result in mapping_results
    )
    coverage = _coverage(mapping_results)
    diagnostics_summary = _diagnostics_summary(diagnostics, parsed["diagnostics"])

    mapping_payload = {
        "schema_version": "frisket.followthemoney.export_mapping.v1",
        "source": dict(source),
        "mappings": _jsonable(mappings),
        "mapping_results": [
            {
                "mapping_index": result.get("mapping_index"),
                "schema": result.get("schema"),
                "rowset": result.get("rowset"),
                "entity_count": result.get("entity_count", 0),
                "skipped_count": result.get("skipped_count", 0),
                "coverage": result.get("coverage") or _zero_coverage(),
                "diagnostic_count": result.get("diagnostic_count", 0),
            }
            for result in mapping_results
        ],
    }
    source_refs_payload = {
        "schema_version": FOLLOWTHEMONEY_SOURCE_REFS_SCHEMA_VERSION,
        "media_policy": media_policy,
        "entry_count": len(source_entries),
        "entries": source_entries,
    }
    validation_report_payload = {
        "schema_version": FOLLOWTHEMONEY_VALIDATION_REPORT_SCHEMA_VERSION,
        "entity_stream_valid": not parsed["diagnostics"],
        "entity_count": len(entities),
        "skipped_count": skipped_count,
        "schemas": schemas,
        "diagnostic_count": len(diagnostics) + len(parsed["diagnostics"]),
        "diagnostics": diagnostics + parsed["diagnostics"],
        "diagnostics_summary": diagnostics_summary,
        "source_evidence_coverage": coverage,
    }

    artifact_payloads: dict[str, bytes] = {
        "entities": entity_stream.encode("utf-8"),
        "validation_report": _json_bytes(validation_report_payload),
        "mapping": _json_bytes(mapping_payload),
        "source_refs": _json_bytes(source_refs_payload),
    }
    artifact_stats = {
        name: _artifact_stat(name, content)
        for name, content in artifact_payloads.items()
    }
    manifest_payload = {
        "schema_version": FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION,
        "project_id": project_id,
        "media_policy": media_policy,
        "validate": validate,
        "entity_count": len(entities),
        "skipped_count": skipped_count,
        "schemas": schemas,
        "diagnostics": diagnostics_summary,
        "source_evidence_coverage": coverage,
        "rowsets": [_rowset_manifest_ref(rowset) for rowset in rowsets_by_key.values()],
        "artifacts": artifact_stats,
    }
    artifact_payloads["manifest"] = _json_bytes(manifest_payload)
    artifact_stats["manifest"] = _artifact_stat(
        "manifest", artifact_payloads["manifest"]
    )

    return {
        "schema_version": FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION,
        "artifacts": {
            name: {
                **artifact_stats[name],
                "content": artifact_payloads[name],
            }
            for name in FOLLOWTHEMONEY_EXPORT_FILENAMES
        },
        "manifest": manifest_payload,
        "validation_report": validation_report_payload,
        "mapping": mapping_payload,
        "source_refs": source_refs_payload,
        "entity_count": len(entities),
        "skipped_count": skipped_count,
        "schemas": schemas,
        "diagnostics": diagnostics_summary,
        "source_evidence_coverage": coverage,
        "byte_count": sum(len(content) for content in artifact_payloads.values()),
    }


def followthemoney_rowset_key(rowset: Any) -> str:
    if not isinstance(rowset, Mapping):
        return "invalid:0"
    return f"{rowset.get('kind')}:{rowset.get('sheet_id')}"


def _artifact_stat(name: str, content: bytes) -> dict[str, Any]:
    return {
        "filename": FOLLOWTHEMONEY_EXPORT_FILENAMES[name],
        "content_type": FOLLOWTHEMONEY_EXPORT_CONTENT_TYPES[name],
        "byte_count": len(content),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


def _source_entries_for_mapping(
    *,
    rowset_payload: Mapping[str, Any],
    mapping_spec: Mapping[str, Any],
    mapping_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records_by_row_ref = {
        _stable_key(record.get("row_ref")): record
        for record in rowset_payload.get("records") or []
        if isinstance(record, Mapping)
    }
    source_ref_spec = mapping_spec.get("source_refs")
    if not isinstance(source_ref_spec, Mapping):
        source_ref_spec = {}
    entries: list[dict[str, Any]] = []
    for mapped in mapping_result.get("rows") or []:
        if not isinstance(mapped, Mapping):
            continue
        row_ref = mapped.get("row_ref")
        record = records_by_row_ref.get(_stable_key(row_ref), {})
        values = record.get("values") if isinstance(record, Mapping) else {}
        values = values if isinstance(values, Mapping) else {}
        source_urls = _string_values(values.get(source_ref_spec.get("url_column")))
        blob_hashes = _string_values(
            values.get(source_ref_spec.get("blob_hash_column"))
        )
        evidence_refs = list(mapped.get("evidence_refs") or [])
        evidence_refs.extend(
            _evidence_values(values.get(source_ref_spec.get("evidence_column")))
        )
        entries.append(
            {
                "entity_id": mapped.get("entity_id"),
                "schema": mapped.get("schema"),
                "row_ref": row_ref,
                "source_row_ref": mapped.get("source_row_ref"),
                "lineage": mapped.get("lineage"),
                "source_urls": sorted(set(source_urls)),
                "blob_hashes": sorted(set(blob_hashes)),
                "evidence_refs": _dedupe_refs(evidence_refs),
                "property_source_refs": list(mapped.get("source_refs") or []),
            }
        )
    return sorted(
        entries,
        key=lambda item: (
            str(item.get("schema") or ""),
            str(item.get("entity_id") or ""),
            _stable_key(item.get("row_ref")),
        ),
    )


def _coverage(mapping_results: list[Mapping[str, Any]]) -> dict[str, Any]:
    row_count = 0
    entity_count = 0
    skipped_count = 0
    rows_with_source_refs = 0
    rows_with_evidence_refs = 0
    for result in mapping_results:
        coverage = result.get("coverage") if isinstance(result, Mapping) else None
        if not isinstance(coverage, Mapping):
            continue
        row_count += int(coverage.get("row_count") or 0)
        entity_count += int(coverage.get("entity_count") or 0)
        skipped_count += int(coverage.get("skipped_count") or 0)
        rows_with_source_refs += int(coverage.get("rows_with_source_refs") or 0)
        rows_with_evidence_refs += int(coverage.get("rows_with_evidence_refs") or 0)
    return {
        "row_count": row_count,
        "entity_count": entity_count,
        "skipped_count": skipped_count,
        "rows_with_source_refs": rows_with_source_refs,
        "rows_with_evidence_refs": rows_with_evidence_refs,
        "source_ref_coverage": _ratio(rows_with_source_refs, row_count),
        "evidence_ref_coverage": _ratio(rows_with_evidence_refs, row_count),
    }


def _diagnostics_summary(
    mapping_diagnostics: list[Mapping[str, Any]],
    validation_diagnostics: list[Mapping[str, Any]],
) -> dict[str, Any]:
    all_diagnostics = [*mapping_diagnostics, *validation_diagnostics]
    by_code = Counter(str(item.get("code") or "unknown") for item in all_diagnostics)
    by_severity = Counter(
        str(item.get("severity") or "unknown") for item in all_diagnostics
    )
    return {
        "count": len(all_diagnostics),
        "by_code": dict(sorted(by_code.items())),
        "by_severity": dict(sorted(by_severity.items())),
    }


def _rowset_manifest_ref(rowset: Mapping[str, Any]) -> dict[str, Any]:
    columns = rowset.get("columns") or []
    row_refs = rowset.get("row_refs") or []
    rowset_payload = {
        "rowset": rowset.get("rowset"),
        "row_refs": row_refs,
        "columns": [
            column.get("name")
            for column in columns
            if isinstance(column, Mapping) and column.get("name") is not None
        ],
    }
    return {
        "rowset": rowset.get("rowset"),
        "sheet": rowset.get("sheet"),
        "row_count": len(row_refs),
        "column_count": len(columns),
        "rowset_hash": "sha256:" + _hash_json(rowset_payload),
    }


def _zero_coverage() -> dict[str, Any]:
    return {
        "row_count": 0,
        "entity_count": 0,
        "skipped_count": 0,
        "rows_with_source_refs": 0,
        "rows_with_evidence_refs": 0,
        "source_ref_coverage": 0.0,
        "evidence_ref_coverage": 0.0,
    }


def _unvalidated_parse() -> dict[str, Any]:
    return {
        "entities": [],
        "diagnostics": [],
        "entity_count": 0,
        "diagnostic_count": 0,
    }


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Mapping):
        candidates = [value.get("blob"), value.get("hash"), value.get("url")]
        return [str(item).strip() for item in candidates if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_string_values(item))
        return values
    text = str(value).strip()
    return [text] if text else []


def _evidence_values(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return _evidence_values(parsed)
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _dedupe_refs(refs: list[Any]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        deduped.setdefault(_stable_key(ref), dict(ref))
    return [deduped[key] for key in sorted(deduped)]


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _hash_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stable_key(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _jsonable(payload: Any) -> Any:
    return json.loads(_stable_key(payload))


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)
