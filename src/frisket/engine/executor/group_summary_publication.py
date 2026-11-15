"""Incremental grouped output publication under one live execution owner."""

from __future__ import annotations

import json
from dataclasses import replace

from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.derived_refresh_stage import DerivedRefreshStage
from frisket.engine.store.derived_updates import (
    DerivedRefreshRow,
    MaterializedIdentity,
    absent_derived_batches,
    complete_derived_membership,
    reconcile_rows_into_open_change,
    refresh_config,
)
from frisket.engine.store.keyed_updates import observation, observe, reconcile_fields
from frisket.engine.store.materialization import (
    AggregateColumnSpec,
    AggregateSheetPlan,
    AggregateSheetWrite,
    materialized_row_sources_ref,
    load_materialized_row_sources_for_op,
    write_aggregate_sheet,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.runner.result_generations import _compatibility_key


class GroupPublicationError(RuntimeError):
    pass


class GroupPublication:
    def __init__(self, project, params, *, receipt_id, params_hash, recovery_run=None):
        self.project, self.params = project, params
        self.receipt_id, self.params_hash = receipt_id, params_hash
        self.stage = DerivedRefreshStage(keyed=True)
        self.write = None
        self.before = {}
        self.positions = {}
        self.claim_token = f"output-claim:{receipt_id}"
        self.envelope = None
        self.completed = 0
        self.recovery_run = recovery_run
        self.saved_capture = None

    def recovery_claim_token(self):
        if self.recovery_run is None:
            return None
        claim = self.project.db.execute(
            "SELECT claim_token FROM output_column_claims WHERE run_id=? AND status='active'",
            (self.recovery_run["id"],),
        ).fetchone()
        return str(claim[0]) if claim is not None else None

    def close(self):
        # This invocation owns its attempt through final receipt publication,
        # not just provider work. Terminal attempts are immutable; an unfinished
        # one is halted so the same execution can recover its retained results.
        try:
            if self.envelope is not None and self.envelope.get("attempt_id"):
                from frisket.execution.attempt import set_attempt_state

                set_attempt_state(
                    self.project, str(self.envelope["attempt_id"]), "halted"
                )
        finally:
            self.stage.close()

    def stopped(self):
        return self.envelope is not None and RunResultStore(
            self.project
        ).cancellation_requested(self.envelope["run_id"])

    def start(self, resolved, envelope):
        try:
            self._start(resolved, envelope)
        except Exception as exc:
            raise GroupPublicationError(str(exc)) from exc

    def _start(self, resolved, envelope):
        from .group_summary_refresh import validate_target
        from .group_summary_runtime import (
            _group_source_binding,
            _estimate_reduce_group_summary_cost,
        )
        from frisket.actions.group_summary_types import GROUP_SUMMARY_COLUMNS

        self.envelope = envelope
        project, params, db = self.project, self.params, self.project.db
        op_id, run_id = int(envelope["op_id"]), int(envelope["run_id"])
        self.resolved = resolved
        op_spec = dict(params.request)
        op_spec["params"] = dict(op_spec["params"])
        op_spec["params"].pop("confirmed", None)
        op_spec["params"].pop("consented_promise_set_hash", None)
        op_spec["params_hash"] = self.params_hash
        op_spec["execution_sources"] = _group_source_binding(resolved)
        self.op_spec = op_spec
        physical = {
            "group": params.group_column_name,
            "rows": params.row_count_column_name,
            "summary": params.summary_column_name,
        }
        columns = [
            AggregateColumnSpec(
                name=physical[c.key], type=c.type, ai_generated=c.key == "summary"
            )
            for c in GROUP_SUMMARY_COLUMNS
        ]
        for group in resolved["groups"]:
            self.stage.add(
                DerivedRefreshRow(
                    key=str(group["identity_key"]),
                    values={
                        params.group_column_name: group["name"],
                        params.row_count_column_name: len(group["source_row_ids"]),
                    },
                    sources=tuple(
                        (int(row), "aggregate_source")
                        for row in group["source_row_ids"]
                    ),
                )
            )
        self.stage.seal()
        if self.recovery_run is not None and ResultGenerationStore(
            project
        ).bindings_for_run(run_id):
            self._resume(resolved, envelope)
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            from frisket.engine.store.receipts import ReceiptStore
            from frisket.contracts.action import ReceiptIO

            receipts = ReceiptStore(project)
            receipt = receipts.parsed_by_id(self.receipt_id)
            receipt = receipt.model_copy(
                update={
                    "run_id": run_id,
                    "op_ids": [op_id],
                    "inputs": [
                        *receipt.inputs,
                        ReceiptIO(
                            name="source_rows",
                            ref={
                                "kind": "reduce_group_summary_source_rows",
                                "sheet_id": resolved["sheet_id"],
                                "row_ids": resolved["source_row_ids"],
                            },
                        ),
                    ],
                }
            )
            if not receipts.update_body_status(
                receipt, require_status="running", commit=False
            ):
                raise ValueError("Grouped receipt reservation was lost")
            ChangeStore(project).start(op_id)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            existing_sheet = (
                db.execute(
                    "SELECT id FROM sheets WHERE parent_op_id=? AND name=? AND hidden=0",
                    (op_id, params.target_sheet_name),
                ).fetchone()
                if self.recovery_run is not None
                else None
            )
            if params.refresh_target is None and existing_sheet is not None:
                self.config = refresh_config(
                    db.execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
                )
                write = self._saved_write(op_id, int(existing_sheet["id"]))
            elif params.refresh_target is None:
                write = write_aggregate_sheet(
                    db.cursor(),
                    AggregateSheetPlan(
                        action_kind=params.action_kind,
                        label=f"Grouped summary {params.target_sheet_name}",
                        target_sheet_name=params.target_sheet_name,
                        parent_sheet_id=int(resolved["sheet_id"]),
                        op_spec=op_spec,
                        columns=columns,
                        rows=[],
                        reuse_op_id=op_id,
                        refresh_identity=MaterializedIdentity(
                            "group",
                            [],
                            [
                                (
                                    int(resolved["group_column"]["column_id"]),
                                    str(resolved["group_column"]["type"]),
                                )
                            ]
                            if resolved["group_column"] is not None
                            else [],
                        ),
                    ),
                )
                self.config = refresh_config(
                    db.execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
                )
            else:
                refusal = validate_target(project, params)
                if refusal is not None:
                    raise ValueError(refusal.message)
                target = params.refresh_target
                self.config = refresh_config(
                    db.execute(
                        "SELECT * FROM ops WHERE id=?", (target.parent_op_id,)
                    ).fetchone()
                )
                ChangeStore(project).start(op_id)
                op_spec.update(
                    sheet_id=target.sheet_id,
                    policy=target.policy,
                    derived_refresh=self.config,
                )
                db.execute(
                    "UPDATE ops SET spec=? WHERE id=?", (json.dumps(op_spec), op_id)
                )
                write = self._saved_write(op_id, target.sheet_id)
            self.write = write
            db.commit()
            rows = []
            for batch in self.stage.batches():
                db.execute("BEGIN IMMEDIATE")
                ChangeStore(project).require_publication(op_id)
                ids = reconcile_rows_into_open_change(
                    db,
                    op_id=op_id,
                    sheet_id=write.sheet_id,
                    config=self.config,
                    columns=columns,
                    rows=[row for _, row, _ in batch],
                    policy=params.refresh_target.policy
                    if params.refresh_target
                    else "incoming_wins",
                )
                if any(row is None for row in ids):
                    raise ValueError("Grouped identities unexpectedly conflict")
                if params.refresh_target:
                    current = read_current_column(
                        db,
                        sheet_id=write.sheet_id,
                        column_id=write.column_ids[params.summary_column_name],
                        row_ids=ids,
                        bounded=False,
                        preserve_invalid=True,
                        include_hidden=True,
                    )
                    field = str(write.column_ids[params.summary_column_name])
                    for row_id in ids:
                        cell = current.get(row_id)
                        self.before[row_id] = (
                            {}
                            if cell is None or not cell.value_present
                            else {field: cell.value},
                            observation(db, self.config["relationship_id"], row_id),
                            bool(
                                db.execute(
                                    "SELECT hidden FROM rows WHERE id=?", (row_id,)
                                ).fetchone()[0]
                            ),
                        )
                db.commit()
                self.stage.record_published(ids)
                for (_, row, _), row_id in zip(batch, ids, strict=True):
                    self.positions[row.key] = row_id
                rows.extend(ids)
            memberships = load_materialized_row_sources_for_op(
                db, op_id=op_id, roles=("aggregate_source",)
            )
            self.write = replace(
                write,
                row_ids=rows,
                source_row_ids_by_row=[g["source_row_ids"] for g in resolved["groups"]],
                materialized_row_sources_ref=materialized_row_sources_ref(
                    op_id=op_id, rows=memberships, include_rows=False
                ),
            )
            db.execute("BEGIN IMMEDIATE")
            ChangeStore(project).require_publication(op_id)
            store = RunResultStore(project)
            store.repoint_run_for_result(
                run_id,
                sheet_id=write.sheet_id,
                params=op_spec["params"],
                total_rows=len(rows),
                cost_estimate=_estimate_reduce_group_summary_cost(params, resolved)[
                    "cost"
                ],
            )
            store.record_run_row_scope(run_id, rows)
            field = {"name": params.summary_column_name, "column_type": "text"}
            claims, conflict = OutputColumnClaimStore(project).acquire(
                sheet_id=write.sheet_id,
                output_names=[params.summary_column_name],
                action_kind=params.action_kind,
                receipt_id=self.receipt_id,
                run_id=run_id,
                op_id=op_id,
                claim_token=self.claim_token,
                details={"output_fields": [field]},
                commit=False,
            )
            if conflict is not None or len(claims) != 1:
                raise ValueError("Grouped summary output is already claimed")
            OutputColumnClaimStore(project).bind_to_run(
                claim_token=self.claim_token,
                run_id=run_id,
                expected_output_names=[params.summary_column_name],
                commit=False,
            )
            ResultGenerationStore(project).declare(
                run_id,
                write.column_ids[params.summary_column_name],
                output_role="summary",
                compatibility_key=_compatibility_key(field=field),
                write_mode="replace_scope" if params.refresh_target else "create",
                claim_token=self.claim_token,
                commit=False,
            )
            from frisket.engine.store.consumed_sources import store_capture

            if isinstance(resolved.get("consumed_capture"), dict):
                store_capture(
                    project,
                    resolved["consumed_capture"],
                    owner_op_id=op_id,
                    output_sheet_id=write.sheet_id,
                    run_id=run_id,
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _saved_write(
        self, op_id, sheet_id, *, row_ids=(), source_row_ids=(), memberships=()
    ):
        """Describe an existing materialization without creating another sheet."""
        return AggregateSheetWrite(
            op_id=op_id,
            sheet_id=sheet_id,
            column_ids=self.config["columns"],
            row_ids=list(row_ids),
            source_row_ids_by_row=list(source_row_ids),
            membership_rows=[],
            undo_info={},
            materialized_sheet_ref={
                "kind": "materialized_sheet",
                "sheet_id": sheet_id,
                "op_id": op_id,
            },
            materialized_column_refs={},
            materialized_rows_ref={},
            materialized_row_sources_ref=materialized_row_sources_ref(
                op_id=op_id, rows=memberships, include_rows=False
            ),
        )

    def _resume(self, resolved, envelope):
        project, db, params = self.project, self.project.db, self.params
        op_id, run_id = int(envelope["op_id"]), int(envelope["run_id"])
        ChangeStore(project).require_publication(op_id)
        binding = ResultGenerationStore(project).bindings_for_run(run_id)[0]
        if binding.state == "sealed":
            raise ValueError("A closed grouped generation cannot resume")
        run = RunResultStore(project).get_run(run_id)
        sheet_id = int(run["sheet_id"])
        self.config = refresh_config(
            db.execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
        )
        rows = RunResultStore(project).run_row_scope(run_id)
        for row in db.execute(
            "SELECT row_id,key FROM keyed_observations WHERE relationship_id=?",
            (self.config["relationship_id"],),
        ):
            if row["row_id"] in rows:
                self.positions[str(row["key"])] = int(row["row_id"])
        if set(self.positions) != set(
            str(g["identity_key"]) for g in resolved["groups"]
        ):
            raise ValueError(
                "Grouped identities changed during recovery; start a new grouped action"
            )
        memberships = load_materialized_row_sources_for_op(
            db, op_id=op_id, roles=("aggregate_source",)
        )
        self.write = self._saved_write(
            op_id,
            sheet_id,
            row_ids=[
                self.positions[str(g["identity_key"])] for g in resolved["groups"]
            ],
            source_row_ids=[g["source_row_ids"] for g in resolved["groups"]],
            memberships=memberships,
        )
        self.claim_token = binding.claim_token
        claim = db.execute(
            "SELECT claim_token FROM output_column_claims WHERE run_id=? AND status='active'",
            (run_id,),
        ).fetchone()
        if claim is not None:
            self.claim_token = str(claim[0])
        else:
            # Recovery replaces an expired claim, never the generation owner.
            db.execute("BEGIN IMMEDIATE")
            claims, conflict = OutputColumnClaimStore(project).acquire(
                sheet_id=sheet_id,
                output_names=[params.summary_column_name],
                action_kind=params.action_kind,
                receipt_id=self.receipt_id,
                run_id=run_id,
                op_id=op_id,
                claim_token=self.claim_token,
                details={
                    "output_fields": [
                        {"name": params.summary_column_name, "column_type": "text"}
                    ]
                },
                commit=False,
            )
            if conflict is not None or len(claims) != 1:
                db.rollback()
                raise ValueError("Grouped recovery output is already claimed")
            OutputColumnClaimStore(project).bind_to_run(
                claim_token=self.claim_token,
                run_id=run_id,
                expected_output_names=[params.summary_column_name],
                commit=False,
            )
            db.commit()
        bundle = db.execute(
            "SELECT capture_json FROM consumed_source_bundles WHERE run_id=? ORDER BY id LIMIT 1",
            (run_id,),
        ).fetchone()
        self.saved_capture = json.loads(bundle[0]) if bundle is not None else None
        self.completed = int(
            db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=? AND column_id=?",
                (run_id, binding.column_id),
            ).fetchone()[0]
        )
        if params.refresh_target:
            self._recover_working_values(rows, binding.column_id, op_id)

    def _recover_working_values(self, rows, column_id, op_id):
        # The normal pending mask intentionally hides the previous working
        # value. Recovery reads the canonical contributions excluding itself.
        current = read_current_column(
            self.project.db,
            sheet_id=self.write.sheet_id,
            column_id=column_id,
            row_ids=rows,
            bounded=False,
            preserve_invalid=True,
            include_hidden=True,
            _exclude_op_id=op_id,
        )
        field = str(column_id)
        for row_id in rows:
            cell = current.get(row_id)
            self.before[row_id] = (
                {} if cell is None or not cell.value_present else {field: cell.value},
                observation(self.project.db, self.config["relationship_id"], row_id),
                bool(
                    self.project.db.execute(
                        "SELECT hidden FROM rows WHERE id=?", (row_id,)
                    ).fetchone()[0]
                ),
            )

    def saved(self, group):
        from frisket.engine.store.value_codec import decode_stored_value

        row_id = self.positions[str(group["identity_key"])]
        result = self.project.db.execute(
            "SELECT * FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (
                self.envelope["run_id"],
                row_id,
                self.write.column_ids[self.params.summary_column_name],
            ),
        ).fetchone()
        if result is None:
            return None
        return {
            "group": group,
            "summary": decode_stored_value(result["value_kind"], result["value"]),
            "error": result["error"],
            "error_code": result["error_code"],
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost": 0.0,
            "model_call": None,
            "consumed_capture": self.saved_capture,
            "direct_effect": self.envelope,
        }

    def publish(self, computation):
        project, params, db = self.project, self.params, self.project.db
        run_id = int(self.envelope["run_id"])
        attempt = str(self.envelope["attempt_id"])
        row_id = self.positions[str(computation["group"]["identity_key"])]
        column_id = self.write.column_ids[params.summary_column_name]
        store = RunResultStore(project)
        db.execute("BEGIN IMMEDIATE")
        try:
            if self.stopped():
                call = computation.get("model_call")
                if isinstance(call, dict):
                    store.write_returned_call_accounting(
                        run_id,
                        [
                            {
                                "row_id": row_id,
                                "column_id": column_id,
                                "model_calls": [call],
                            }
                        ],
                        writer_attempt_id=attempt,
                        claim_token=self.claim_token,
                        claimless_direct_effect=False,
                        authorized_attempt_id=attempt,
                        commit=False,
                    )
                    computation["model_call"] = None
                db.commit()
                return False
            error = computation.get("error")
            value = computation.get("summary")
            effect = (
                "publish_error"
                if error
                else "publish_null"
                if value is None
                else "publish_value"
            )
            if params.refresh_target:
                current, baseline, hidden = self.before[row_id]
                field = str(column_id)
                changes, restore = reconcile_fields(
                    current,
                    {field: value},
                    baseline,
                    params.refresh_target.policy,
                    deleted=hidden,
                )
                if restore:
                    from frisket.engine.store.derived_updates import (
                        restore_derived_rows,
                    )

                    restore_derived_rows(
                        db,
                        op_id=self.write.op_id,
                        sheet_id=self.write.sheet_id,
                        row_ids=[row_id],
                    )
                if field not in changes:
                    effect = "retain"
            result = {
                "row_id": row_id,
                "column_id": column_id,
                "value": value,
                "tokens_in": computation.get("tokens_in"),
                "tokens_out": computation.get("tokens_out"),
                "confidence": None,
                "justification": None,
                "error": error,
                "error_code": computation.get("error_code"),
                "cost": float(computation.get("cost") or 0),
                "publication_effect": effect,
            }
            if isinstance(computation.get("model_call"), dict):
                result["model_calls"] = [
                    {
                        **computation["model_call"],
                        "run_id": run_id,
                        "row_id": row_id,
                        "column_id": column_id,
                    }
                ]

            def publish_result(checkpoint=None):
                authorized = (
                    attempt
                    if checkpoint is None
                    else checkpoint["authorized_attempt_id"]
                )
                store.write_results(
                    run_id,
                    [result],
                    writer_attempt_id=attempt,
                    authorized_attempt_id=authorized,
                    claim_token=self.claim_token,
                    commit=False,
                )
                capture = computation.get("consumed_capture")
                if isinstance(capture, dict):
                    from frisket.engine.store.consumed_sources import (
                        capture_fingerprint,
                        store_capture,
                    )

                    known = db.execute(
                        "SELECT 1 FROM consumed_source_bundles WHERE run_id=? AND input_fingerprint=? LIMIT 1",
                        (run_id, capture_fingerprint(capture)),
                    ).fetchone()
                    if known is None:
                        store_capture(
                            project,
                            capture,
                            owner_op_id=self.write.op_id,
                            output_sheet_id=self.write.sheet_id,
                            run_id=run_id,
                            row_id=row_id,
                        )
                if not error:
                    observe(
                        db,
                        relationship_id=self.config["relationship_id"],
                        row_id=row_id,
                        key=str(computation["group"]["identity_key"]),
                        incoming={str(column_id): value},
                    )

            effect = computation.get("effect")
            if effect:
                from frisket.engine.store.effect_checkpoints import (
                    EffectCheckpointStore,
                )

                EffectCheckpointStore(db).consume_and_retire(
                    str(effect["checkpoint_id"]),
                    family=str(effect["family"]),
                    group_key=str(effect["group_key"]),
                    unit_key=str(effect["unit_key"]),
                    action_kind=params.action_kind,
                    identity=str(effect["identity"]),
                    run_id=run_id,
                    writer_attempt_id=attempt,
                    claim_token=self.claim_token,
                    finalize=publish_result,
                    commit=False,
                )
            else:
                publish_result()
            db.commit()
            self.completed += 1
            return True
        except BaseException:
            db.rollback()
            raise

    def complete_membership(self):
        if self.params.refresh_target is None or self.stopped():
            return
        db = self.project.db
        for batch in absent_derived_batches(db, config=self.config, seen=self.stage):
            db.execute("BEGIN IMMEDIATE")
            try:
                if self.stopped():
                    db.rollback()
                    return
                complete_derived_membership(
                    db,
                    op_id=self.write.op_id,
                    sheet_id=self.write.sheet_id,
                    config=self.config,
                    observed_rows=batch,
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
