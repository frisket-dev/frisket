"""Typed row execution for the coupled semantic-join terminal."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from frisket.actions.core import MapRows
from frisket.actions.types import Row, RowError, RowResult, SheetColumnRef
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
from frisket.engine.executor.semantic_join_matcher import AdmittedSemanticMatcher
from frisket.engine.runner.row_inputs import RowInputValues, row_source_snapshot
from frisket.engine.store.writer_fence import WriterFence
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


def _require_writer_attempt_id(extras: dict[str, Any]) -> str:
    from frisket.execution.attempt import StaleAttemptWriter, attempt_in_scope

    attempt = attempt_in_scope(extras)
    if attempt is None:
        raise StaleAttemptWriter("semantic join requires an active attempt writer")
    return attempt.attempt_id


class _SemanticJoinProgram(_TypedMapRowsProgram):
    consumes_resolution = False
    cost_class = "metered"
    max_concurrency = 1
    defer_generation_seal = True

    def execution_source_binding(self, spec):
        from frisket.engine.store.source_dependencies import execution_source_binding

        sides = [spec["semantic_join"][role] for role in ("source", "target")]
        return execution_source_binding(
            sheet_ids=[side["sheet_id"] for side in sides],
            column_ids=[
                column for side in sides for column in side["columns"].values()
            ],
        )

    def estimate(self, project, spec, rows, *, resolution=None):
        from frisket.ai.llm import estimate_tokens, model_pricing
        from frisket.ai.embeddings.backends import embedder_is_remote

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
        from frisket.ops.base import ActionProgramInvocationHalt
        from frisket.ai.embeddings.backends import resolve_embedder
        from frisket.engine.store.consumed_sources import (
            read_consumed_source,
            store_capture,
        )

        state = spec["semantic_join"]
        try:
            validate_semantic_join_pins(ctx.project, state)
        except SemanticJoinSourceChanged as exc:
            raise ActionProgramInvocationHalt("promise_violation", str(exc)) from exc
        source, target, _ = semantic_join_references(self._terminal, self._params)
        backend = resolve_embedder(ctx.extras.get("router"), allow_remote=True)
        if backend is None or backend[1] != state["embedding_model"]:
            raise ActionProgramInvocationHalt(
                "promise_violation",
                "The admitted semantic embedding backend is no longer available; "
                "review the current backend before running again.",
            )
        _, target_values, self._target_capture = read_consumed_source(
            ctx.project, target.sheet_id, [state["target_column"]["id"]]
        )
        if ctx.extras.get("preview") is not True:
            from frisket.engine.store.output_claims import OutputColumnClaimStore

            writer_attempt_id = _require_writer_attempt_id(ctx.extras)
            with ctx.project.transaction_scope():
                OutputColumnClaimStore.require_current_writer(
                    ctx.project.db,
                    fence=WriterFence.claimed(
                        run_id=ctx.extras["run_id"],
                        writer_attempt_id=writer_attempt_id,
                        claim_token=ctx.extras["claim_token"],
                    ),
                )
                store_capture(
                    ctx.project,
                    self._target_capture,
                    owner_op_id=ctx.project.db.execute(
                        "SELECT op_id FROM runs WHERE id=?", (ctx.extras["run_id"],)
                    ).fetchone()[0],
                    output_sheet_id=spec["sheet_id"],
                    run_id=ctx.extras["run_id"],
                    shared=True,
                )
        self._matcher = AdmittedSemanticMatcher(
            target=target,
            values=tuple(sorted(target_values[state["target_column"]["id"]].items())),
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
            self._target_capture = None

    async def execute(self, row_values: dict[str, Any], spec, ctx):
        source, _, _ = semantic_join_references(self._terminal, self._params)
        row_id = ctx.extras["row_id"]
        if row_id not in self._source_values:
            raise ValueError("semantic join row was not admitted")
        expected = self._source_values[row_id]
        if row_values.get(source.name) != expected:
            raise ValueError("semantic join source cell changed after admission")
        assert self._matcher is not None
        matcher = self._matcher.bind_row(ctx, expected_source=expected)
        try:
            assert isinstance(self._terminal, MapRows)
            produced = self._terminal.handler(self._params, Row(row_values), matcher)
            if inspect.isawaitable(produced):
                produced = await produced
            assert isinstance(produced, RowResult)
            matcher.validate_output(produced.output)
            if ctx.extras.get("preview") is not True:
                from frisket.engine.store.receipts import ReceiptStore

                writer_attempt_id = _require_writer_attempt_id(ctx.extras)
                ReceiptStore(ctx.project).record_semantic_match(
                    matcher.evidence(),
                    fence=WriterFence.claimed(
                        run_id=ctx.extras["run_id"],
                        writer_attempt_id=writer_attempt_id,
                        claim_token=ctx.extras["claim_token"],
                    ),
                )
            publication = self._prepare_publication(produced)
            if ctx.extras.get("preview") is not True:
                from frisket.engine.store.consumed_sources import capture_fingerprint

                assert isinstance(row_values, RowInputValues)
                assert isinstance(row_values.consumed_capture, dict)
                assert self._target_capture is not None
                # The corpus is stored once at the actual read. Each returned
                # row/checkpoint refers to the corpus it really matched, even
                # when a resumed run later reads an equal-text contribution.
                row_values.consumed_capture["shared_inputs"] = [
                    capture_fingerprint(self._target_capture)
                ]
            return publication
        except RowError as exc:
            return self._row_error_publication(exc)
