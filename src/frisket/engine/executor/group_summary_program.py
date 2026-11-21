"""Grouped message rendering and keyed publication on the ordinary row runner."""

import json
from typing import Any

from frisket.ops.base import ActionProgram, RenderedCall
from frisket.engine.store.keyed_updates import observe, reconcile_fields
from frisket.engine.store.runs import RunResultStore


class GroupSummaryProgram(ActionProgram):
    consumes_resolution = False
    cost_class = "metered"

    def __init__(self, params, publication):
        super().__init__(
            name=params.action_kind,
            version="group_summary.v1",
            keep_zero_success_generated_columns=True,
            # Preserve ordered, incremental group publication and bound one
            # potentially large group prompt in memory at a time.
            max_concurrency=1,
            disable_failure_halt=True,
        )
        self._params = params
        self.publication = publication
        self.group_keys: dict[int, Any] = {}

    def source_columns(self, spec):
        return []

    def output_fields(self, spec):
        return [
            {
                "key": "summary",
                "name": self._params.summary_column_name,
                "column_type": "text",
            }
        ]

    def render(self, values, spec):
        return RenderedCall(messages=values["messages"], schema=None)

    def model_response_data(self, response, spec):
        return {
            self._params.summary_column_name: response.content
            if response.content is not None
            else json.dumps(response.data or {}, sort_keys=True)
        }

    def write_result_evidence(self, project, spec, *, batch, **context):
        """Apply refresh policy in the runner's result publication transaction."""
        publication = self.publication
        publication._require_writer()
        for result in batch:
            row_id, column_id = result["row_id"], result["column_id"]
            value, error = result.get("value"), result.get("error")
            if self._params.refresh_target:
                current, baseline, hidden = publication.before[row_id]
                field = str(column_id)
                changes, restore = reconcile_fields(
                    current,
                    {field: value},
                    baseline,
                    self._params.refresh_target.policy,
                    deleted=hidden,
                )
                if restore:
                    from frisket.engine.store.derived_updates import (
                        restore_derived_rows,
                    )

                    restore_derived_rows(
                        project.db,
                        op_id=context["op_id"],
                        sheet_id=context["sheet_id"],
                        row_ids=[row_id],
                    )
                if field not in changes:
                    result["publication_effect"] = "retain"
                    RunResultStore(project).retain_unpublished_result(
                        context["run_id"], row_id, column_id
                    )
            if not error:
                observe(
                    project.db,
                    relationship_id=publication.config["relationship_id"],
                    row_id=row_id,
                    key=self.group_keys[row_id],
                    incoming={str(column_id): value},
                )
