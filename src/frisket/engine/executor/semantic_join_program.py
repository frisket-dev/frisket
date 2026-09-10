"""Typed row execution for the coupled semantic-join terminal."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from frisket.actions.types import Row, RowError, SheetColumnRef
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
from frisket.engine.executor.semantic_join_matcher import AdmittedSemanticMatcher
from frisket.engine.runner.row_inputs import row_source_snapshot
from frisket.ops.cost_source import (
    cost_estimate,
    free_local_estimate,
    unknown_cost_estimate,
)


def semantic_join_references(terminal, params: BaseModel):
    """Resolve declared roles, rejecting ambiguity rather than guessing names."""
    columns = terminal.child_source_columns(params)
    targets = []

    def visit(value, *, repeated=False):
        if isinstance(value, SheetColumnRef):
            targets.append(value)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                visit(getattr(value, name), repeated=repeated)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, repeated=True)

    visit(params)
    if len(targets) != 1:
        raise ValueError("semantic join needs exactly one target column")
    return (
        columns["source"],
        targets[0],
        tuple(ref for key, ref in columns.items() if key != "source"),
    )


class SemanticJoinSourceChanged(ValueError):
    """A completed match can no longer be published against its admitted input."""


def validate_semantic_join_pins(project, state, *, replaced_source_columns=()):
    for role in ("source", "target"):
        facts = state[role]
        columns = facts["columns"]
        expected = facts["snapshot"]
        if role == "source":
            if (
                project.visible_row_ids(facts["sheet_id"], facts["row_ids"])
                != facts["row_ids"]
            ):
                raise SemanticJoinSourceChanged(
                    "semantic join source rows changed after admission"
                )
            columns = {
                name: cid
                for name, cid in columns.items()
                if cid not in replaced_source_columns
            }
            expected = {
                **expected,
                "sources": [
                    source
                    for source in expected["sources"]
                    if source["column"]["id"] not in replaced_source_columns
                ],
            }
        current = row_source_snapshot(
            project,
            facts["sheet_id"],
            columns,
            row_ids=facts["row_ids"] if role == "source" else None,
        )
        if current != expected:
            raise SemanticJoinSourceChanged(
                f"semantic join {role} changed after admission"
            )


class _SemanticJoinProgram(_TypedMapRowsProgram):
    consumes_resolution = False
    cost_class = "metered"
    max_concurrency = 1
    defer_generation_seal = True

    def estimate(self, project, spec, rows, *, resolution=None):
        from frisket.ai.llm import estimate_tokens, model_pricing
        from frisket.semantic import embedder_is_remote

        state = spec["semantic_join"]
        model_id = state["embedding_model"]
        if not embedder_is_remote(model_id):
            return free_local_estimate()
        pricing = model_pricing(model_id)
        if pricing.price is None:
            return unknown_cost_estimate(requires_confirmation=True)
        source, _, _ = semantic_join_references(self._terminal, self._params)
        tokens = sum(
            estimate_tokens(str(value))
            for value in [
                *(row.get(source.name) for row in rows),
                *project.get_values(
                    state["target"]["sheet_id"], state["target_column"]["id"]
                ).values(),
            ]
            if value not in (None, "")
        )
        return cost_estimate(
            cost=round(tokens * pricing.price[0] / 1e6, 6),
            cost_source=pricing.cost_source,
            **({"pricing_key": pricing.pricing_key} if pricing.pricing_key else {}),
            requires_confirmation=True,
        )

    @asynccontextmanager
    async def execution_scope(self, spec, ctx, *, expected_rows):
        from frisket.ops.base import RecipeInvocationHalt
        from frisket.semantic import resolve_embedder

        state = spec["semantic_join"]
        try:
            validate_semantic_join_pins(ctx.project, state)
        except SemanticJoinSourceChanged as exc:
            raise RecipeInvocationHalt("promise_violation", str(exc)) from exc
        source, target, _ = semantic_join_references(self._terminal, self._params)
        backend = resolve_embedder(ctx.extras.get("router"), allow_remote=True)
        if backend is None or backend[1] != state["embedding_model"]:
            raise RecipeInvocationHalt(
                "promise_violation",
                "The admitted semantic embedding backend is no longer available; "
                "review the current backend before running again.",
            )
        self._matcher = AdmittedSemanticMatcher(
            target=target,
            values=tuple(
                sorted(
                    ctx.project.get_values(
                        target.sheet_id, state["target_column"]["id"]
                    ).items()
                )
            ),
            backend=backend,
            estimated_model=state["embedding_model"],
        )
        self._source_values = ctx.project.get_values(
            spec["sheet_id"],
            state["source"]["columns"][source.name],
            row_ids=state["source"]["row_ids"],
        )
        try:
            yield
        finally:
            self._matcher = None
            self._source_values = {}

    async def execute(self, row_values: dict[str, Any], spec, ctx):
        source, _, _ = semantic_join_references(self._terminal, self._params)
        row_id = ctx.extras["row_id"]
        if row_id not in self._source_values:
            raise ValueError("semantic join row was not admitted")
        expected = self._source_values[row_id]
        if row_values.get(source.name) != expected:
            raise ValueError("semantic join source cell changed after admission")
        matcher = self._matcher.bind_row(ctx, expected_source=expected)
        try:
            produced = self._terminal.handler(self._params, Row(row_values), matcher)
            if inspect.isawaitable(produced):
                produced = await produced
            matcher.validate_output(produced.output)
            if ctx.extras.get("preview") is not True:
                from frisket.engine.store.receipts import ReceiptStore
                from frisket.execution.attempt import attempt_in_scope

                attempt = attempt_in_scope(ctx.extras)
                ReceiptStore(ctx.project).record_semantic_match(
                    matcher.evidence(),
                    run_id=ctx.extras["run_id"],
                    writer_attempt_id=attempt.attempt_id
                    if attempt is not None
                    else None,
                    claim_token=ctx.extras["claim_token"],
                )
            return self._prepare_publication(produced)
        except RowError as exc:
            return self._row_error_publication(exc)
