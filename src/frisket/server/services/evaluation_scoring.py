"""One binary scoring path for Compare, queues, rule previews, and downloads."""

from __future__ import annotations

from pydantic import ValidationError

from frisket.contracts.http.evaluation import EvaluationVariantSnapshot
from frisket.engine.evaluation_matching import match_answer
from frisket.engine.store import evaluation as store
from frisket.server.route_errors import RouteError
from frisket.server.services import evaluation_links as links
from frisket.server.services.evaluation_values import (
    current_rows,
    source_fingerprints,
    value_hash,
)

RULES = ("exact", "ignore_case", "number", "date", "set", "hand")


def identity(sheet_id, row_id, field, fingerprint, expected, answer):
    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "expected_column_id": field["expected_column_id"],
        "output_column_id": field["output_column_id"],
        "source_fingerprint": fingerprint,
        "expected_hash": value_hash(expected),
        "answer_hash": value_hash(answer),
    }


def aggregate(verdicts, states, checked, rule):
    attempted = sum(verdict not in {"unchecked", "not_run"} for verdict in verdicts)
    matches, misses = verdicts.count("match"), verdicts.count("miss")
    scored = matches + misses
    remaining = verdicts.count("unscored")
    return {
        "checked": checked,
        "attempted": attempted,
        "matches": matches,
        "misses": misses,
        "failed": sum(
            state == "error" and verdict == "miss"
            for state, verdict in zip(states, verdicts, strict=True)
        ),
        "scored": scored,
        "remaining": remaining,
        "accuracy": matches / scored if scored else None,
        "coverage": attempted / checked if checked else 0.0,
    }


def _signature(columns):
    return sorted(
        (
            column["logical_key"],
            ("column", column["column_id"])
            if column.get("column_id") is not None
            else ("name", column["current_name"]),
        )
        for column in columns
    )


def settings_key(settings):
    if not isinstance(settings, dict):
        return value_hash(settings)
    return value_hash(
        {
            key: value
            for key, value in settings.items()
            if key
            not in {"scope", "idempotency_key", "confirmation", "confirmed", "consent"}
        }
    )


def _variant(service, pid, project, context, request, variant):
    row_ids = request["row_ids"]
    sheet_id = context["sheet"]["id"]
    source = variant["source"]
    action_id = context["draft"]["action_id"]
    outputs = context["output_columns"]
    if source["kind"] == "current":
        rows = current_rows(project, sheet_id, outputs, row_ids)
        fingerprints = source_fingerprints(
            project,
            sheet_id,
            [column["column_id"] for column in context["source_columns"]],
            row_ids,
        )
        settings = context["draft"]
    else:
        if source["kind"] == "saved":
            chunks = [service._saved_full(pid, source["saved_id"])]
        else:
            if service.preview_reader is None:
                raise RouteError(409, "Comparison preview is unavailable")
            if len(set(source["preview_ids"])) != len(source["preview_ids"]):
                raise RouteError(422, "Comparison preview chunks must be unique")
            chunks = [
                service.preview_reader(pid, preview_id)
                for preview_id in source["preview_ids"]
            ]
        rows, fingerprints, settings = {}, {}, None
        for chunk in chunks:
            try:
                chunk = EvaluationVariantSnapshot.model_validate(
                    {
                        key: chunk[key]
                        for key in (
                            "sheet_id",
                            "action_id",
                            "output_columns",
                            "settings",
                            "rows",
                            "source_fingerprints",
                        )
                    }
                ).model_dump(mode="json", exclude_none=True)
            except (KeyError, TypeError, ValidationError) as exc:
                raise RouteError(
                    409, "Comparison result snapshot is no longer valid"
                ) from exc
            if (
                chunk["sheet_id"] != sheet_id
                or chunk["action_id"] != action_id
                or _signature(chunk["output_columns"]) != _signature(outputs)
            ):
                raise RouteError(
                    409, "Comparison variant action or output schema changed"
                )
            if set(rows) & set(chunk["rows"]):
                raise RouteError(422, "Comparison preview chunks repeat a row")
            rows.update(chunk["rows"])
            fingerprints.update(chunk["source_fingerprints"])
            if settings is not None and settings_key(settings) != settings_key(
                chunk["settings"]
            ):
                raise RouteError(
                    409, "Comparison chunks use different variant settings"
                )
            settings = chunk["settings"]
        rows = {str(row_id): rows.get(str(row_id), {}) for row_id in row_ids}
        fingerprints = {
            str(row_id): fingerprints[str(row_id)]
            for row_id in row_ids
            if str(row_id) in fingerprints
        }
        keys = {output["logical_key"] for output in outputs}
        for row_id, cells in rows.items():
            if set(cells) - keys:
                raise RouteError(409, "Comparison variant returned an unknown output")
            if cells and row_id not in fingerprints:
                raise RouteError(409, "Comparison variant has no source identity")
    return {
        "key": variant["key"],
        "label": variant["label"],
        "source": source,
        "settings": settings,
        "rows": rows,
        "source_fingerprints": fingerprints,
        "verdicts": {},
        "scores": {},
    }


def prepare(service, pid, request):
    project = service.workspace.get(pid)
    context = service._context(pid, request["basis"], request["row_ids"])
    sheet_id = context["sheet"]["id"]
    if not context.get("available", True):
        raise RouteError(409, "This action does not support expected-answer comparison")
    if set(project.visible_row_ids(sheet_id, request["row_ids"])) != set(
        request["row_ids"]
    ):
        raise RouteError(422, "Comparison rows must belong to the target sheet")
    outputs = [
        {key: item[key] for key in ("logical_key", "column_id", "current_name")}
        for item in context["output_columns"]
    ]
    link = None
    if request["link_id"] is not None:
        link = links.link_payload(project, store.link(project, request["link_id"]))
        if (
            link["target_sheet_id"] != sheet_id
            or link["action_id"] != context["draft"]["action_id"]
        ):
            raise RouteError(409, "Expected link belongs to another comparison")
        output_ids = {column["logical_key"]: column["column_id"] for column in outputs}
        if any(
            output_ids.get(field["logical_key"]) != field["output_column_id"]
            for field in link["fields"]
        ):
            raise RouteError(409, "Expected link output columns changed")
    expected_cells = (
        links.resolve(
            project,
            {
                key: value
                for key, value in link.items()
                if key not in {"id", "target_sheet_name", "expected_sheet_name"}
            },
            request["row_ids"],
        )
        if link
        else {str(row_id): {} for row_id in request["row_ids"]}
    )
    expected = {
        row_id: {key: cell["value"] for key, cell in cells.items()}
        for row_id, cells in expected_cells.items()
    }
    variants = [
        _variant(service, pid, project, context, request, variant)
        for variant in request["variants"]
    ]
    return (
        project,
        context,
        {
            "sheet_id": sheet_id,
            "action_id": context["draft"]["action_id"],
            "output_columns": outputs,
            "row_ids": request["row_ids"],
            "link": link,
            "expected": expected,
            "variants": variants,
            "best": {},
            "rule_previews": {},
        },
    )


def score_prepared(project, result, current_fingerprints):
    fields = (
        {field["logical_key"]: field for field in result["link"]["fields"]}
        if result["link"]
        else {}
    )
    row_ids = result["row_ids"]
    for variant in result["variants"]:
        captured = variant["source_fingerprints"]
        variant["stale_source_row_ids"] = [
            row_id
            for row_id in row_ids
            if str(row_id) in captured
            and captured[str(row_id)] != current_fingerprints.get(str(row_id))
        ]
    for output in result["output_columns"]:
        key = output["logical_key"]
        field = fields.get(key)
        checked = sum(
            result["expected"].get(str(row_id), {}).get(key) not in (None, "")
            for row_id in row_ids
        )
        projections = {rule: {} for rule in RULES}
        for variant in result["variants"]:
            by_rule, states = {rule: [] for rule in RULES}, []
            for row_id in row_ids:
                rid = str(row_id)
                cell = variant["rows"].get(rid, {}).get(key, {"state": "missing"})
                expected = result["expected"].get(rid, {}).get(key)
                state, answer = cell["state"], cell.get("value")
                hand = None
                if (
                    field
                    and rid in variant["source_fingerprints"]
                    and variant["source_fingerprints"][rid]
                    == current_fingerprints.get(rid)
                ):
                    hand = store.judgment(
                        project,
                        identity(
                            result["sheet_id"],
                            row_id,
                            field,
                            variant["source_fingerprints"][rid],
                            expected,
                            answer,
                        ),
                    )
                for rule in RULES:
                    by_rule[rule].append(
                        match_answer(
                            rule, expected, answer, state=state, hand_verdict=hand
                        )
                    )
                states.append(state)
                rule = field["rule"] if field else "exact"
                variant["verdicts"].setdefault(rid, {})[key] = by_rule[rule][-1]
            for rule in RULES:
                projections[rule][variant["key"]] = aggregate(
                    by_rule[rule], states, checked, rule
                )
            variant["scores"][key] = projections[field["rule"] if field else "exact"][
                variant["key"]
            ]
        result["rule_previews"][key] = projections if field else {}
        # Equal counts alone can hide disjoint coverage. Compare exact attempted/scored rows.
        scopes = []
        for variant in result["variants"]:
            scopes.append(
                tuple(
                    str(row_id)
                    for row_id in row_ids
                    if variant["verdicts"][str(row_id)][key] in {"match", "miss"}
                )
            )
        scores = [variant["scores"][key] for variant in result["variants"]]
        comparable = bool(
            scopes
            and scopes[0]
            and all(scope == scopes[0] for scope in scopes)
            and all(score["remaining"] == 0 for score in scores)
        )
        if comparable:
            best = max(score["accuracy"] for score in scores)
            result["best"][key] = [
                variant["key"]
                for variant in result["variants"]
                if variant["scores"][key]["accuracy"] == best
            ]
        else:
            result["best"][key] = []
    return result
