"""Focused editor adapters for expected links, judgments, and saved comparisons."""

from __future__ import annotations

import uuid

from frisket.contracts.http.evaluation import (
    ComparisonCaptureRequest,
    EvaluationCellRequest,
    EvaluationJudgmentRequest,
    EvaluationRequest,
    ExpectedLinkInput,
    ExpectedProjectionRequest,
    ExpectedRuleChange,
    ExpectedRowsRequest,
    ShelfRename,
    ShelfSaveRequest,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.store import evaluation as store
from frisket.engine.store.transactions import read_transaction
from frisket.server.route_errors import RouteError
from frisket.server.services.action_compare import ActionCompareService
from frisket.server.services import evaluation_links as links
from frisket.server.services import evaluation_scoring as scoring
from frisket.server.services.evaluation_values import source_fingerprints
from frisket.server.services.evaluation_presentation import bound


def _body(model, value):
    return model.model_validate(value).model_dump(mode="json")


def _summary(item):
    return {
        key: item[key]
        for key in (
            "id",
            "label",
            "sheet_id",
            "action_id",
            "output_columns",
            "row_count",
            "saved_at",
            "kept",
        )
    }


class EvaluationService:
    def __init__(self, workspace, *, preview_reader=None):
        self.workspace = workspace
        self.preview_reader = preview_reader

    def _context(self, pid, basis, row_ids):
        return ActionCompareService(self.workspace).context(
            pid, {"source": basis, "sample_row_ids": row_ids}
        )

    def links(self, pid, sheet_id):
        project = self.workspace.get(pid)
        with read_transaction(project.db):
            links.sheet(project, sheet_id)
            return {
                "links": [
                    links.link_payload(project, item)
                    for item in store.links(project, sheet_id)
                ]
            }

    def preview_link(self, pid, raw):
        project = self.workspace.get(pid)
        with read_transaction(project.db):
            body = links.validate(project, raw, preview=True)
            return links.row_matches(project, body)[1]

    def save_link(self, pid, raw, link_id=None):
        project = self.workspace.get(pid)
        body = _body(ExpectedLinkInput, raw)
        with store.write_transaction(project):
            links.validate(project, body)
            if link_id is not None and store.link(project, link_id) is None:
                raise RouteError(404, "Expected answer link was not found")
            links.require_unambiguous(links.row_matches(project, body)[1])
            link_id = store.save_link(project, body, link_id)
        return links.link_payload(project, store.link(project, link_id))

    def delete_link(self, pid, link_id):
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            if store.link(project, link_id) is None:
                raise RouteError(404, "Expected answer link was not found")
            project.db.execute(
                "DELETE FROM expected_answer_links WHERE id=?", (link_id,)
            )

    def expected_rows(self, pid, raw):
        body = _body(ExpectedRowsRequest, raw)
        project = self.workspace.get(pid)
        with read_transaction(project.db):
            return links.checked_rows(project, body["link_id"], body["limit"])

    def projection(self, pid, sheet_id, row_ids):
        _body(ExpectedProjectionRequest, {"sheet_id": sheet_id, "row_ids": row_ids})
        project = self.workspace.get(pid)
        with read_transaction(project.db):
            return links.projection(project, sheet_id, row_ids)

    def set_rule(self, pid, expected_column_id, rule):
        rule = _body(ExpectedRuleChange, {"rule": rule})["rule"]
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            if (
                project.db.execute(
                    "SELECT 1 FROM expected_answer_links link, json_each(link.fields_json) field WHERE json_extract(field.value,'$.expected_column_id')=? LIMIT 1",
                    (expected_column_id,),
                ).fetchone()
                is None
            ):
                raise RouteError(404, "Expected answer column was not found")
            store.set_rule(project, expected_column_id, rule)
        return {"expected_column_id": expected_column_id, "rule": rule}

    def score(self, pid, raw):
        return bound(self._score_full(pid, raw))

    def _touch_comparison(self, pid, request, extra_ids=()):
        ids = [
            variant["source"]["saved_id"]
            for variant in request["variants"]
            if variant["source"]["kind"] == "saved"
        ]
        ids.extend(extra_ids)
        if ids:
            project = self.workspace.get(pid)
            with store.write_transaction(project):
                store.touch_saved(project, ids)

    def _score_full(self, pid, raw):
        request = _body(EvaluationRequest, raw)
        self._touch_comparison(pid, request)
        project = self.workspace.get(pid)
        with read_transaction(project.db):
            project, context, result = scoring.prepare(self, pid, request)
            current = source_fingerprints(
                project,
                result["sheet_id"],
                [column["column_id"] for column in context["source_columns"]],
                result["row_ids"],
            )
            return scoring.score_prepared(project, result, current)

    def judgment(self, pid, raw, *, actor="local:project"):
        body = _body(EvaluationJudgmentRequest, raw)
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            project, context, result = scoring.prepare(self, pid, body["comparison"])
            variant = next(
                (v for v in result["variants"] if v["key"] == body["variant_key"]), None
            )
            field = next(
                (
                    f
                    for f in (result["link"] or {}).get("fields", [])
                    if f["logical_key"] == body["logical_key"]
                ),
                None,
            )
            rid, key = str(body["row_id"]), body["logical_key"]
            if (
                variant is None
                or field is None
                or body["row_id"] not in result["row_ids"]
            ):
                raise RouteError(422, "Choose a checked comparison field and row")
            expected = result["expected"].get(rid, {}).get(key)
            cell = variant["rows"].get(rid, {}).get(key, {})
            if (
                expected is None
                or expected == ""
                or cell.get("state") not in {"value", "null"}
            ):
                raise RouteError(
                    422,
                    "Only attempted answers with an expected value can be scored by hand",
                )
            current = source_fingerprints(
                project,
                result["sheet_id"],
                [column["column_id"] for column in context["source_columns"]],
                [body["row_id"]],
            )[rid]
            if variant["source_fingerprints"].get(rid) != current:
                raise RouteError(
                    409, "Comparison source inputs changed; rerun before scoring"
                )
            identity = scoring.identity(
                result["sheet_id"],
                body["row_id"],
                field,
                current,
                expected,
                cell.get("value"),
            )
            store.save_judgment(project, identity, body["verdict"], actor)
            fingerprints = source_fingerprints(
                project,
                result["sheet_id"],
                [column["column_id"] for column in context["source_columns"]],
                result["row_ids"],
            )
            return bound(scoring.score_prepared(project, result, fingerprints))

    @staticmethod
    def _edit_cell(pid, project, row_id, column_id, value):
        """Use the same typed, undoable mutation as editing an ordinary grid cell."""
        result = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {"row_id": row_id, "column_id": column_id, "value": value}
                    ]
                },
                "idempotency_key": f"expected-answer:{uuid.uuid4()}",
            },
            project_id=pid,
        )
        if result.status != "completed":
            raise RouteError(
                422,
                result.errors[0].message
                if result.errors
                else "Expected answer could not be saved",
            )

    def expected_answer(self, pid, raw):
        from frisket.server.services.evaluation_answers import save_expected_answer

        return save_expected_answer(self, pid, raw)

    def capture(self, pid, raw):
        body = _body(ComparisonCaptureRequest, raw)
        target_id = body.pop("target_id")
        if len(body["variant_keys"]) != 1:
            raise RouteError(422, "Capture one variant chunk at a time")
        body["comparison"]["link_id"] = None
        body["comparison"]["variants"] = [
            variant
            for variant in body["comparison"]["variants"]
            if variant["key"] in body["variant_keys"]
        ]
        self._touch_comparison(
            pid, body["comparison"], [target_id] if target_id is not None else []
        )
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            store.purge_stale_captures(project)
        if target_id is None:
            return self.shelve(pid, body, kept=False)
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            project, _context, result = scoring.prepare(self, pid, body["comparison"])
            incoming = next(
                (
                    variant
                    for variant in result["variants"]
                    if variant["key"] == body["variant_keys"][0]
                ),
                None,
            )
            target = self._saved_full(pid, target_id)
            if target["kept"]:
                raise RouteError(409, "Shelved comparison results are immutable")
            if (
                incoming is None
                or target["sheet_id"] != result["sheet_id"]
                or target["action_id"] != result["action_id"]
                or scoring._signature(target["output_columns"])
                != scoring._signature(result["output_columns"])
                or scoring.settings_key(target["settings"])
                != scoring.settings_key(incoming["settings"])
            ):
                raise RouteError(409, "Captured comparison settings or outputs changed")
            rows = {rid: cells for rid, cells in incoming["rows"].items() if cells}
            if set(target["rows"]) & set(rows):
                raise RouteError(409, "Captured comparison chunks repeat a row")
            target["rows"].update(rows)
            target["source_fingerprints"].update(incoming["source_fingerprints"])
            project.db.execute(
                "UPDATE saved_compare_variants SET rows_json=?,fingerprints_json=? WHERE id=?",
                (
                    store.dump(target["rows"]),
                    store.dump(target["source_fingerprints"]),
                    target_id,
                ),
            )
            return {"items": [_summary(self._saved_full(pid, target_id))]}

    def shelve(self, pid, raw, *, kept=True):
        body = _body(ShelfSaveRequest, raw)
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            project, _context, result = scoring.prepare(self, pid, body["comparison"])
            by_key = {variant["key"]: variant for variant in result["variants"]}
            if set(body["variant_keys"]) - by_key.keys() or len(
                set(body["variant_keys"])
            ) != len(body["variant_keys"]):
                raise RouteError(422, "Choose unique comparison variants to shelve")
            items = []
            for key in body["variant_keys"]:
                variant = by_key[key]
                if variant["source"]["kind"] == "saved":
                    saved_id = variant["source"]["saved_id"]
                    if kept:
                        project.db.execute(
                            "UPDATE saved_compare_variants SET kept=1 WHERE id=?",
                            (saved_id,),
                        )
                else:
                    saved_id = store.save_variant(
                        project,
                        sheet_id=result["sheet_id"],
                        action_id=result["action_id"],
                        label=variant["label"],
                        output_columns=result["output_columns"],
                        settings=variant["settings"],
                        rows={
                            rid: cells
                            for rid, cells in variant["rows"].items()
                            if cells
                        },
                        source_fingerprints=variant["source_fingerprints"],
                        kept=kept,
                    )
                items.append(_summary(store.saved(project, saved_id)))
            return {"items": items}

    def shelf(self, pid, sheet_id, action_id):
        project = self.workspace.get(pid)
        links.sheet(project, sheet_id)
        rows = project.db.execute(
            "SELECT id FROM saved_compare_variants WHERE sheet_id=? AND action_id=? AND kept=1 ORDER BY id DESC",
            (sheet_id, action_id),
        ).fetchall()
        return {"items": [_summary(store.saved(project, row[0])) for row in rows]}

    def saved(self, pid, saved_id):
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            store.touch_saved(project, [saved_id])
        return bound(self._saved_full(pid, saved_id))

    def _saved_full(self, pid, saved_id):
        result = store.saved(self.workspace.get(pid), saved_id)
        if result is None:
            raise RouteError(404, "Saved comparison was not found")
        links.sheet(self.workspace.get(pid), result["sheet_id"])
        return result

    def rename_saved(self, pid, saved_id, label):
        label = _body(ShelfRename, {"label": label})["label"]
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            self.saved(pid, saved_id)
            project.db.execute(
                "UPDATE saved_compare_variants SET label=? WHERE id=?",
                (label, saved_id),
            )
        return _summary(self._saved_full(pid, saved_id))

    def discard_capture(self, pid, saved_id):
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            project.db.execute(
                "DELETE FROM saved_compare_variants WHERE id=? AND kept=0", (saved_id,)
            )

    def delete_saved(self, pid, saved_id):
        project = self.workspace.get(pid)
        with store.write_transaction(project):
            self.saved(pid, saved_id)
            project.db.execute(
                "DELETE FROM saved_compare_variants WHERE id=?", (saved_id,)
            )

    def cell(self, pid, raw):
        body = _body(EvaluationCellRequest, raw)
        result = self._score_full(pid, body["comparison"])
        rid, key = str(body["row_id"]), body["logical_key"]
        if body["row_id"] not in result["row_ids"] or key not in {
            column["logical_key"] for column in result["output_columns"]
        }:
            raise RouteError(422, "Choose a comparison row and output field")
        if body["variant_key"] == "expected":
            value = result["expected"].get(rid, {}).get(key)
            return {"state": "null" if value is None else "value", "value": value}
        variant = next(
            (
                variant
                for variant in result["variants"]
                if variant["key"] == body["variant_key"]
            ),
            None,
        )
        if variant is None:
            raise RouteError(404, "Comparison variant was not found")
        return variant["rows"].get(rid, {}).get(key, {"state": "missing"})

    def download_csv(self, pid, raw):
        from frisket.server.exports.evaluation import comparison_csv

        return comparison_csv(self._score_full(pid, raw))

    def download_settings(self, pid, raw):
        from frisket.server.exports.evaluation import comparison_settings

        return comparison_settings(self._score_full(pid, raw))

    def review_csv(self, pid, run_id):
        from frisket.server.exports.evaluation import review_csv

        project = self.workspace.get(pid)
        with read_transaction(project.db):
            return review_csv(project, run_id)
