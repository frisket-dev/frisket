"""Read-only FollowTheMoney entity matching helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION = "frisket.followthemoney.matching.v1"
FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION = (
    "frisket.followthemoney.match_review_plan.v1"
)

DEFAULT_MIN_SCORE = 0.55

DEFAULT_IDENTITY_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Person": (
        "name",
        "alias",
        "birthDate",
        "email",
        "phone",
        "address",
        "country",
        "nationality",
        "passportNumber",
        "idNumber",
        "taxNumber",
        "sourceUrl",
    ),
    "Company": (
        "name",
        "alias",
        "registrationNumber",
        "taxNumber",
        "vatCode",
        "jurisdiction",
        "address",
        "email",
        "phone",
        "sourceUrl",
    ),
    "Organization": (
        "name",
        "alias",
        "registrationNumber",
        "taxNumber",
        "vatCode",
        "jurisdiction",
        "address",
        "email",
        "phone",
        "sourceUrl",
    ),
    "LegalEntity": (
        "name",
        "alias",
        "registrationNumber",
        "taxNumber",
        "vatCode",
        "jurisdiction",
        "address",
        "email",
        "phone",
        "sourceUrl",
    ),
    "Asset": (
        "name",
        "registrationNumber",
        "serialNumber",
        "propertyId",
        "address",
        "isin",
        "sourceUrl",
    ),
    "Document": ("fileName", "title", "sourceUrl", "checksum", "mimeType"),
}

_GENERIC_IDENTITY_PROPERTIES = (
    "name",
    "alias",
    "email",
    "phone",
    "address",
    "registrationNumber",
    "propertyId",
    "idNumber",
    "taxNumber",
    "sourceUrl",
)

_NAME_PROPERTIES = frozenset({"name", "alias"})
_WEAK_PROPERTIES = frozenset(
    {"name", "alias", "birthDate", "country", "nationality", "jurisdiction"}
)
_STRONG_PROPERTY_PATTERNS = (
    "email",
    "phone",
    "registration",
    "number",
    "identifier",
    "id",
    "tax",
    "vat",
    "passport",
    "address",
    "property",
    "serial",
    "checksum",
    "url",
)


def match_followthemoney_entities(
    sources: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    identity_properties: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    """Return read-only candidate groups across mapped FtM rows.

    ``sources`` accepts results from :func:`map_rowset_to_entities` and, for
    imported FtM sheet plans/tests, rowset-like payloads whose records expose
    ``_ftm_id``/``_ftm_schema``/``_ftm_properties_json`` values. The helper never
    writes sheets or changes source rows.
    """

    items, diagnostics = _collect_items(sources)
    if min_score < 0 or min_score > 1:
        diagnostics.append(
            _diagnostic(
                "invalid_min_score",
                "warning",
                "min_score must be between 0 and 1; using default",
                min_score=min_score,
            )
        )
        min_score = DEFAULT_MIN_SCORE

    pairs: list[dict[str, Any]] = []
    compared_count = 0
    grouped_by_schema: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped_by_schema[item["schema"]].append(item)

    for schema_items in grouped_by_schema.values():
        for left_index, left in enumerate(schema_items):
            for right in schema_items[left_index + 1 :]:
                compared_count += 1
                pair = _score_pair(
                    left,
                    right,
                    properties=_properties_for_schema(
                        left["schema"], identity_properties
                    ),
                )
                if pair["score"] >= min_score:
                    pairs.append(pair)
                elif _is_name_only_pair(pair):
                    diagnostics.append(
                        _diagnostic(
                            "same_name_without_corroboration",
                            "info",
                            "same-name FtM entities were not grouped without corroborating fields",
                            schema=left["schema"],
                            entity_ids=[left["entity_id"], right["entity_id"]],
                            row_refs=[left.get("row_ref"), right.get("row_ref")],
                        )
                    )

    groups = _candidate_groups(items, pairs)
    return {
        "schema_version": FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION,
        "candidate_groups": groups,
        "diagnostics": diagnostics,
        "candidate_group_count": len(groups),
        "candidate_count": sum(len(group["candidates"]) for group in groups),
        "pair_count": len(pairs),
        "compared_count": compared_count,
    }


def plan_followthemoney_match_review_sheet(
    match_result: Mapping[str, Any],
    *,
    sheet_name: str = "FollowTheMoney entity match review",
    decisions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan ordinary review-sheet rows for explicit reviewer decisions.

    This is intentionally a plan/operation shape, not a materializer. Callers
    can apply it through the existing sheet APIs only after a user confirms a
    review workflow.
    """

    decision_by_group = dict(decisions or {})
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for group in match_result.get("candidate_groups") or []:
        if not isinstance(group, Mapping):
            continue
        group_id = str(group.get("group_id") or "")
        decision = decision_by_group.get(group_id, "pending")
        if decision not in {"pending", "accepted", "rejected"}:
            diagnostics.append(
                _diagnostic(
                    "invalid_review_decision",
                    "warning",
                    "review decision must be pending, accepted, or rejected",
                    group_id=group_id,
                    decision=decision,
                )
            )
            decision = "pending"
        rows.append(
            {
                "values": {
                    "match_group_id": group_id,
                    "decision": decision,
                    "score": group.get("score"),
                    "schema": group.get("schema"),
                    "candidate_count": len(group.get("candidates") or []),
                    "entity_ids": [
                        candidate.get("entity_id")
                        for candidate in group.get("candidates") or []
                    ],
                    "source_row_refs_json": group.get("source_row_refs") or [],
                    "evidence_refs_json": group.get("evidence_refs") or [],
                    "source_refs_json": group.get("source_refs") or [],
                    "reasons_json": group.get("reasons") or [],
                    "pair_scores_json": group.get("pair_scores") or [],
                    "review_notes": "",
                }
            }
        )

    columns = [
        {"name": "match_group_id", "type": "text", "hidden": False},
        {"name": "decision", "type": "text", "hidden": False},
        {"name": "score", "type": "number", "hidden": False},
        {"name": "schema", "type": "text", "hidden": False},
        {"name": "candidate_count", "type": "integer", "hidden": False},
        {"name": "entity_ids", "type": "json", "hidden": False},
        {"name": "source_row_refs_json", "type": "json", "hidden": False},
        {"name": "evidence_refs_json", "type": "json", "hidden": False},
        {"name": "source_refs_json", "type": "json", "hidden": False},
        {"name": "reasons_json", "type": "json", "hidden": False},
        {"name": "pair_scores_json", "type": "json", "hidden": True},
        {"name": "review_notes", "type": "text", "hidden": False},
    ]
    return {
        "schema_version": FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION,
        "sheet_name": sheet_name,
        "columns": columns,
        "rows": rows,
        "operations": [
            {
                "kind": "create_ordinary_sheet",
                "sheet_name": sheet_name,
                "columns": columns,
                "rows": rows,
            }
        ],
        "diagnostics": diagnostics,
        "row_count": len(rows),
    }


def _collect_items(
    sources: Iterable[Mapping[str, Any]] | Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    source_list = [sources] if isinstance(sources, Mapping) else sources
    for source_index, source in enumerate(source_list):
        if not isinstance(source, Mapping):
            diagnostics.append(
                _diagnostic(
                    "invalid_source",
                    "warning",
                    "FtM matching source must be a mapping",
                    source_index=source_index,
                )
            )
            continue
        source_items = _items_from_mapping_result(source, source_index=source_index)
        if not source_items:
            source_items = _items_from_ftm_rowset(source, source_index=source_index)
        if not source_items:
            diagnostics.append(
                _diagnostic(
                    "no_matchable_entities",
                    "info",
                    "FtM matching source exposed no matchable entities",
                    source_index=source_index,
                )
            )
            continue
        for item in source_items:
            key = (item["entity_id"], json.dumps(item.get("row_ref"), sort_keys=True))
            if key in seen:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_candidate",
                        "warning",
                        "duplicate FtM matching candidate ignored",
                        entity_id=item["entity_id"],
                        row_ref=item.get("row_ref"),
                    )
                )
                continue
            seen.add(key)
            items.append(item)
    items.sort(
        key=lambda item: (
            str(item.get("schema") or ""),
            str(item.get("entity_id") or ""),
            json.dumps(item.get("row_ref"), sort_keys=True),
        )
    )
    return items, diagnostics


def _items_from_mapping_result(
    source: Mapping[str, Any], *, source_index: int
) -> list[dict[str, Any]]:
    entities_by_id = {
        entity.get("id"): entity
        for entity in source.get("entities") or []
        if isinstance(entity, Mapping) and entity.get("id")
    }
    items: list[dict[str, Any]] = []
    for row_index, row in enumerate(source.get("rows") or []):
        if not isinstance(row, Mapping):
            continue
        entity_id = row.get("entity_id")
        entity = entities_by_id.get(entity_id)
        if entity is None:
            continue
        items.append(
            _item(
                entity_id=str(entity_id),
                schema=str(entity.get("schema") or row.get("schema") or ""),
                properties=_property_values(entity.get("properties") or {}),
                row_ref=row.get("row_ref"),
                source_row_ref=row.get("source_row_ref"),
                source_refs=row.get("source_refs") or entity.get("source_refs") or [],
                evidence_refs=row.get("evidence_refs") or [],
                raw_values=row.get("raw_values") or {},
                source_index=source_index,
                row_index=row_index,
            )
        )
    return [item for item in items if item["schema"] and item["entity_id"]]


def _items_from_ftm_rowset(
    source: Mapping[str, Any], *, source_index: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row_index, record in enumerate(source.get("records") or []):
        if not isinstance(record, Mapping):
            continue
        values = record.get("values") or {}
        if not isinstance(values, Mapping):
            continue
        entity_id = values.get("_ftm_id") or values.get("ftm_id")
        schema = values.get("_ftm_schema") or source.get("schema")
        properties = values.get("_ftm_properties_json")
        if not isinstance(properties, Mapping):
            properties = {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and not key.startswith("_ftm_")
            }
        if not entity_id or not schema:
            continue
        source_refs = values.get("_ftm_source_refs_json") or []
        evidence_refs: list[Any] = []
        for cell in record.get("cells") or []:
            if isinstance(cell, Mapping):
                evidence_refs.extend(cell.get("evidence_refs") or [])
        items.append(
            _item(
                entity_id=str(entity_id),
                schema=str(schema),
                properties=_property_values(properties),
                row_ref=record.get("row_ref"),
                source_row_ref=record.get("source_row_ref"),
                source_refs=source_refs,
                evidence_refs=evidence_refs,
                raw_values=values,
                source_index=source_index,
                row_index=row_index,
            )
        )
    return items


def _item(
    *,
    entity_id: str,
    schema: str,
    properties: dict[str, list[str]],
    row_ref: Any,
    source_row_ref: Any,
    source_refs: Iterable[Any],
    evidence_refs: Iterable[Any],
    raw_values: Mapping[str, Any],
    source_index: int,
    row_index: int,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "schema": schema,
        "properties": properties,
        "row_ref": row_ref,
        "source_row_ref": source_row_ref,
        "source_refs": list(source_refs or []),
        "evidence_refs": list(evidence_refs or []),
        "raw_values": dict(raw_values),
        "source_index": source_index,
        "row_index": row_index,
    }


def _property_values(properties: Mapping[str, Any]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for prop, value in properties.items():
        if not isinstance(prop, str):
            continue
        values: list[str] = []
        for item in _value_items(value):
            text = str(item).strip()
            if text and text not in values:
                values.append(text)
        if values:
            normalized[prop] = values
    return normalized


def _value_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[Any] = []
        iterable = sorted(value, key=str) if isinstance(value, set) else value
        for item in iterable:
            items.extend(_value_items(item))
        return items
    return [value]


def _properties_for_schema(
    schema: str, identity_properties: Mapping[str, Iterable[str]] | Iterable[str] | None
) -> tuple[str, ...]:
    if identity_properties is None:
        return DEFAULT_IDENTITY_PROPERTIES.get(schema, _GENERIC_IDENTITY_PROPERTIES)
    if isinstance(identity_properties, Mapping):
        configured = identity_properties.get(schema) or identity_properties.get("*")
        if configured is None:
            return DEFAULT_IDENTITY_PROPERTIES.get(schema, _GENERIC_IDENTITY_PROPERTIES)
        return tuple(str(prop) for prop in configured if str(prop))
    return tuple(str(prop) for prop in identity_properties if str(prop))


def _score_pair(
    left: Mapping[str, Any], right: Mapping[str, Any], *, properties: tuple[str, ...]
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    score = 0.0
    left_properties = left.get("properties") or {}
    right_properties = right.get("properties") or {}
    for prop in properties:
        left_values = {
            _identity_value(prop, value) for value in left_properties.get(prop, [])
        }
        right_values = {
            _identity_value(prop, value) for value in right_properties.get(prop, [])
        }
        matches = sorted((left_values & right_values) - {""})
        if not matches:
            continue
        weight = _property_weight(prop)
        score += weight
        reasons.append(
            {
                "code": "exact_property_match",
                "property": prop,
                "values": matches,
                "weight": weight,
            }
        )

    source_ref_matches = _source_ref_matches(left, right)
    if source_ref_matches:
        weight = 0.45
        score += weight
        reasons.append(
            {
                "code": "source_ref_match",
                "property": "source_refs",
                "values": source_ref_matches,
                "weight": weight,
            }
        )

    if reasons and not any(not _is_weak_reason(reason) for reason in reasons):
        score = min(score, 0.54)

    return {
        "left_entity_id": left["entity_id"],
        "right_entity_id": right["entity_id"],
        "score": round(min(score, 1.0), 6),
        "reasons": reasons,
    }


def _property_weight(prop: str) -> float:
    normalized = prop.lower()
    if normalized in {"name", "alias"}:
        return 0.35
    if normalized in {"birthdate", "country", "nationality", "jurisdiction"}:
        return 0.25 if normalized == "birthdate" else 0.1
    if any(pattern in normalized for pattern in _STRONG_PROPERTY_PATTERNS):
        if "url" in normalized:
            return 0.45
        if "address" in normalized:
            return 0.4
        return 0.55
    return 0.3


def _is_weak_reason(reason: Mapping[str, Any]) -> bool:
    if reason.get("code") != "exact_property_match":
        return False
    return str(reason.get("property") or "") in _WEAK_PROPERTIES


def _is_name_only_pair(pair: Mapping[str, Any]) -> bool:
    reasons = pair.get("reasons") or []
    return bool(reasons) and all(
        reason.get("code") == "exact_property_match"
        and reason.get("property") in _NAME_PROPERTIES
        for reason in reasons
    )


def _identity_value(prop: str, value: Any) -> str:
    text = str(value).strip()
    normalized_prop = prop.lower()
    if not text:
        return ""
    if "email" in normalized_prop:
        return text.lower()
    if "phone" in normalized_prop:
        digits = re.sub(r"\D+", "", text)
        if len(digits) == 11 and digits.startswith("1"):
            return digits[1:]
        return digits
    if "url" in normalized_prop:
        return _normalize_url(text)
    if normalized_prop in {"name", "alias", "address"}:
        return re.sub(r"\s+", " ", text.casefold())
    return text.casefold()


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().casefold()
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = re.sub(r"/+$", "", parts.path)
    return urlunsplit((scheme, netloc, path, "", ""))


def _source_ref_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    left_refs = _source_ref_keys(left)
    right_refs = _source_ref_keys(right)
    return sorted(left_refs & right_refs)


def _source_ref_keys(item: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for source_ref in item.get("source_refs") or []:
        if not isinstance(source_ref, Mapping):
            continue
        for key_name in ("export_ref", "stable_id", "url", "href"):
            value = source_ref.get(key_name)
            if isinstance(value, str) and value.strip():
                if key_name in {"url", "href"}:
                    keys.add(_normalize_url(value))
                else:
                    keys.add(value.strip())
    for value in (item.get("properties") or {}).get("sourceUrl", []):
        keys.add(_normalize_url(str(value)))
    return keys


def _candidate_groups(
    items: list[dict[str, Any]], pairs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    parent = {item["entity_id"]: item["entity_id"] for item in items}

    def find(entity_id: str) -> str:
        while parent[entity_id] != entity_id:
            parent[entity_id] = parent[parent[entity_id]]
            entity_id = parent[entity_id]
        return entity_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        parent[max(left_root, right_root)] = min(left_root, right_root)

    for pair in pairs:
        union(str(pair["left_entity_id"]), str(pair["right_entity_id"]))

    pairs_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entity_ids_by_root: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        root = find(str(pair["left_entity_id"]))
        pairs_by_root[root].append(pair)
        entity_ids_by_root[root].update(
            {str(pair["left_entity_id"]), str(pair["right_entity_id"])}
        )

    items_by_id = {item["entity_id"]: item for item in items}
    groups: list[dict[str, Any]] = []
    for root, entity_ids in entity_ids_by_root.items():
        candidates = [items_by_id[entity_id] for entity_id in sorted(entity_ids)]
        pair_scores = sorted(
            pairs_by_root[root],
            key=lambda pair: (
                -float(pair["score"]),
                str(pair["left_entity_id"]),
                str(pair["right_entity_id"]),
            ),
        )
        reasons = _merge_reasons(pair_scores)
        group_id = _group_id(candidates)
        groups.append(
            {
                "group_id": group_id,
                "schema": candidates[0]["schema"],
                "score": round(
                    sum(float(pair["score"]) for pair in pair_scores)
                    / len(pair_scores),
                    6,
                ),
                "max_pair_score": max(float(pair["score"]) for pair in pair_scores),
                "reasons": reasons,
                "candidates": [
                    _candidate_payload(candidate) for candidate in candidates
                ],
                "source_row_refs": _unique_json(
                    candidate.get("source_row_ref") or candidate.get("row_ref")
                    for candidate in candidates
                ),
                "evidence_refs": _unique_json(
                    evidence
                    for candidate in candidates
                    for evidence in candidate.get("evidence_refs") or []
                ),
                "source_refs": _unique_json(
                    source_ref
                    for candidate in candidates
                    for source_ref in candidate.get("source_refs") or []
                ),
                "pair_scores": pair_scores,
            }
        )
    groups.sort(key=lambda group: (-float(group["score"]), group["group_id"]))
    return groups


def _candidate_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": candidate.get("entity_id"),
        "schema": candidate.get("schema"),
        "row_ref": candidate.get("row_ref"),
        "source_row_ref": candidate.get("source_row_ref"),
        "properties": candidate.get("properties") or {},
        "source_refs": candidate.get("source_refs") or [],
        "evidence_refs": candidate.get("evidence_refs") or [],
    }


def _merge_reasons(pairs: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in pairs:
        entity_ids = [pair.get("left_entity_id"), pair.get("right_entity_id")]
        for reason in pair.get("reasons") or []:
            if not isinstance(reason, Mapping):
                continue
            key = (str(reason.get("code") or ""), str(reason.get("property") or ""))
            entry = merged.setdefault(
                key,
                {
                    "code": key[0],
                    "property": key[1],
                    "values": [],
                    "candidate_entity_ids": [],
                },
            )
            for value in reason.get("values") or []:
                if value not in entry["values"]:
                    entry["values"].append(value)
            for entity_id in entity_ids:
                if entity_id not in entry["candidate_entity_ids"]:
                    entry["candidate_entity_ids"].append(entity_id)
    return sorted(merged.values(), key=lambda item: (item["code"], item["property"]))


def _group_id(candidates: list[Mapping[str, Any]]) -> str:
    material = json.dumps(
        [
            {
                "entity_id": candidate.get("entity_id"),
                "row_ref": candidate.get("row_ref"),
            }
            for candidate in candidates
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"ftm-match:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _unique_json(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _diagnostic(
    code: str,
    severity: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    diagnostic = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    diagnostic.update({key: value for key, value in extra.items() if value is not None})
    return diagnostic
