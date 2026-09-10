"""Run/result store for execution envelopes, row scopes, and model-call facts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from frisket.ai.llm.remediation import RESUMABLE_PROVIDER_ERROR_DETAILS
from frisket.ai.llm.types import validate_credential_source
from frisket.ai.models.metadata import (
    PROVIDER_COST_MAX_USD,
    PROVIDER_COST_TOTAL_MAX_USD,
    duration_ms_value,
    validate_provider_cost,
)
from frisket.engine.store.credentials import ProviderSpendDelta
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointStore,
    is_operator_attestation,
    require_model_call_ids,
)
from frisket.redaction import redact_text

_RUN_SCOPE_INSERT_CHUNK_SIZE = 1000

# Shared effect-checkpoint families owned by this store:
# MapRunner row effects (group = run id, unit = row id; retire-on-consume)
# and the model-call checkpoint shape (group = action kind, unit = the
# caller-computed checkpoint id; retired by the owning action's result
# transaction, e.g. cluster.values in action_families/entities.py).
ROW_EFFECT_CHECKPOINT_FAMILY = "row_effect"
MODEL_CALL_CHECKPOINT_FAMILY = "model_call"

# The run-detail error summary's group cap (distinct messages surfaced) and
# per-group row-id example cap. Small on purpose — this is a "why did it
# fail" glance, not the full per-row list (that stays behind the existing
# /actions/runs/{id}/rows?status=error on-demand endpoint).
ROW_ERROR_SUMMARY_MAX_GROUPS = 10
ROW_ERROR_SUMMARY_MAX_EXAMPLES = 5


@dataclass(frozen=True)
class RunResumeAdmission:
    """The outcome of one ``begin_run_resume`` attempt plus the pre-resume
    run state it observed under the admission transaction.

    ``admitted`` is the decision the runner keys on; the ``prior_*`` fields
    exist so a pre-dispatch refusal can revert the row exactly
    (``revert_run_resume``) instead of leaving a run stuck at "running" with
    zero rows dispatched. They are ``None`` only when there was nothing to
    observe (no such run)."""

    admitted: bool
    prior_status: str | None = None
    prior_finished_at: Any = None
    prior_params: Any = None
    # A resume is a new invocation decision, so it clears stale cooperative
    # cancel intent. A refused pre-dispatch resume restores this exact value.
    prior_cancel_requested_at: Any = None
    # Exact intent value admission left behind. A prepared queued run enters
    # this method already ``running``; preserving a request that raced its
    # dispatch prevents first-row startup from erasing live cancel intent.
    transient_cancel_requested_at: Any = None
    # The cleared halt columns are carried here so
    # ``revert_run_resume`` restores them the same way it restores ``params``.
    # The relocation, not a retirement: a refused resume must leave the run
    # indistinguishable from one never resumed, awaiting-reconsent markers
    # included.
    prior_halted_code: str | None = None
    prior_halted_reason: str | None = None
    # The exact transient params value written by this admission. Revert is
    # a compare-and-swap against this state, never an unconditional restore
    # over a later writer.
    transient_params: str | None = None


_ROW_ERROR_SELECT = (
    "SELECT res.run_id AS run_id, res.row_id AS row_id, res.error AS error, "
    "res.error_code AS error_code, res.outcome AS outcome, "
    "r.position AS row_position "
    "FROM results res "
    "JOIN columns c ON c.id = res.column_id "
    "JOIN rows r ON r.id = res.row_id "
    "LEFT JOIN run_scopes run_scope ON run_scope.run_id=res.run_id "
    "LEFT JOIN run_rows scoped_row ON scoped_row.run_id=res.run_id "
    "AND scoped_row.row_id=res.row_id "
)


def _group_row_errors(
    error_rows: list[Any],
    *,
    max_groups: int,
    max_examples: int,
) -> dict[str, Any] | None:
    """Groups raw per-cell error rows (one row per failed (row_id, column_id)
    pair, ordered by row_id then column position) into the run-detail error
    summary shape: distinct MESSAGE text is the grouping key (not error_code
    — a coarse code like 'model_error' would wrongly collapse unrelated
    failures together); each row_id contributes its FIRST error (the
    earliest-position column that has one) as its representative message.
    Returns None when nothing failed."""
    representative: dict[int, tuple[str, str | None, str | None, int]] = {}
    for row in error_rows:
        row_id = row["row_id"]
        if row_id in representative:
            continue
        representative[row_id] = (
            row["error"],
            row["error_code"],
            row["outcome"],
            row["row_position"],
        )
    if not representative:
        return None
    groups: dict[str, dict[str, Any]] = {}
    for row_id, (message, code, outcome, position) in representative.items():
        group = groups.setdefault(
            message,
            {
                "message": message,
                "count": 0,
                "code": code,
                "outcome": outcome,
                "rows": [],
            },
        )
        group["count"] += 1
        group["rows"].append((position, row_id))
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["message"]))
    out_groups = []
    for group in ordered[:max_groups]:
        rows_sorted = sorted(group["rows"])
        entry: dict[str, Any] = {
            "message": group["message"],
            "count": group["count"],
            "code": group["code"],
            # The taxonomy bucket the runner classified this failure into, and
            # whether that bucket is terminal (automation never retries it) —
            # the failure-triage UI groups and words its actions from these
            # instead of parsing error strings.
            "outcome": group["outcome"],
            "terminal": group["outcome"] in TERMINAL_FAILURE_OUTCOMES,
            "row_ids": [row_id for _position, row_id in rows_sorted[:max_examples]],
        }
        # Typed resumable provider codes carry their honest retry/resume
        # metadata from the ONE declaration
        # (frisket.llm.remediation.RESUMABLE_PROVIDER_ERROR_DETAILS), so the
        # public run status agrees with the receipt-level ActionError built
        # from the same rows (_resumable_provider_error).
        details = RESUMABLE_PROVIDER_ERROR_DETAILS.get(group["code"] or "")
        if details is not None:
            entry["details"] = dict(details)
        out_groups.append(entry)
    return {
        "total_failed_rows": len(representative),
        "groups": out_groups,
    }


# Result outcome taxonomy. `outcome` is the single
# classification axis for failed/done/pending; `error` is a display message only.
# Expected data failures stay failed/retryable, without treating a bad row as
# evidence of an unexpected systemic failure or hiding its diagnostic column.
EXPECTED_ROW_ERROR = "row_error"
FAILURE_OUTCOMES = ("model_error", "invalid_output", EXPECTED_ROW_ERROR)
# terminal = produced its final state, never re-run by backfill.
# `unverified_memory`: a produced answer the model gave from parametric memory
# with NO external source (research.answer), kept but marked -- terminal and
# NOT a failure ("mark, don't destroy"), so it neither nulls the value nor
# inflates failed_rows.
TERMINAL_OUTCOMES = ("ok", "empty", "withheld_unverified", "unverified_memory")
# terminal failure = failed (shown as a failure, counted in failed_rows) AND
# terminal (automation never retries it): an empty
# result over non-empty source is an honest failure, but whether a retry would
# fix it is not our judgment call, so only a deliberate user-initiated retry
# (run.backfill with explicit row_ids) re-runs such a row.
TERMINAL_FAILURE_OUTCOMES = ("empty_output",)


def outcome_sql_list(outcomes: tuple[str, ...]) -> str:
    """Inline SQL list body for an outcome tuple — trusted module constants
    only, never user input. Single source for the outcome buckets in SQL, so
    query sites cannot drift from the taxonomy above."""
    return ", ".join(f"'{outcome}'" for outcome in outcomes)


# "done" for run-completion / backfill scoping: terminal successes AND terminal
# failures — neither is ever re-run automatically.
_DONE_OUTCOMES_SQL = outcome_sql_list(TERMINAL_OUTCOMES + TERMINAL_FAILURE_OUTCOMES)
# "failed" for failed_rows accounting: auto-retryable failures AND terminal
# failures — an empty_output row is failed honestly, not silently green.
_FAILED_OUTCOMES_SQL = outcome_sql_list(FAILURE_OUTCOMES + TERMINAL_FAILURE_OUTCOMES)
# Produced cells still eligible for the human review queue (accept/reject/edit).
# `unverified_memory` is a kept answer with no source, so it is at least as
# review-worthy as an `ok` cell — never silently drop it from the queue. Single
# source the review-queue SQL sites import so they cannot drift from this set.
REVIEWABLE_OUTCOMES = ("ok", "empty", "unverified_memory")
REVIEWABLE_OUTCOMES_SQL = outcome_sql_list(REVIEWABLE_OUTCOMES)


def _result_outcome(r: dict[str, Any]) -> str:
    """The outcome a producer declared, or a derived one (transition shim, removed once
    every producer sets it explicitly): error -> model_error; value -> ok; else empty."""
    outcome = r.get("outcome")
    if outcome:
        return str(outcome)
    if r.get("error"):
        return "model_error"
    return "ok" if r.get("value") is not None else "empty"


def _safe_result_error(value: Any, error_code: object = None) -> str | None:
    """Write-boundary result sink: store a bounded display detail, not raw error text."""
    if value is None:
        return None
    return redact_text(value, max_chars=500)


class RunResultStore:
    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access.

        ``Project.db`` is deliberately per-thread (its docstring: a single
        shared sqlite3 connection races across threads — statement-cache
        reuse gives ``InterfaceError``/phantom empty fetches). Capturing it in
        ``__init__`` silently defeats that: the store would hand every thread
        the CONSTRUCTING thread's connection. The map runner's cancellation
        predicate is exactly such a caller — a recipe running under
        ``asyncio.to_thread`` calls ``cancelled()`` -> ``get_run()`` from a
        pool thread while the event-loop thread is mid-query on the same
        connection, and the run dies with ``sqlite3.InterfaceError: bad
        parameter or other API misuse``.
        """
        return self.project.db

    def start_run(
        self,
        op_id: int,
        sheet_id: int,
        action_kind: str,
        *,
        action_version: str = "1",
        model: str | None = None,
        prompt_hash: str | None = None,
        params: dict | None = None,
        total_rows: int = 0,
        cost_estimate: float | None = None,
        row_ids: list[int] | None = None,
        commit: bool = True,
    ) -> int:
        if action_kind != action_kind.strip() or "." not in action_kind:
            raise ValueError(f"action kind {action_kind!r} is not canonical")
        try:
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            cur = self.db.execute(
                "INSERT INTO runs (op_id, sheet_id, action_kind, action_version, "
                "model, prompt_hash, params, total_rows, "
                "cost_estimate) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    sheet_id,
                    action_kind,
                    action_version,
                    model,
                    prompt_hash,
                    json.dumps(params or {}, sort_keys=True),
                    total_rows,
                    cost_estimate,
                ),
            )
            run_id = int(cur.lastrowid)
            if row_ids is not None:
                self.record_run_row_scope(run_id, row_ids)
            if commit:
                self.db.commit()
            return run_id
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def repoint_run_for_result(
        self,
        run_id: int,
        *,
        sheet_id: int,
        params: dict[str, Any],
        total_rows: int,
        cost_estimate: float | None,
    ) -> None:
        """Repoint a pre-egress accounting run at its materialized result.

        A family that mints its run before the provider effect (so crash-window
        facts have a durable home) finalizes the SAME run against the summary
        sheet and final op spec instead of minting a second one. Joins the
        caller's open transaction; never commits."""

        cur = self.db.execute(
            "UPDATE runs SET sheet_id=?, params=?, total_rows=?, "
            "cost_estimate=? WHERE id=?",
            (
                sheet_id,
                json.dumps(params, sort_keys=True),
                total_rows,
                cost_estimate,
                run_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"run {run_id} vanished before the result write")

    def set_run_halt(
        self,
        run_id: int,
        halted_code: str | None,
        halted_reason: str | None,
        *,
        commit: bool = True,
    ) -> None:
        """Write both durable halt representations atomically.

        ``halted_reason``-without-``halted_code`` stays representable: the
        legacy consecutive-failure circuit breaker sets a reason with no typed
        code, and collapsing the two would reclassify its halts as typed
        recipe-invocation halts.
        """
        stored_code = halted_code
        stored_reason = halted_reason if halted_code is None else halted_reason or ""

        try:
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT params FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            try:
                params = json.loads(row["params"] or "{}") if row else {}
            except (TypeError, ValueError):
                params = {}
            if not isinstance(params, dict):
                params = {}
            if stored_code is not None:
                params["halted_code"] = stored_code
                params["halted_reason"] = stored_reason
            elif stored_reason is not None:
                params.pop("halted_code", None)
                params["halted_reason"] = stored_reason
            else:
                params.pop("halted_code", None)
                params.pop("halted_reason", None)
            self.db.execute(
                "UPDATE runs SET halted_code=?, halted_reason=? WHERE id=?",
                (stored_code, stored_reason, run_id),
            )
            self.db.execute(
                "UPDATE runs SET params=? WHERE id=?",
                (json.dumps(params, sort_keys=True), run_id),
            )
            if commit:
                self.db.commit()
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def set_worker_version(
        self, run_id: int, worker_version: str, *, commit: bool = True
    ) -> None:
        """Stamp the code identity of the worker claiming this run
        (worker-version-guard-v1). Called by the project.run/action.run
        handlers as soon as they claim a run, so even a run that fails
        immediately after still shows which code touched it."""
        self.db.execute(
            "UPDATE runs SET worker_version=? WHERE id=?",
            (worker_version, run_id),
        )
        if commit:
            self.db.commit()

    def set_total_rows(
        self, run_id: int, total_rows: int, *, commit: bool = True
    ) -> None:
        self.db.execute(
            "UPDATE runs SET total_rows=? WHERE id=?",
            (int(total_rows), run_id),
        )
        if commit:
            self.db.commit()

    def increment_total_rows(
        self, run_id: int, delta: int, *, commit: bool = True
    ) -> None:
        self.db.execute(
            "UPDATE runs SET total_rows = total_rows + ? WHERE id=?",
            (int(delta), run_id),
        )
        if commit:
            self.db.commit()

    def record_run_row_scope(self, run_id: int, row_ids: list[int]) -> None:
        """Record immutable run scope inside the caller's transaction."""
        from frisket.engine.store.result_generations import (
            GenerationStateError,
            ResultGenerationStore,
        )

        existing = self.run_row_scope_if_present(run_id)
        if ResultGenerationStore(self.project).bindings_for_run(run_id):
            requested = [int(row_id) for row_id in row_ids]
            if existing is not None and existing != requested:
                raise GenerationStateError(
                    f"generation-managed run {run_id} cannot change its row scope"
                )
            if existing is not None:
                return
        self.db.execute(
            "INSERT INTO run_scopes (run_id, row_count) VALUES (?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET row_count=excluded.row_count",
            (run_id, len(row_ids)),
        )
        self.db.execute("DELETE FROM run_rows WHERE run_id=?", (run_id,))
        rows = list(enumerate(row_ids))
        for start in range(0, len(rows), _RUN_SCOPE_INSERT_CHUNK_SIZE):
            chunk = rows[start : start + _RUN_SCOPE_INSERT_CHUNK_SIZE]
            self.db.executemany(
                "INSERT INTO run_rows (run_id, row_id, position) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id, row_id) DO UPDATE SET position=excluded.position",
                [(run_id, row_id, index) for index, row_id in chunk],
            )

    def run_row_scope(self, run_id: int) -> list[int]:
        return [
            int(row["row_id"])
            for row in self.db.execute(
                "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY position",
                (run_id,),
            )
        ]

    def run_row_scope_count(self, run_id: int) -> int | None:
        row = self.db.execute(
            "SELECT row_count FROM run_scopes WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return int(row["row_count"])

    def run_row_scope_if_present(self, run_id: int) -> list[int] | None:
        marker = self.db.execute(
            "SELECT row_count FROM run_scopes WHERE run_id=?", (run_id,)
        ).fetchone()
        if marker is None:
            return None
        return self.run_row_scope(run_id)

    def pending_run_row_scope_count(
        self, run_id: int, output_column_ids: list[int]
    ) -> int:
        if not output_column_ids:
            row = self.db.execute(
                "SELECT COUNT(*) AS count FROM run_rows WHERE run_id=?", (run_id,)
            ).fetchone()
            return int(row["count"])
        completion = self._result_completion_predicate(
            run_id, output_column_ids, alias="res"
        )
        if len(output_column_ids) == 1:
            row = self.db.execute(
                "SELECT COUNT(*) AS count FROM run_rows rr "
                "WHERE rr.run_id=? AND NOT EXISTS ("
                "SELECT 1 FROM results res "
                "WHERE res.run_id=? AND res.row_id=rr.row_id "
                "AND res.column_id=? "
                f"AND {completion}"
                ")",
                (run_id, run_id, output_column_ids[0]),
            ).fetchone()
            return int(row["count"])
        placeholders = ",".join("?" for _ in output_column_ids)
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM run_rows rr "
            "WHERE rr.run_id=? AND NOT EXISTS ("
            "SELECT 1 FROM results res "
            "WHERE res.run_id=? AND res.row_id=rr.row_id "
            f"AND res.column_id IN ({placeholders}) "
            f"AND {completion} "
            "GROUP BY res.row_id "
            "HAVING COUNT(DISTINCT res.column_id)=?"
            ")",
            (run_id, run_id, *output_column_ids, len(output_column_ids)),
        ).fetchone()
        return int(row["count"])

    def pending_run_row_scope_page_after(
        self,
        run_id: int,
        output_column_ids: list[int],
        *,
        after_position: int = -1,
        limit: int = 500,
    ) -> list[tuple[int, int]]:
        limit = max(1, int(limit))
        after_position = max(-1, int(after_position))
        params: list[Any] = [run_id, after_position]
        done_filter = ""
        completion = self._result_completion_predicate(
            run_id, output_column_ids, alias="res"
        )
        if len(output_column_ids) == 1:
            done_filter = (
                "AND NOT EXISTS ("
                "SELECT 1 FROM results res "
                "WHERE res.run_id=? AND res.row_id=rr.row_id "
                "AND res.column_id=? "
                f"AND {completion}"
                ") "
            )
            params.extend([run_id, output_column_ids[0]])
        elif output_column_ids:
            placeholders = ",".join("?" for _ in output_column_ids)
            done_filter = (
                "AND NOT EXISTS ("
                "SELECT 1 FROM results res "
                "WHERE res.run_id=? AND res.row_id=rr.row_id "
                f"AND res.column_id IN ({placeholders}) "
                f"AND {completion} "
                "GROUP BY res.row_id "
                "HAVING COUNT(DISTINCT res.column_id)=?"
                ") "
            )
            params.extend([run_id, *output_column_ids, len(output_column_ids)])
        params.append(limit)
        rows = self.db.execute(
            "SELECT rr.position, rr.row_id FROM run_rows rr "
            "WHERE rr.run_id=? AND rr.position>? "
            f"{done_filter}"
            "ORDER BY rr.position LIMIT ?",
            params,
        ).fetchall()
        return [(int(row["position"]), int(row["row_id"])) for row in rows]

    def has_run_row_scope(self, run_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM run_scopes WHERE run_id=? LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )

    def completed_result_row_ids(
        self, run_id: int, output_column_ids: list[int]
    ) -> set[int]:
        if not output_column_ids:
            return set()
        placeholders = ",".join("?" for _ in output_column_ids)
        completion = self._result_completion_predicate(
            run_id, output_column_ids, alias="results"
        )
        return {
            int(row["row_id"])
            for row in self.db.execute(
                "SELECT row_id FROM results "
                f"WHERE run_id=? AND column_id IN ({placeholders}) "
                f"AND {completion} "
                "GROUP BY row_id HAVING COUNT(DISTINCT column_id)=?",
                (run_id, *output_column_ids, len(output_column_ids)),
            )
        }

    def _result_completion_predicate(
        self, run_id: int, output_column_ids: list[int], *, alias: str
    ) -> str:
        from frisket.engine.store.result_generations import ResultGenerationStore

        requested = {int(column_id) for column_id in output_column_ids}
        managed = {
            binding.column_id
            for binding in ResultGenerationStore(self.project).bindings_for_run(run_id)
        }
        if requested and requested <= managed:
            return f"{alias}.publication_effect IS NOT NULL"
        return f"{alias}.outcome IN ({_DONE_OUTCOMES_SQL})"

    def result_rows_for_column(self, run_id: int, column_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT row_id, value, justification FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id",
            (run_id, column_id),
        ).fetchall()

    def clear_result_justification(
        self,
        run_id: int,
        row_id: int,
        column_id: int,
        *,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            "UPDATE results SET justification=NULL "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_id, column_id),
        )
        if commit:
            self.db.commit()

    def fail_result_value(
        self,
        run_id: int,
        row_id: int,
        column_id: int,
        error: str,
        *,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            "UPDATE results SET value=NULL, error=?, justification=NULL, "
            "outcome='model_error', publication_effect='publish_error' "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (_safe_result_error(error, "model_error"), run_id, row_id, column_id),
        )
        if commit:
            self.db.commit()

    def withhold_result_value(
        self,
        run_id: int,
        row_id: int,
        column_id: int,
        reason: str,
        *,
        commit: bool = True,
    ) -> None:
        """Deliberately keep a value out on policy (e.g. map.extract citation grounding):
        null the value + record the human-readable reason, but mark it terminal
        `withheld_unverified` -- NOT a failure. So it does not inflate failed_rows and is
        not re-run by backfill (the run-completion queries treat it as done)."""
        self.db.execute(
            "UPDATE results SET value=NULL, error=?, justification=NULL, "
            "outcome='withheld_unverified', publication_effect='publish_error' "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (_safe_result_error(reason, "withheld_result"), run_id, row_id, column_id),
        )
        if commit:
            self.db.commit()

    def set_result_review_state(
        self,
        run_id: int,
        row_id: int,
        column_id: int,
        review_state: str,
        *,
        commit: bool = True,
    ) -> int:
        cur = self.db.execute(
            "UPDATE results SET review_state=? "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (review_state, run_id, row_id, column_id),
        )
        if commit:
            self.db.commit()
        return int(cur.rowcount)

    def set_result_review_metadata(
        self,
        run_id: int,
        row_id: int,
        column_id: int,
        decision: str | None,
        note: str | None,
        *,
        commit: bool = True,
    ) -> int:
        cur = self.db.execute(
            "UPDATE results SET review_decision=?, review_note=? "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (decision, note, run_id, row_id, column_id),
        )
        if commit:
            self.db.commit()
        return int(cur.rowcount)

    def mark_run_results_verified(self, run_id: int, *, commit: bool = True) -> int:
        """Auto-verify every still-unreviewed result in a run (mechanical ops).

        clean_column has no confidence columns, so without this
        every produced row would queue at NULL confidence and flood the review
        queue. Only ``unreviewed`` rows flip — a re-run that upserts over a
        previously accepted/edited/rejected cell keeps that human decision (the
        write_results ON CONFLICT clause never touches review_state)."""
        cur = self.db.execute(
            "UPDATE results SET review_state='verified' "
            "WHERE run_id=? AND review_state='unreviewed'",
            (run_id,),
        )
        if commit:
            self.db.commit()
        return int(cur.rowcount)

    def write_results(
        self,
        run_id: int,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        authorized_attempt_id: str | None = None,
        project_heads: bool = True,
        commit: bool = True,
    ) -> None:
        """Atomically persist a result batch and its accounting evidence.

        A nested savepoint makes the batch atomic inside either transaction
        mode: a rejected fact, terminal outcome, or counter update cannot
        leave a sibling result mutation for a later commit to publish.
        ``commit=False`` leaves the surrounding transaction and its earlier
        mutations under caller ownership.
        """
        if not batch:
            return
        started_transaction = not self.db.in_transaction
        if started_transaction:
            # Releasing an outermost SQLite savepoint commits it.  Start an
            # explicit transaction first so commit=False still hands an open,
            # uncommitted transaction back to its caller.
            self.db.execute("BEGIN IMMEDIATE")
        savepoint = f"write_results_{uuid.uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            self._write_results_uncommitted(
                run_id,
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=authorized_attempt_id,
                project_heads=project_heads,
            )
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if commit and started_transaction:
                self.db.rollback()
            raise
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            self.db.commit()

    def _write_results_uncommitted(
        self,
        run_id: int,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None,
        claim_token: str | None,
        claimless_direct_effect: bool,
        authorized_attempt_id: str | None,
        project_heads: bool,
    ) -> None:
        self._fence_effect_write(
            run_id,
            batch,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
            renew_claim=True,
        )
        from frisket.engine.store.result_generations import ResultGenerationStore

        ResultGenerationStore(self.project).require_effect_batch_publishable(
            run_id, batch
        )
        fact_owner_attempt_id = authorized_attempt_id or writer_attempt_id
        fact_attempt_id, terminal_outcomes = self._attempt_row_outcomes_for_batch(
            run_id,
            batch,
            authorized_attempt_id=fact_owner_attempt_id,
        )
        rows_in_batch = {int(r["row_id"]) for r in batch}
        before_states = self.result_row_failure_states(run_id, rows_in_batch)
        self.db.executemany(
            "INSERT INTO results (run_id, row_id, column_id, value, tokens_in, "
            "tokens_out, confidence, justification, error, error_code, outcome, "
            "publication_effect) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, row_id, column_id) DO UPDATE SET value=excluded.value, "
            "tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out, "
            "confidence=excluded.confidence, justification=excluded.justification, "
            "error=excluded.error, error_code=excluded.error_code, "
            "outcome=excluded.outcome, "
            "publication_effect=excluded.publication_effect",
            [
                (
                    run_id,
                    r["row_id"],
                    r["column_id"],
                    json.dumps(r.get("value")) if r.get("value") is not None else None,
                    r.get("tokens_in"),
                    r.get("tokens_out"),
                    r.get("confidence"),
                    r.get("justification"),
                    _safe_result_error(r.get("error"), r.get("error_code")),
                    r.get("error_code"),
                    _result_outcome(r),
                    r.get("publication_effect"),
                )
                for r in batch
            ],
        )
        self.write_model_calls(
            run_id,
            batch,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
            authorized_attempt_id=fact_owner_attempt_id,
            _renew_claim=False,
        )
        self._write_attempt_row_outcomes(fact_attempt_id, terminal_outcomes)
        after_states = self.result_row_failure_states(run_id, rows_in_batch)
        new_rows = set(after_states) - set(before_states)
        completed_delta = len(new_rows)
        failed_delta = sum(after_states.values()) - sum(before_states.values())
        # runs.cost_actual is NOT touched here: it is a projection of this
        # run's model_calls facts, maintained by write_model_calls (the one
        # mint) — never a second sum over the batch's per-row `cost` field.
        self.db.execute(
            "UPDATE runs SET completed_rows = completed_rows + ?, "
            "failed_rows = CASE "
            "WHEN failed_rows + ? < 0 THEN 0 ELSE failed_rows + ? END "
            "WHERE id=?",
            (
                completed_delta,
                failed_delta,
                failed_delta,
                run_id,
            ),
        )
        # Generation-managed fresh outputs remain progressive: the exact
        # result, accounting facts, counters, and head move share this batch's
        # savepoint. Staged replacement generations intentionally do nothing
        # here and publish only in ResultGenerationStore.seal().
        if project_heads:
            ResultGenerationStore(self.project)._project_written_results_uncommitted(
                run_id,
                batch,
                claim_token=claim_token,
            )

    def _attempt_row_outcomes_for_batch(
        self,
        run_id: int | None,
        batch: list[dict[str, Any]],
        *,
        authorized_attempt_id: str | None,
        receipt_id: str | None = None,
    ) -> tuple[str | None, dict[int, str]]:
        """Validate and classify attempt-owned terminal retail outcomes.

        The allocation rows are minted with consent.  Result persistence may
        only fill their one terminal slot; it cannot add a row, retarget one
        to another attempt, or change a failure into a success on replay.
        """
        attempt_id = self._attempt_for_fact_write(
            run_id, authorized_attempt_id, receipt_id=receipt_id
        )
        if attempt_id is None:
            return None, {}
        allocation_rows = self.db.execute(
            "SELECT row_id, terminal_outcome FROM attempt_row_authorizations "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchall()
        if not allocation_rows:
            return attempt_id, {}
        allocated = {
            int(row["row_id"]): row["terminal_outcome"] for row in allocation_rows
        }
        classifications: dict[int, list[str]] = {}
        known_successes = set(TERMINAL_OUTCOMES)
        known_failures = set(FAILURE_OUTCOMES + TERMINAL_FAILURE_OUTCOMES)
        for result in batch:
            row_id = int(result["row_id"])
            if row_id not in allocated:
                raise RuntimeError(
                    "result row is outside the attempt's consent-bound quote allocation"
                )
            outcome = _result_outcome(result)
            if outcome == "cancelled":
                classification = "cancelled"
            elif outcome in known_failures:
                classification = "failed"
            elif outcome in known_successes:
                classification = "succeeded"
            else:
                raise RuntimeError(
                    f"result carries unknown terminal outcome {outcome!r}"
                )
            classifications.setdefault(row_id, []).append(classification)

        terminal = {
            row_id: (
                "cancelled"
                if "cancelled" in values
                else "failed"
                if "failed" in values
                else "succeeded"
            )
            for row_id, values in classifications.items()
        }
        for row_id, classification in terminal.items():
            prior = allocated[row_id]
            if prior is not None and prior != classification:
                raise RuntimeError(
                    "attempt row terminal outcome is immutable; refusing "
                    f"{prior!r} -> {classification!r} for row {row_id}"
                )
        return attempt_id, terminal

    def _write_attempt_row_outcomes(
        self, attempt_id: str | None, terminal_outcomes: dict[int, str]
    ) -> None:
        for row_id, terminal_outcome in terminal_outcomes.items():
            updated = self.db.execute(
                "UPDATE attempt_row_authorizations SET terminal_outcome=? "
                "WHERE attempt_id=? AND row_id=? AND "
                "(terminal_outcome IS NULL OR terminal_outcome=?)",
                (terminal_outcome, attempt_id, row_id, terminal_outcome),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "attempt row terminal outcome changed during result write"
                )

    def _fence_receipt_accounting(
        self,
        receipt_id: str,
        writer_attempt_id: str | None,
        batch: list[dict[str, Any]],
    ) -> sqlite3.Row:
        from frisket.execution.attempt import require_receipt_attempt_writer

        attempt = require_receipt_attempt_writer(
            self.project, receipt_id, writer_attempt_id
        )
        scope = set(json.loads(attempt["scope_json"]))
        for result in batch:
            for item in (result, *(result.get("model_calls") or ())):
                if not isinstance(item, dict):
                    continue
                if item.get("column_id") is not None:
                    raise RuntimeError("receipt accounting cannot name output columns")
                row_id = item.get("row_id")
                if row_id is not None and (
                    type(row_id) is not int or row_id not in scope
                ):
                    raise RuntimeError(
                        "receipt accounting row is outside its admitted scope"
                    )
        return attempt

    def write_receipt_row_outcomes(
        self,
        receipt_id: str,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str,
        commit: bool = True,
    ) -> None:
        """Record consent-bound retail outcomes without materializing preview cells."""
        with self._atomic_fact_batch():
            self._fence_receipt_accounting(receipt_id, writer_attempt_id, batch)
            attempt_id, outcomes = self._attempt_row_outcomes_for_batch(
                None,
                batch,
                authorized_attempt_id=writer_attempt_id,
                receipt_id=receipt_id,
            )
            self._write_attempt_row_outcomes(attempt_id, outcomes)
        if commit:
            self.db.commit()

    def write_returned_call_accounting(
        self,
        run_id: int | None,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        authorized_attempt_id: str | None = None,
        commit: bool = True,
        receipt_id: str | None = None,
    ) -> float:
        """Persist returned provider facts independently of result cells.

        Cancellation keeps the row resumable, so it must not write a result
        cell or advance ``completed_rows``.  A provider call that already
        returned is different: its fact, project-key spend, and run cost are
        historical truth and cannot be discarded with the cell.
        ``write_model_calls`` reprojects ``runs.cost_actual`` from the durable
        facts, so a replay (same call ids, id-conflict ignored) leaves the
        run cost unchanged by construction.  The returned delta is the newly
        incurred cost, for in-memory progress display only.

        Receipt-owned previews use the same writer and spend accrual, with
        no run or output columns. Their reservation and dispatching attempt
        are the writer authority; returned preview values remain ephemeral.

        The batch is atomic because ``write_model_calls`` owns a savepoint:
        this path commits, so a mid-batch constraint failure must leave
        nothing behind to publish.
        """
        if not batch:
            return 0.0
        cost_delta = self.write_model_calls(
            run_id,
            batch,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
            authorized_attempt_id=authorized_attempt_id or writer_attempt_id,
            receipt_id=receipt_id,
        )
        if commit:
            self.db.commit()
        return cost_delta

    def carry_forward_results_outside_scope(
        self,
        run_id: int,
        column_ids: list[int],
        *,
        commit: bool = True,
    ) -> int:
        """Preserve an exactly reused output family's untargeted cells.

        A column pointer selects one run for the whole column. A targeted rerun
        of an atomic output family therefore has to materialize the prior
        pointer's cells outside the new run's immutable ``run_rows`` scope
        before moving that pointer. These are display-state carry-forwards, not
        work performed by the new run: usage/model-call facts and run counters
        stay with the original run.

        Existing rows in ``run_id`` always win, even if a malformed caller put
        one outside the declared scope. Manual edits need no copying because
        their overlay is independent of run pointers.
        """

        ordered_column_ids = sorted({int(column_id) for column_id in column_ids})
        if not ordered_column_ids:
            return 0
        if not self.has_run_row_scope(run_id):
            return 0

        placeholders = ",".join("?" for _ in ordered_column_ids)
        columns = self.db.execute(
            "SELECT id,current_run_id FROM columns "
            f"WHERE id IN ({placeholders}) ORDER BY id",
            ordered_column_ids,
        ).fetchall()
        carried = 0
        for column in columns:
            source_run_id = column["current_run_id"]
            column_id = int(column["id"])
            if source_run_id is None or int(source_run_id) == int(run_id):
                continue
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO results "
                "(run_id,row_id,column_id,value,tokens_in,tokens_out,confidence,"
                "justification,error,error_code,review_state,outcome) "
                "SELECT ?,prior.row_id,prior.column_id,prior.value,NULL,NULL,"
                "prior.confidence,prior.justification,prior.error,prior.error_code,"
                "prior.review_state,prior.outcome FROM results prior "
                "WHERE prior.run_id=? AND prior.column_id=? "
                "AND NOT EXISTS (SELECT 1 FROM run_rows scope "
                "WHERE scope.run_id=? AND scope.row_id=prior.row_id)",
                (run_id, int(source_run_id), column_id, run_id),
            )
            carried += int(inserted.rowcount)
        if commit:
            self.db.commit()
        return carried

    def result_row_failure_states(
        self, run_id: int, row_ids: set[int]
    ) -> dict[int, bool]:
        if not row_ids:
            return {}
        ordered_row_ids = sorted(row_ids)
        placeholders = ",".join("?" for _ in ordered_row_ids)
        return {
            int(row["row_id"]): bool(row["failed"])
            for row in self.db.execute(
                "SELECT row_id, "
                f"MAX(CASE WHEN outcome IN ({_FAILED_OUTCOMES_SQL}) "
                "THEN 1 ELSE 0 END) AS failed "
                "FROM results "
                f"WHERE run_id=? AND row_id IN ({placeholders}) "
                "GROUP BY row_id",
                (run_id, *ordered_row_ids),
            )
        }

    def row_error_summary(
        self,
        run_id: int,
        *,
        max_groups: int = ROW_ERROR_SUMMARY_MAX_GROUPS,
        max_examples: int = ROW_ERROR_SUMMARY_MAX_EXAMPLES,
    ) -> dict[str, Any] | None:
        """The run-detail error summary (run-row-error-observability-v1):
        distinct failure MESSAGES grouped with counts and up to
        ``max_examples`` row ids each (e.g. "11x: media has no
        values..."), instead of the receipt's old single aggregate "failed
        for every target row" with no persisted reason. Reuses
        `results.error`/`error_code` (MapRunner already writes these at
        row-failure time; run_rows itself carries no row-level detail)
        rather than a separate store. None when the run has no failed rows."""
        rows = self.db.execute(
            _ROW_ERROR_SELECT
            + "WHERE res.run_id=? AND res.error IS NOT NULL AND res.error <> '' "
            "AND (run_scope.run_id IS NULL OR scoped_row.row_id IS NOT NULL) "
            "ORDER BY res.row_id, c.position, c.id",
            (run_id,),
        ).fetchall()
        return _group_row_errors(rows, max_groups=max_groups, max_examples=max_examples)

    def batch_row_error_summaries(
        self,
        run_ids: list[int],
        *,
        max_groups: int = ROW_ERROR_SUMMARY_MAX_GROUPS,
        max_examples: int = ROW_ERROR_SUMMARY_MAX_EXAMPLES,
    ) -> dict[int, dict[str, Any]]:
        """Batched twin of ``row_error_summary`` for a whole history page
        (one query for every run's error groups, not one per run — matching
        `server/run_payloads.py`'s existing batched-columns discipline for
        the same page)."""
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        rows = self.db.execute(
            _ROW_ERROR_SELECT + f"WHERE res.run_id IN ({placeholders}) "
            "AND res.error IS NOT NULL AND res.error <> '' "
            "AND (run_scope.run_id IS NULL OR scoped_row.row_id IS NOT NULL) "
            "ORDER BY res.run_id, res.row_id, c.position, c.id",
            run_ids,
        ).fetchall()
        by_run: dict[int, list[Any]] = {}
        for row in rows:
            by_run.setdefault(row["run_id"], []).append(row)
        return {
            run_id: summary
            for run_id, rs in by_run.items()
            if (
                summary := _group_row_errors(
                    rs, max_groups=max_groups, max_examples=max_examples
                )
            )
            is not None
        }

    # Capabilities that resolve through the execution seam: a fact for
    # one of these written while the run HAS a persisted route MUST carry
    # the observation payload (A1 ground truth below).
    # OCR is included. The set is the epoch invariant's ground truth — a fact
    # for one of these written under a run that HAS a persisted route MUST
    # carry the observation payload, so adding a capability here is what makes
    # a missing route-to-fact wire loud instead of silently NULL-epoched.
    # to_markdown is included (spelled "document.convert", the literal the
    # fact column already carries — see test_phase4_capability_registration's
    # test_the_capability_token_is_the_one_the_fact_column_already_carries),
    # then geocode and census_demographics — one lane, two capability tokens
    # (each names the external API a receipt says the user's data went to).
    # All three bind every fact they mint to the admitted route before it
    # reaches this writer.
    _ROUTED_FACT_CAPABILITIES = frozenset(
        {"transcribe", "ocr", "document.convert", "geocode", "census_demographics"}
    )

    def _run_has_persisted_route(self, run_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM routes WHERE subject_kind='run' AND subject_id=? "
                "LIMIT 1",
                (str(run_id),),
            ).fetchone()
            is not None
        )

    def _attempt_for_fact_write(
        self,
        run_id: int | None,
        authorized_attempt_id: str | None,
        *,
        receipt_id: str | None = None,
    ) -> str | None:
        """Resolve immutable attempt ownership for a provider fact batch.

        MapRunner supplies the historical commitment that authorized egress.
        No mutable current-attempt pointer repairs a missing id. Unscoped
        facts use ``write_unscoped_model_calls``; this run-scoped writer
        never infers authority from absent attempt rows.
        """

        if authorized_attempt_id is not None:
            attempt = self.db.execute(
                "SELECT run_id, receipt_id FROM execution_attempts WHERE id=?",
                (str(authorized_attempt_id),),
            ).fetchone()
            if attempt is None:
                raise RuntimeError(
                    "cannot stamp model-call fact: authorized attempt "
                    f"{authorized_attempt_id!r} is missing"
                )
            attempt_run_id = attempt["run_id"]
            if (
                attempt_run_id != run_id
                or attempt["receipt_id"] != receipt_id
                or (run_id is None and receipt_id is None)
            ):
                raise RuntimeError(
                    "cannot stamp model-call fact: authorized attempt "
                    f"{authorized_attempt_id!r} belongs to run "
                    f"{attempt_run_id!r}, not run {run_id}"
                )
            return str(authorized_attempt_id)

        from frisket.execution.attempt import StaleAttemptWriter

        raise StaleAttemptWriter(
            "a run-scoped fact supplied no immutable authorized attempt"
        )

    def _fence_effect_write(
        self,
        run_id: int,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None,
        claim_token: str | None,
        claimless_direct_effect: bool,
        renew_claim: bool,
    ) -> None:
        """Apply the current-writer fence before any batch mutation."""

        from frisket.execution.attempt import STALE_DISPATCHING_AGE, StaleAttemptWriter

        if writer_attempt_id is None:
            raise StaleAttemptWriter(
                "a run-scoped mutation supplied no immutable writer attempt"
            )
        column_ids = (
            set()
            if claimless_direct_effect
            else {
                int(item["column_id"])
                for item in batch
                if item.get("column_id") is not None
            }
        )
        if not claimless_direct_effect:
            column_ids.update(
                int(call["column_id"])
                for item in batch
                for call in (item.get("model_calls") or ())
                if isinstance(call, dict) and call.get("column_id") is not None
            )
        from frisket.engine.store.output_claims import OutputColumnClaimStore

        claims = OutputColumnClaimStore(self.project)
        claims.require_current_writer(
            self.db,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            output_column_ids=column_ids,
            claimless_direct_effect=claimless_direct_effect,
        )
        if claim_token is None or not renew_claim:
            return

        try:
            renewed = claims.renew(
                claim_token=claim_token,
                run_id=run_id,
                lease_seconds=int(STALE_DISPATCHING_AGE.total_seconds()),
                commit=False,
            )
        except sqlite3.OperationalError as exc:
            from frisket.engine.store.output_claims import (
                ClaimLeaseRenewalFailed,
            )

            raise ClaimLeaseRenewalFailed(str(exc)) from exc
        if renewed == 0:
            raise StaleAttemptWriter(
                f"claim token {claim_token!r} stopped being active during renewal"
            )

    def write_model_calls(
        self,
        run_id: int | None,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        authorized_attempt_id: str | None = None,
        _renew_claim: bool = True,
        receipt_id: str | None = None,
    ) -> float:
        """Persist one batch of provider facts, its spend, and the run cost.

        The batch is ALL-OR-NOTHING, and the savepoint lives here rather than
        in any one caller.  ``_insert_model_call_rows`` runs one statement per
        row (``RETURNING`` forbids ``executemany``), so a row the schema
        refuses raises with earlier rows already inserted and BEFORE
        ``_accrue_project_key_spend`` and ``_project_cost_actual`` have run.
        A caller that commits afterwards would then publish paid facts that
        charged no key and moved no run cost — the exact inverse of the
        accrue-without-a-fact defect the ``RETURNING`` insert exists to
        prevent.  ``write_results`` happened to be covered by its own
        savepoint; ``write_returned_call_accounting`` (the post-cancellation
        path) and the embeddings refresh writer were not, so the guarantee
        belongs to the writer every fact goes through, not to its callers.

        With ``commit=False`` semantics upstream, the surrounding transaction
        stays open and caller-owned; only this batch's mutations unwind.
        """
        if not batch:
            return 0.0
        with self._atomic_fact_batch():
            return self._write_model_calls_uncommitted(
                run_id,
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=authorized_attempt_id,
                renew_claim=_renew_claim,
                receipt_id=receipt_id,
            )

    @contextmanager
    def _atomic_fact_batch(self) -> Iterator[None]:
        """All-or-nothing boundary around ONE batch of provider-fact writes.

        Shared by both fact writers because both insert row by row (``ON
        CONFLICT(id) DO NOTHING RETURNING id`` cannot run under
        ``executemany``) and only then charge spend: without this, row 1
        landing and row 2 violating a NOT NULL column leaves a durable paid
        fact with no accrual behind it.  A caller that already holds a
        transaction keeps it and its earlier mutations; only this batch
        unwinds.
        """
        started_transaction = not self.db.in_transaction
        if started_transaction:
            # Releasing an outermost SQLite savepoint commits it.  Start an
            # explicit transaction first so a caller that owns the commit
            # still gets an open, uncommitted transaction back.
            self.db.execute("BEGIN IMMEDIATE")
        savepoint = f"fact_batch_{uuid.uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_transaction:
                self.db.rollback()
            raise
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _write_model_calls_uncommitted(
        self,
        run_id: int | None,
        batch: list[dict[str, Any]],
        *,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        authorized_attempt_id: str | None = None,
        renew_claim: bool = True,
        receipt_id: str | None = None,
    ) -> float:
        receipt_attempt = None
        if receipt_id is not None:
            if run_id is not None or claim_token is not None:
                raise RuntimeError(
                    "receipt accounting cannot name a run or output claim"
                )
            if authorized_attempt_id not in {None, writer_attempt_id}:
                raise RuntimeError("receipt facts require their actual writer attempt")
            receipt_attempt = self._fence_receipt_accounting(
                receipt_id, writer_attempt_id, batch
            )
        else:
            self._fence_effect_write(
                run_id,
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                renew_claim=renew_claim,
            )
        rows: list[tuple[Any, ...]] = []
        candidate_calls: list[dict[str, Any]] = []
        fact_attempt_id = self._attempt_for_fact_write(
            run_id,
            authorized_attempt_id or writer_attempt_id,
            receipt_id=receipt_id,
        )
        # A same-id terminal enrichment may only belong to the attempt that
        # recorded acceptance. Refuse ownership drift before resolving a
        # route observation: that observation can append epoch/violation
        # rows, so discovering the mismatch afterwards would let a rejected
        # B write mutate A's route history before raising.
        explicit_call_ids = sorted(
            {
                str(call["id"])
                for result in batch
                for call in (result.get("model_calls") or [])
                if isinstance(call, dict) and call.get("id")
            }
        )
        for start in range(0, len(explicit_call_ids), 400):
            chunk = explicit_call_ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            existing_owners = self.db.execute(
                "SELECT id, run_id, attempt_id FROM model_calls "
                f"WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for owner in existing_owners:
                if owner["run_id"] != run_id:
                    raise RuntimeError(
                        "model-call reconciliation identity drift for "
                        f"{owner['id']!r}: run_id changed"
                    )
                if owner["attempt_id"] != fact_attempt_id:
                    raise RuntimeError(
                        "model-call reconciliation identity drift for "
                        f"{owner['id']!r}: attempt_id changed"
                    )
        # (call_id, provider, credential_source, provider_cost_usd) for every
        # candidate fact, kept parallel to `rows` so spend accrual is derived
        # from the SAME facts that get written and cannot drift from them.
        spend_facts: list[tuple[str, str, str, Any]] = []
        # A1 (epoch-invariant ground truth): the invariant is keyed on the
        # PERSISTED route chain, not on transient payload presence — if this
        # run has a head route, every routed-capability fact must carry the
        # observation payload; a missing one means a wiring link dropped
        # (extras not merged, recipe missed the extra) and MUST fail loudly
        # before anything is written, never silently record a legacy
        # NULL-epoch fact under a routed run. One cheap route query per
        # batch, resolved lazily.
        run_has_route: bool | None = None
        for result in batch:
            for call in result.get("model_calls") or []:
                if not isinstance(call, dict):
                    continue
                # Route binding strips private observations from persisted facts,
                # not from the producer's reusable accounting payload.
                call = dict(call)
                nested_run_id = call.get("run_id")
                if nested_run_id is not None:
                    try:
                        nested_run_id = int(nested_run_id)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "model-call fact run_id must match the writer run_id"
                        ) from exc
                    if nested_run_id != run_id:
                        raise RuntimeError(
                            "model-call fact run_id override does not match the "
                            f"writer run_id ({nested_run_id} != {run_id})"
                        )
                fact_run_id = run_id
                nested_attempt_id = call.get("attempt_id")
                if nested_attempt_id is not None and str(nested_attempt_id) != str(
                    fact_attempt_id
                ):
                    raise RuntimeError(
                        "model-call fact attempt_id override does not match the "
                        "writer's authorized attempt"
                    )
                capability = call.get("capability")
                if receipt_attempt is not None:
                    from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY

                    observation = call.get(ROUTE_OBSERVATION_KEY)
                    if observation is not None and (
                        observation.get("route_id") != receipt_attempt["head_route_id"]
                    ):
                        raise RuntimeError(
                            "receipt fact does not name its admitted route"
                        )
                epoch_id = self._route_epoch_for_call(call)
                if epoch_id is None and capability in self._ROUTED_FACT_CAPABILITIES:
                    if run_has_route is None:
                        run_has_route = (
                            receipt_attempt["head_route_id"] is not None
                            if receipt_attempt is not None
                            else self._run_has_persisted_route(fact_run_id)
                        )
                    if run_has_route:
                        raise RuntimeError(
                            "route-epoch invariant: run "
                            f"{fact_run_id} has a persisted "
                            "execution route but this "
                            f"{capability!r} fact carries no binding "
                            "observation — a route-to-fact wiring link "
                            "dropped; refusing to record a silent legacy "
                            "fact"
                        )
                call_id = call.get("id") or str(uuid.uuid4())
                credential_source = validate_credential_source(
                    call.get("credential_source", "none")
                )
                # ONE cost domain (ai/models/metadata.py): a stated cost this
                # codebase will not treat as money refuses at the write, so no
                # downstream surface has to reinterpret it.
                cost_usd = validate_provider_cost(
                    call.get("provider_cost_usd"), call_id=call_id
                )
                spend_facts.append(
                    (
                        call_id,
                        str(call.get("provider", "unknown")),
                        credential_source,
                        cost_usd,
                    )
                )
                rows.append(
                    (
                        call_id,
                        call["fact_version"],
                        fact_run_id,
                        call.get("row_id") or result["row_id"],
                        call.get("column_id") or result["column_id"],
                        call["capability"],
                        call["engine"],
                        call.get("provider", "unknown"),
                        call.get("provider_kind", "unknown"),
                        json.dumps(call.get("model_ids") or []),
                        credential_source,
                        call.get("provider_reported_cost_usd"),
                        cost_usd,
                        call.get("cost_source", "unknown"),
                        json.dumps(call.get("units") or {}),
                        json.dumps(call.get("cache") or {}),
                        call.get("request_id"),
                        json.dumps(call.get("warnings") or []),
                        epoch_id,
                        fact_attempt_id,
                        duration_ms_value(call.get("duration_ms")),
                    )
                )
                candidate_calls.append(
                    {
                        **call,
                        "id": call_id,
                        # One mint: terminal enrichment reconciles against the
                        # cost this batch VALIDATED, never a second reading of
                        # the caller's raw dict.
                        "provider_cost_usd": cost_usd,
                        "run_id": fact_run_id,
                        "row_id": call.get("row_id") or result["row_id"],
                        "column_id": call.get("column_id") or result["column_id"],
                        "epoch_id": epoch_id,
                        "attempt_id": fact_attempt_id,
                    }
                )
        if not rows:
            return 0.0
        # Which of these facts are genuinely new?  A replayed id is ignored,
        # and accrual has to drop exactly the same ones or a crash-replayed
        # batch would charge the key twice.  ``already_recorded`` decides which
        # candidates are terminal ENRICHMENTS of a durable fact; the insert
        # itself reports what actually became durable.
        already_recorded = self._existing_model_call_ids([f[0] for f in spend_facts])
        inserted = self._insert_model_call_rows(rows)
        # Accrue from rows that were ACTUALLY inserted, never from candidates.
        # A broad ``INSERT OR IGNORE`` used to swallow every constraint
        # violation, not just id replays: a fact rejected by a NOT NULL column
        # vanished while the cap still charged the key for it and the run
        # projection found no fact to mint from — the cap said $1, the run said
        # $0, and no receipt fact existed to reconcile them.  ``inserted`` is
        # parallel to ``rows``/``spend_facts``, so an in-batch duplicate id
        # (only the first copy is written) drops out of accrual for free.
        newly_recorded_spend = [
            fact
            for fact, was_inserted in zip(spend_facts, inserted, strict=True)
            if was_inserted
        ]
        self._accrue_project_key_spend(newly_recorded_spend)
        # A provider can acknowledge a separately-running job before its
        # final meter/cost exists.  That acceptance is persisted immediately
        # as an unknown-cost fact.  A later completed/error row carries the
        # SAME deterministic call id with strictly richer accounting; enrich
        # the one fact rather than ignoring reality (a bare id-conflict
        # ignore) or minting a second provider call.  The returned delta lets
        # the cancellation-after-return path add newly-known cost to the run.
        reconciled_cost = 0.0
        reconciled_ids: set[str] = set()
        for call in candidate_calls:
            call_id = str(call["id"])
            if call_id not in already_recorded or call_id in reconciled_ids:
                continue
            reconciled_ids.add(call_id)
            reconciled_cost += self._reconcile_existing_model_call(call)
        # THE one mint for runs.cost_actual: reproject it from the facts this
        # transaction just made durable (all model_calls writers live in this
        # module, so every fact mutation passes through here or leaves the
        # run untouched).  A projection cannot drift from its source and a
        # replay recomputes the same value, so overwrite-vs-accumulate and
        # cached-call divergences are structurally impossible.
        if run_id is not None:
            self._project_cost_actual(run_id)
        # ``provider_cost_usd`` on a cache fact is historical provenance, not
        # money spent by this execution.  Keep it in model_calls, but exclude
        # it from the run's newly-incurred cost just as row_execution does for
        # the normal result path.
        return reconciled_cost + sum(
            float(cost_usd)
            for _call_id, _provider, credential_source, cost_usd in newly_recorded_spend
            if credential_source != "cache" and cost_usd is not None
        )

    _MODEL_CALL_INSERT = (
        "INSERT INTO model_calls "
        "(id, fact_version, run_id, row_id, column_id, "
        "capability, engine, provider, provider_kind, model_ids, "
        "credential_source, provider_reported_cost_usd, provider_cost_usd, "
        "cost_source, units, cache, request_id, warnings, epoch_id, "
        "attempt_id, duration_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO NOTHING RETURNING id"
    )

    def _insert_model_call_rows(self, rows: list[tuple[Any, ...]]) -> list[bool]:
        """Write candidate fact rows; report which ones became durable.

        ``ON CONFLICT(id) DO NOTHING`` narrows the ignore to the ONE conflict
        replay actually produces — the same call id arriving twice.  Every
        other constraint (NOT NULL columns, the run_id foreign key) fails
        loudly and names itself, because a fact the schema refused is not a
        fact: charging a spend cap for it while the run projection cannot see
        it is how the two ledgers came to disagree.

        ``RETURNING`` makes "did this row land?" an observation rather than an
        inference from a pre-insert probe, so accrual and the cost projection
        read exactly the set the database accepted.  One statement per row:
        ``sqlite3.executemany`` refuses RETURNING, and these batches are one
        provider call each.

        Row-at-a-time means a refusal lands mid-batch, so callers MUST hold
        ``_atomic_fact_batch`` — the rows written before the refusal are not
        yet paid for.
        """
        inserted: list[bool] = []
        for row in rows:
            landed = self.db.execute(self._MODEL_CALL_INSERT, row).fetchone()
            inserted.append(landed is not None)
        return inserted

    def _project_cost_actual(self, run_id: int) -> None:
        """Reproject ``runs.cost_actual`` from this run's durable call facts.

        SQL mirror of :func:`frisket.ai.models.metadata.model_calls_cost_actual`
        (the receipt derivation — a parity regression pins the two spellings
        together): cache facts are historical provenance and excluded; every
        live known cost contributes; one live NULL/negative/non-finite cost
        makes the run figure NULL (honest unknown) instead of a fabricated
        exact zero.  Runs inside the fact writer's transaction.

        The aggregate is bounded on BOTH sides here, exactly as
        :func:`~frisket.ai.models.metadata.provider_cost_total` bounds it in
        Python: per fact, then on the sum.  Bounding only the fact let two
        in-domain costs add up to ``inf``, which SQL rendered NULL and Python
        rendered ``inf`` — the very divergence this projection exists to
        close.
        """
        self.db.execute(
            "UPDATE runs SET cost_actual = ("
            "SELECT CASE "
            "WHEN COUNT(*) = 0 THEN 0.0 "
            "WHEN COUNT(provider_cost_usd) < COUNT(*) THEN NULL "
            "WHEN MIN(provider_cost_usd) < 0 THEN NULL "
            "WHEN MAX(provider_cost_usd) > ? THEN NULL "
            "WHEN NOT (SUM(provider_cost_usd) <= ?) THEN NULL "
            "ELSE ROUND(SUM(provider_cost_usd), 8) END "
            "FROM model_calls "
            "WHERE run_id=? AND credential_source != 'cache'"
            ") WHERE id=?",
            (
                PROVIDER_COST_MAX_USD,
                PROVIDER_COST_TOTAL_MAX_USD,
                run_id,
                run_id,
            ),
        )

    def write_unscoped_model_calls(
        self,
        calls: list[dict[str, Any]],
        *,
        row_id: int | None,
        column_id: int | None,
        commit: bool = True,
    ) -> float:
        """Persist neutral provider facts for effects with no ``runs`` row.

        Direct whole-project actions can call a provider before their project
        transaction without minting a row-oriented run.  Their facts still
        belong in the same neutral ledger, and project-key spend must advance
        from exactly the facts that became durable.  ``ON CONFLICT(id) DO
        NOTHING`` plus the call id makes a repeated write free; cache facts
        remain historical provenance and are excluded from the returned
        newly-incurred cost.

        ``row_id`` and ``column_id`` are explicit even though either may be
        ``None``: an unscoped caller must deliberately choose the best durable
        source refs rather than inheriting a made-up run/result coordinate.
        With ``commit=False`` the caller owns the surrounding transaction.
        """
        rows: list[tuple[Any, ...]] = []
        spend_facts: list[tuple[str, str, str, Any]] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or uuid.uuid4())
            credential_source = validate_credential_source(
                call.get("credential_source", "none")
            )
            cost_usd = validate_provider_cost(
                call.get("provider_cost_usd"), call_id=call_id
            )
            spend_facts.append(
                (
                    call_id,
                    str(call.get("provider", "unknown")),
                    credential_source,
                    cost_usd,
                )
            )
            rows.append(
                (
                    call_id,
                    call["fact_version"],
                    None,
                    row_id,
                    column_id,
                    call["capability"],
                    call["engine"],
                    call.get("provider", "unknown"),
                    call.get("provider_kind", "unknown"),
                    json.dumps(call.get("model_ids") or []),
                    credential_source,
                    call.get("provider_reported_cost_usd"),
                    cost_usd,
                    call.get("cost_source", "unknown"),
                    json.dumps(call.get("units") or {}),
                    json.dumps(call.get("cache") or {}),
                    call.get("request_id"),
                    json.dumps(call.get("warnings") or []),
                    None,
                    None,
                    duration_ms_value(call.get("duration_ms")),
                )
            )
        if not rows:
            return 0.0

        try:
            with self._atomic_fact_batch():
                inserted = self._insert_model_call_rows(rows)
                # Same rule as the scoped writer: money follows the rows the
                # database ACCEPTED, so a constraint-rejected fact can never
                # leave an accrual behind with no fact to explain it — and the
                # savepoint makes the converse true too, so a row that landed
                # before a later row was refused cannot become durable with no
                # accrual.
                newly_recorded_spend = [
                    fact
                    for fact, was_inserted in zip(spend_facts, inserted, strict=True)
                    if was_inserted
                ]
                self._accrue_project_key_spend(newly_recorded_spend)
            if commit:
                self.db.commit()
        except BaseException:
            if commit:
                self.db.rollback()
            raise

        return sum(
            float(cost_usd)
            for _call_id, _provider, credential_source, cost_usd in newly_recorded_spend
            if credential_source != "cache" and cost_usd is not None
        )

    def model_call_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Load one pre-egress/returned reconciliation checkpoint.

        A thin projection over the shared ``effect_checkpoints`` store
        (family ``model_call``); the caller-facing shape is unchanged.
        Malformed payload JSON is returned as ``None`` under ``payload`` rather
        than hidden as absence: the effect-site consumer can then fail closed
        with its action-specific error instead of mistaking corruption for
        permission to call the provider again.
        """
        row = self._effect_store().get(checkpoint_id)
        if row is None or row["family"] != MODEL_CALL_CHECKPOINT_FAMILY:
            return None
        wrapper = row["payload"] if isinstance(row["payload"], dict) else None
        if is_operator_attestation(wrapper):
            # An operator's reconcile decision has no inner payload wrapper;
            # surface it intact so effect sites can name the decision instead
            # of reporting corruption.
            payload = wrapper
        else:
            payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
        fact_id = wrapper.get("provider_fact_id") if isinstance(wrapper, dict) else None
        if not isinstance(fact_id, str) or not fact_id:
            fact_id = None

        fact: dict[str, Any] | None = None
        if fact_id is not None:
            fact_row = self.db.execute(
                "SELECT id, fact_version, capability, engine, provider, "
                "provider_kind, model_ids, credential_source, "
                "provider_reported_cost_usd, provider_cost_usd, cost_source, "
                "units, cache, request_id, warnings "
                "FROM model_calls WHERE id=?",
                (fact_id,),
            ).fetchone()
            if fact_row is not None:
                try:
                    fact = {
                        "id": str(fact_row["id"]),
                        "fact_version": str(fact_row["fact_version"]),
                        "capability": str(fact_row["capability"]),
                        "engine": str(fact_row["engine"]),
                        "provider": str(fact_row["provider"]),
                        "provider_kind": str(fact_row["provider_kind"]),
                        "model_ids": json.loads(fact_row["model_ids"]),
                        "credential_source": str(fact_row["credential_source"]),
                        "provider_reported_cost_usd": fact_row[
                            "provider_reported_cost_usd"
                        ],
                        "provider_cost_usd": fact_row["provider_cost_usd"],
                        "cost_source": str(fact_row["cost_source"]),
                        "units": json.loads(fact_row["units"]),
                        "cache": json.loads(fact_row["cache"]),
                        "request_id": fact_row["request_id"],
                        "warnings": json.loads(fact_row["warnings"]),
                    }
                except (TypeError, json.JSONDecodeError):
                    fact = None
        return {
            "id": row["id"],
            "action_kind": row["action_kind"],
            "state": row["state"],
            "provider_fact_id": fact_id,
            "payload": payload,
            "provider_fact": fact,
            "created_at": row["created_at"],
        }

    def _effect_store(self) -> EffectCheckpointStore:
        return EffectCheckpointStore(self.db)

    def row_effect_checkpoint(self, run_id: int, row_id: int) -> dict[str, Any] | None:
        """Load the durable state for one MapRunner row effect.

        A thin projection over the shared ``effect_checkpoints`` store
        (family ``row_effect``: group key = run id, unit key = row id).  The
        lookup is by logical row rather than by a caller-computed id so a
        changed request cannot evade an older reservation by minting a new
        hash.  The effect site compares ``identity`` and fails closed on any
        drift.  Malformed returned JSON remains visible as ``response=None``;
        corruption is never mistaken for permission to egress again.
        """

        row = self._effect_store().find_unit(
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "run_id": int(row["group_key"]),
            "row_id": int(row["unit_key"]),
            "action_kind": row["action_kind"],
            "identity": row["identity"],
            "authorized_attempt_id": row["authorized_attempt_id"],
            "state": row["state"],
            "response": row["payload"],
            "accounting_persisted": row["accounting_persisted"],
            "created_at": row["created_at"],
        }

    def reserve_row_effect_checkpoint(
        self,
        checkpoint_id: str,
        *,
        run_id: int,
        row_id: int,
        action_kind: str,
        identity: str,
        authorized_attempt_id: str | None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> bool:
        """Reserve one logical row immediately before possible egress."""

        return self._effect_store().reserve(
            checkpoint_id,
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
            action_kind=action_kind,
            identity=identity,
            authorized_attempt_id=authorized_attempt_id,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )

    def complete_row_effect_checkpoint(
        self,
        checkpoint_id: str,
        *,
        run_id: int,
        row_id: int,
        action_kind: str,
        identity: str,
        batch: list[dict[str, Any]],
        replay_response: dict[str, Any],
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> float:
        """Atomically persist a returned row's facts, spend, and replay.

        The provider invoice is historical truth as soon as the response has
        returned; deferring its fact until result insertion leaves the project
        key cap blind in the crash window between those operations.  The
        shared store books the fact under the attempt that authorized egress
        (the ``accrue`` hook receives the verified checkpoint); the later
        result transaction re-inserts the same call ids, and the fact-derived
        ``runs.cost_actual`` projection makes that replay free.

        Replay dedup rests on caller-minted call ids (``ON CONFLICT(id) DO
        NOTHING``): an id-less ``model_calls`` entry would mint a fresh uuid
        on every replay and accrue cap spend once per replay, so the
        checkpoint path refuses it before anything is written.
        """

        require_model_call_ids(batch)

        def accrue(checkpoint: dict[str, Any]) -> float:
            from frisket.engine.store.row_files import (
                register_returned_row_files,
                record_row_file_calls,
            )

            register_returned_row_files(self.project, replay_response)
            record_row_file_calls(
                self.project,
                batch,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
            )
            return self.write_returned_call_accounting(
                int(run_id),
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
                commit=False,
            )

        return self._effect_store().complete(
            checkpoint_id,
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
            action_kind=action_kind,
            identity=identity,
            payload=replay_response,
            accrue=accrue,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )

    def consume_returned_row_effect_checkpoint(
        self,
        checkpoint_id: str,
        *,
        run_id: int,
        row_id: int,
        action_kind: str,
        identity: str,
        batch: list[dict[str, Any]],
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        project_heads: bool = True,
        commit: bool = True,
    ) -> None:
        """Atomically commit a returned row, its original-attempt facts, and retire.

        A later resume may be executing under attempt B, but the stored
        ``authorized_attempt_id`` is attempt A: A authorized the provider
        effect, so A remains the immutable owner of every fact in ``batch``.
        Retire-on-consume is the ``runs.py`` family decision: the
        replay authority is deleted in the same transaction that commits the
        results, unlike the retained-forever plugin family.

        Id-less ``model_calls`` entries are refused before anything is
        written — see ``complete_row_effect_checkpoint``.
        """

        require_model_call_ids(batch)

        def finalize(checkpoint: dict[str, Any]) -> None:
            self.write_results(
                int(run_id),
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
                project_heads=project_heads,
                commit=False,
            )

        self._effect_store().consume_and_retire(
            checkpoint_id,
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
            action_kind=action_kind,
            identity=identity,
            finalize=finalize,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
            commit=commit,
        )

    def account_returned_row_effect_checkpoint(
        self,
        checkpoint_id: str,
        *,
        run_id: int,
        row_id: int,
        action_kind: str,
        identity: str,
        batch: list[dict[str, Any]],
        replay_response: dict[str, Any],
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> float:
        """Persist a cancelled-after-return fact while keeping the row replayable.

        Cancellation deliberately leaves the result absent.  Facts and spend
        are still historical truth, so they commit under the original attempt
        and the durable replay response is rewritten with zero row cost.  A
        later result consume re-inserts the already-owned facts idempotently;
        the fact-derived ``runs.cost_actual`` projection recomputes to the
        same value rather than accruing twice.

        That idempotence rests on stable caller-minted call ids, so id-less
        ``model_calls`` entries are refused before anything is written — see
        ``complete_row_effect_checkpoint``.
        """

        require_model_call_ids(batch)

        def accrue(checkpoint: dict[str, Any]) -> float:
            return self.write_returned_call_accounting(
                int(run_id),
                batch,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
                commit=False,
            )

        return self._effect_store().account_returned(
            checkpoint_id,
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
            action_kind=action_kind,
            identity=identity,
            payload=replay_response,
            accrue=accrue,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )

    def discard_reserved_row_effect_checkpoint(
        self,
        checkpoint_id: str,
        *,
        run_id: int,
        row_id: int,
        action_kind: str,
        identity: str,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> bool:
        """Retire a reservation only when the caller proves egress did not start."""

        return self._effect_store().discard_reserved(
            checkpoint_id,
            family=ROW_EFFECT_CHECKPOINT_FAMILY,
            group_key=str(int(run_id)),
            unit_key=str(int(row_id)),
            action_kind=action_kind,
            identity=identity,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )

    def reserve_model_call_checkpoint(
        self,
        checkpoint_id: str,
        *,
        action_kind: str,
        payload: dict[str, Any],
    ) -> bool:
        """Durably reserve one effect identity immediately before egress.

        Family ``model_call`` on the shared store: the caller-computed
        checkpoint id IS the unit (and the verbatim identity), grouped by
        action kind.  Returns ``True`` only to the invocation that created
        the reservation.  A concurrent/exact retry gets ``False`` and must
        inspect the existing state; it may reconcile ``returned`` but must
        never call through an ambiguous ``reserved`` row.
        """
        return self._effect_store().reserve(
            checkpoint_id,
            family=MODEL_CALL_CHECKPOINT_FAMILY,
            group_key=action_kind,
            unit_key=checkpoint_id,
            action_kind=action_kind,
            identity=checkpoint_id,
            payload={"payload": payload, "provider_fact_id": None},
        )

    def complete_model_call_checkpoint(
        self,
        checkpoint_id: str,
        *,
        action_kind: str,
        payload: dict[str, Any],
        fact: dict[str, Any],
        row_id: int | None,
        column_id: int | None,
    ) -> float:
        """Atomically attach a returned fact, spend, and recovery payload."""
        fact_id = str(fact.get("id") or "")
        if not fact_id:
            raise ValueError("checkpoint provider fact requires an id")

        def accrue(_checkpoint: dict[str, Any]) -> float:
            return self.write_unscoped_model_calls(
                [fact],
                row_id=row_id,
                column_id=column_id,
                commit=False,
            )

        return self._effect_store().complete(
            checkpoint_id,
            family=MODEL_CALL_CHECKPOINT_FAMILY,
            group_key=action_kind,
            unit_key=checkpoint_id,
            action_kind=action_kind,
            identity=checkpoint_id,
            payload={"payload": payload, "provider_fact_id": fact_id},
            accrue=accrue,
        )

    def discard_reserved_model_call_checkpoint(
        self,
        checkpoint_id: str,
        *,
        action_kind: str,
    ) -> bool:
        """Remove this invocation's reservation when egress provably did not start."""
        return self._effect_store().discard_reserved(
            checkpoint_id,
            family=MODEL_CALL_CHECKPOINT_FAMILY,
            group_key=action_kind,
            unit_key=checkpoint_id,
            action_kind=action_kind,
            identity=checkpoint_id,
        )

    def _reconcile_existing_model_call(self, call: dict[str, Any]) -> float:
        """Enrich one accepted-but-unmetered call with terminal accounting.

        Provider job APIs can prove acceptance before they expose a final
        meter.  The acceptance fact is the call's durable identity; terminal
        polling may only add information to that SAME id.  Immutable identity
        drift refuses rather than rewriting history, known cost never
        downgrades to unknown, and a project-key call moves atomically from
        the unmetered counter to priced spend when its cost becomes known.
        """
        call_id = str(call["id"])
        existing = self.db.execute(
            "SELECT * FROM model_calls WHERE id=?", (call_id,)
        ).fetchone()
        if existing is None:
            return 0.0

        immutable = {
            "fact_version": call.get("fact_version"),
            "run_id": call.get("run_id"),
            "row_id": call.get("row_id"),
            "column_id": call.get("column_id"),
            "capability": call.get("capability"),
            "engine": call.get("engine"),
            "provider": call.get("provider", "unknown"),
            "provider_kind": call.get("provider_kind", "unknown"),
            "credential_source": call.get("credential_source", "none"),
            "request_id": call.get("request_id"),
            "epoch_id": call.get("epoch_id"),
            "attempt_id": call.get("attempt_id"),
        }
        for field, incoming in immutable.items():
            if (
                field == "column_id"
                and existing[field] is None
                and incoming is not None
            ):
                # Acceptance happens before the recipe has projected a result
                # onto its first output column.  The terminal row may add that
                # reference, but may never replace one already known.
                continue
            if existing[field] != incoming:
                raise RuntimeError(
                    "model-call reconciliation identity drift for "
                    f"{call_id!r}: {field} changed"
                )
        incoming_models = list(call.get("model_ids") or [])
        if json.loads(existing["model_ids"]) != incoming_models:
            raise RuntimeError(
                f"model-call reconciliation identity drift for {call_id!r}: "
                "model_ids changed"
            )

        old_cost = existing["provider_cost_usd"]
        # Already through the cost domain: ``write_model_calls`` validates
        # every candidate in the batch before anything is written and puts the
        # validated number on the candidate dict, so this route cannot see a
        # value the insertion route would have refused.  It used to: a terminal
        # poll reporting -0.50 was written onto the fact, SUBTRACTED from
        # ``spent_micro`` and allowed to decrement ``unmetered_calls``, so an
        # exhausted cap reopened by fifty cents while both cost derivations
        # still read the fact as unknown and left the run figure NULL.
        incoming_cost = call.get("provider_cost_usd")
        if old_cost is not None:
            if incoming_cost is not None and float(old_cost) != float(incoming_cost):
                raise RuntimeError(
                    f"model-call reconciliation cost drift for {call_id!r}"
                )
            return 0.0

        old_units = json.loads(existing["units"])
        incoming_units = dict(call.get("units") or {})
        if any(incoming_units.get(key) != value for key, value in old_units.items()):
            raise RuntimeError(f"model-call reconciliation meter drift for {call_id!r}")

        # duration_ms is enriched via COALESCE: the accepted row's NULL may be
        # filled in exactly once by the terminal poll's bracketed elapsed
        # time, and an already-set value (a prior reconcile, or a transport
        # that stamps it at acceptance) is never overwritten.
        self.db.execute(
            "UPDATE model_calls SET column_id=COALESCE(column_id, ?), "
            "provider_reported_cost_usd=?, "
            "provider_cost_usd=?, cost_source=?, units=?, cache=?, warnings=?, "
            "duration_ms=COALESCE(duration_ms, ?) "
            "WHERE id=? AND provider_cost_usd IS NULL",
            (
                call.get("column_id"),
                call.get("provider_reported_cost_usd"),
                incoming_cost,
                call.get("cost_source", "unknown"),
                json.dumps(incoming_units),
                json.dumps(call.get("cache") or {}),
                json.dumps(call.get("warnings") or []),
                duration_ms_value(call.get("duration_ms")),
                call_id,
            ),
        )
        credential_source = str(existing["credential_source"])
        if credential_source == "project_key" and incoming_cost is not None:
            provider = str(existing["provider"])
            key_row = self.db.execute(
                "SELECT unmetered_calls FROM project_provider_keys WHERE provider=?",
                (provider,),
            ).fetchone()
            if key_row is not None:
                if int(key_row["unmetered_calls"]) <= 0:
                    raise RuntimeError(
                        "project-key spend reconciliation has no matching "
                        f"unmetered {provider!r} call"
                    )
                self.db.execute(
                    "UPDATE project_provider_keys SET "
                    "spent_micro=spent_micro+?, "
                    "unmetered_calls=unmetered_calls-1 WHERE provider=?",
                    (int(round(float(incoming_cost) * 1_000_000)), provider),
                )
        return (
            float(incoming_cost)
            if credential_source != "cache" and incoming_cost is not None
            else 0.0
        )

    def _existing_model_call_ids(self, call_ids: list[str]) -> set[str]:
        found: set[str] = set()
        # Chunked well under SQLite's variable limit; ids are the PK so this
        # is an index probe, not a scan.
        for start in range(0, len(call_ids), 400):
            chunk = call_ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            found.update(
                str(row["id"])
                for row in self.db.execute(
                    f"SELECT id FROM model_calls WHERE id IN ({placeholders})",
                    chunk,
                )
            )
        return found

    def _accrue_project_key_spend(
        self, spend_facts: list[tuple[str, str, str, Any]]
    ) -> None:
        """Charge newly recorded provider calls against the project key that
        paid for them.

        This is the ONE insertion-time accrual path, and it sits at the
        effect site: money becomes known exactly when a model_call fact is
        written, so nothing upstream has to remember to report spend and no
        optional parameter can be forgotten. The same store also owns the
        one legal later transition, in ``_reconcile_existing_model_call``:
        an accepted unknown-cost fact becoming priced moves one matching
        unmetered call into spend atomically. Both paths run in the fact
        writer's transaction, so facts and money commit or roll back
        together.

        ``credential_source == "project_key"`` is the whole attribution rule,
        and it is exact rather than heuristic: the routers set that token
        only for providers taken from ``Project.provider_model_keys()``
        (server/workspace.py, engine/jobs/runs.py ``_router_for``), which is
        keyed by ``project_provider_keys.provider`` — so the provider string
        on the fact is the primary key of the row to charge, with no name
        translation in between. Cache hits carry ``credential_source
        == "cache"`` and are excluded for free: a replayed answer costs the
        provider nothing.
        """
        if not spend_facts:
            return
        micro: dict[str, float] = {}
        unmetered: dict[str, int] = {}
        for _call_id, provider, credential_source, cost_usd in spend_facts:
            if credential_source != "project_key":
                continue
            if cost_usd is None:
                # An unpriced model. This call DID spend money; we just
                # cannot say how much. Counting it as 0 is the lie this
                # whole change exists to remove.
                unmetered[provider] = unmetered.get(provider, 0) + 1
                continue
            micro[provider] = micro.get(provider, 0.0) + float(cost_usd) * 1_000_000
        providers = set(micro) | set(unmetered)
        if not providers:
            return
        # Round once per provider per batch rather than per call, so a run of
        # sub-micro-dollar calls accumulates instead of each truncating to 0.
        self.project.accrue_provider_spend(
            {
                provider: ProviderSpendDelta(
                    micro=int(round(micro.get(provider, 0.0))),
                    unmetered_calls=unmetered.get(provider, 0),
                )
                for provider in providers
            }
        )

    def _route_epoch_for_call(self, call: dict[str, Any]) -> str | None:
        """The §6 observation point, run inside THIS store's transaction.

        A route-bound fact arrives carrying the transient
        observation payload attached by
        ``frisket.execution.runtime_binding.bind_fact_to_route``;
        it is popped here (never a stored column) and handed to
        ``observe_binding_divergence`` on ``self.db`` — the fact writer's own
        connection, so the (possibly successor) route, epoch, and violation
        rows commit atomically with the fact row itself. Returns
        the ``epoch_id`` the fact row must link.

        A fact WITHOUT the payload is a non-routed fact and keeps the honest
        NULL epoch; receipt-owned routed previews obey the same invariant.
        Epoch invariant (§1.1): a payload that cannot be resolved to an
        epoch is a defect and fails loudly, never a silent NULL.
        """
        from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY

        observation = call.pop(ROUTE_OBSERVATION_KEY, None)
        if observation is None:
            return None
        from frisket.engine.store.execution_routes import (
            observe_binding_divergence,
            route_row_by_id,
        )

        route = route_row_by_id(self.db, str(observation["route_id"]))
        if route is None:
            raise RuntimeError(
                "route-bound model-call fact references unknown route "
                f"{observation['route_id']!r} (epoch invariant: a fact "
                "written under a route MUST carry an epoch)"
            )
        _route_id, epoch_id = observe_binding_divergence(
            self.db, route, observation["observed"]
        )
        if not epoch_id:  # pragma: no cover - observe always returns an epoch
            raise RuntimeError(
                f"observation point returned no epoch for route {route.id}"
            )
        return epoch_id

    def model_calls(
        self, run_id: int | None = None, *, receipt_id: str | None = None
    ) -> list[sqlite3.Row]:
        if receipt_id is not None:
            if run_id is not None:
                raise ValueError("model calls require one owner")
            return self.db.execute(
                "SELECT m.* FROM model_calls m JOIN execution_attempts a "
                "ON a.id=m.attempt_id WHERE a.receipt_id=? "
                "ORDER BY m.created_at, m.id",
                (receipt_id,),
            ).fetchall()
        if run_id is None:
            return self.db.execute(
                "SELECT * FROM model_calls ORDER BY created_at, id"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM model_calls WHERE run_id=? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()

    def finish_run(
        self, run_id: int, status: str = "completed", *, commit: bool = True
    ) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, finished_at=datetime('now') WHERE id=?",
            (status, run_id),
        )
        if commit:
            self.db.commit()

    def request_cancel(self, run_id: int, *, commit: bool = True) -> bool:
        """Durably request cooperative cancellation of a live run.

        The request is an intent, not a terminal status transition. Repeated
        requests preserve the first timestamp and remain successful while the
        run is still live; a terminal run refuses a new request.
        """

        requested = self.db.execute(
            "UPDATE runs SET cancel_requested_at="
            "COALESCE(cancel_requested_at, datetime('now')) "
            "WHERE id=? AND status='running'",
            (run_id,),
        )
        if commit:
            self.db.commit()
        return requested.rowcount == 1

    def has_cancel_intent(self, run_id: int) -> bool:
        """Whether the durable non-terminal cancellation intent is present."""

        row = self.db.execute(
            "SELECT cancel_requested_at FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        return bool(row is not None and row["cancel_requested_at"] is not None)

    def cancellation_requested(self, run_id: int) -> bool:
        """Worker predicate: cooperative intent OR legacy terminal status."""

        row = self.db.execute(
            "SELECT status, cancel_requested_at FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        return bool(
            row is not None
            and (row["status"] == "cancelled" or row["cancel_requested_at"] is not None)
        )

    def begin_run_resume(
        self,
        run_id: int,
        *,
        reopen_operator_cancelled: bool = True,
        resumable_halt_codes: Collection[str] = (),
        commit: bool = True,
    ) -> RunResumeAdmission:
        """Atomically reopen a genuine resume and clear prior halt metadata.

        A direct ``run.backfill`` may intentionally resume any terminal run. A
        queue worker may resume only the already-running attempt or a cancelled
        invocation carrying an allowlisted internal-halt marker. The status,
        marker, and update are evaluated under one write transaction so a
        terminal transition racing admission cannot be erased accidentally.

        Returns the admission decision TOGETHER with the exact pre-resume run
        state observed under that transaction, so a caller whose pre-dispatch
        phase then refuses (route binding / run-start verification) can put
        the run row back exactly as it found it — see
        :meth:`revert_run_resume`. Reading the prior state here rather than in
        a separate SELECT is what makes the revert honest: it restores what
        admission actually saw, not what a racing writer left behind.
        """

        try:
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT status, finished_at, params, cancel_requested_at, "
                "halted_code, halted_reason FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                if commit:
                    self.db.commit()
                return RunResumeAdmission(admitted=False)
            try:
                params = json.loads(row["params"] or "{}")
            except (TypeError, ValueError):
                params = {}
            if not isinstance(params, dict):
                params = {}
            # The resume-admission decision reads the column.
            # This is the delicate reader the spec names — it runs INSIDE the
            # BEGIN IMMEDIATE, so it must see exactly what the transaction
            # will then clear and what a refused resume will restore. The
            # ``params`` fallback covers rows written before the column
            # existed; the one writer (runner/finalization) fills both from
            # the same locals, so the two can never disagree.
            halted_code = row["halted_code"]
            if halted_code is None:
                halted_code = params.get("halted_code")
            is_internal_halt = (
                isinstance(halted_code, str) and halted_code in resumable_halt_codes
            )
            status = str(row["status"])
            prior = RunResumeAdmission(
                admitted=False,
                prior_status=status,
                prior_finished_at=row["finished_at"],
                prior_params=row["params"],
                prior_cancel_requested_at=row["cancel_requested_at"],
                prior_halted_code=row["halted_code"],
                prior_halted_reason=row["halted_reason"],
            )
            from frisket.engine.store.result_generations import ResultGenerationStore

            if any(
                binding.state == "sealed"
                for binding in ResultGenerationStore(self.project).bindings_for_run(
                    run_id
                )
            ):
                if commit:
                    self.db.commit()
                return prior
            queued_resume_admitted = status == "running" or (
                status == "cancelled" and is_internal_halt
            )
            if not reopen_operator_cancelled and not queued_resume_admitted:
                if commit:
                    self.db.commit()
                return prior
            params.pop("halted_code", None)
            params.pop("halted_reason", None)
            transient_params = json.dumps(params, sort_keys=True)
            transient_cancel_requested_at = (
                row["cancel_requested_at"] if status == "running" else None
            )
            self.db.execute(
                "UPDATE runs SET status='running', finished_at=NULL, params=?, "
                "cancel_requested_at=?, halted_code=NULL, "
                "halted_reason=NULL WHERE id=?",
                (transient_params, transient_cancel_requested_at, run_id),
            )
            if commit:
                self.db.commit()
            return replace(
                prior,
                admitted=True,
                transient_params=transient_params,
                transient_cancel_requested_at=transient_cancel_requested_at,
            )
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def revert_run_resume(
        self, run_id: int, admission: RunResumeAdmission, *, commit: bool = True
    ) -> None:
        """Undo :meth:`begin_run_resume` for a resume that never dispatched.

        Restores status, ``finished_at`` AND ``params`` (the halt markers
        admission cleared) to the exact pre-resume state — a run whose resume
        refused before the first row must be left indistinguishable from one
        that was never resumed, including its awaiting-reconsent markers.
        A no-op when admission never flipped the row.
        """
        if (
            not admission.admitted
            or admission.prior_status is None
            or admission.transient_params is None
        ):
            return
        # This is rollback of OUR transient resume state, not a generic
        # "restore the old row" write. A sibling invocation may have observed
        # the transient running state, won the attempt claim, and become the
        # run's live writer before this invocation learns that its own claim
        # lost. The claim transaction publishes both the dispatching attempt
        # and current_attempt_id atomically. Refuse to overwrite either that
        # winner or any other concurrent transition, and match every field
        # begin_run_resume itself wrote so an unrelated update cannot be
        # erased.
        self.db.execute(
            "UPDATE runs SET status=?, finished_at=?, params=?, "
            "cancel_requested_at=?, halted_code=?, halted_reason=? WHERE id=? "
            "AND status='running' AND finished_at IS NULL AND params=? "
            "AND cancel_requested_at IS ? "
            "AND halted_code IS NULL AND halted_reason IS NULL "
            "AND current_attempt_id IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM execution_attempts "
            "WHERE run_id=? AND state='dispatching'"
            ")",
            (
                admission.prior_status,
                admission.prior_finished_at,
                admission.prior_params,
                admission.prior_cancel_requested_at,
                admission.prior_halted_code,
                admission.prior_halted_reason,
                run_id,
                admission.transient_params,
                admission.transient_cancel_requested_at,
                run_id,
            ),
        )
        if commit:
            self.db.commit()

    def point_column_at_run(
        self, op_id: int, column_id: int, run_id: int, *, commit: bool = True
    ) -> None:
        """Move a column pointer, capturing prior state for undo."""
        prev = self.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()[0]
        op = self.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (op_id,)
        ).fetchone()
        info = json.loads(op["undo_info"] or "{}") if op else {}
        # keep the FIRST prior pointer: repeat pointing within one op must
        # undo to the pre-op state, not an intermediate run
        info.setdefault("column_pointers", {}).setdefault(str(column_id), prev)
        info.setdefault("column_pointers_after", {})[str(column_id)] = run_id
        self.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(info), op_id)
        )
        self.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (run_id, column_id)
        )
        if commit:
            self.db.commit()
            self.project.refresh_pending_review_summary()
