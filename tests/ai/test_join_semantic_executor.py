from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.store import Project

VECS = {
    "Acme Corporation": [1.0, 0.0, 0.0],
    "Globex LLC": [0.0, 1.0, 0.0],
    "Initech Inc": [0.0, 0.0, 1.0],
    "ACME Corp": [1.0, 0.0, 0.0],
    "Globex": [0.6258, 0.78, 0.0],
    "Umbrella Holdings": [0.5774, 0.5774, 0.5774],
}

# Reset by _patch_embedder at the start of every harness test for this case.
# _BACKEND lets a gate's prepare hook simulate the embedding backend going
# away without touching monkeypatch state mid-test.
_EMBED_CALLS: list[list[str]] = []
_BACKEND = {"available": True}


def _fake_embed(texts: list[str]) -> list[list[float]]:
    _EMBED_CALLS.append(list(texts))
    return [VECS[text] for text in texts]


def _patch_embedder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _EMBED_CALLS.clear()
    _BACKEND["available"] = True
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (
            (_fake_embed, "stub/semantic-join") if _BACKEND["available"] else None
        ),
    )
    return {"router": ModelRouter(cache=None, cache_mode="off")}


def _join_action(
    source_sheet_id: int,
    target_sheet_id: int,
    *,
    idempotency_key: str = "join_semantic@sha256:v1",
    child_sheet_name: str = "Semantic Links",
) -> dict[str, Any]:
    return {
        "action_id": "join.semantic",
        "scope": {"kind": "sheet_rows", "sheet_id": source_sheet_id},
        "sheet_name": child_sheet_name,
        "output_names": {
            "source": "donor",
            "match_value": "company_match_value",
            "match_score": "company_match_score",
            "matched_row_id": "company_match_row_id",
        },
        "params": {
            "source": "donor",
            "target": {"sheet_id": target_sheet_id, "column": "company"},
            "match_threshold": 0.70,
            "confident_threshold": 0.85,
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    source_sheet_id = project.add_sheet("Donors")
    source_columns = {
        "donor": project.add_column(source_sheet_id, "donor", type="text"),
        "location": project.add_column(source_sheet_id, "location", type="text"),
    }
    source_row_ids = project.add_rows(
        source_sheet_id,
        [
            {"donor": "ACME Corp", "location": "Boston"},
            {"donor": "Globex", "location": "NYC"},
            {"donor": "Umbrella Holdings", "location": "LA"},
        ],
        source_columns,
    )
    target_sheet_id = project.add_sheet("Registry")
    target_columns = {
        "company": project.add_column(target_sheet_id, "company", type="text")
    }
    target_row_ids = project.add_rows(
        target_sheet_id,
        [
            {"company": "Acme Corporation"},
            {"company": "Globex LLC"},
            {"company": "Initech Inc"},
        ],
        target_columns,
    )
    return {
        "source_sheet_id": source_sheet_id,
        "target_sheet_id": target_sheet_id,
        "source_row_ids": source_row_ids,
        "target_row_ids": target_row_ids,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(seeded["source_sheet_id"], seeded["target_sheet_id"])


def _authored_capability_override_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _join_action(
        seeded["source_sheet_id"],
        seeded["target_sheet_id"],
        idempotency_key="join_semantic@sha256:missing-capability",
    )
    action["capabilities"] = ["project:write"]
    return action


def _same_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        seeded["source_sheet_id"],
        seeded["source_sheet_id"],
        idempotency_key="join_semantic@sha256:same-sheet",
        child_sheet_name="Same Sheet Links",
    )


def _no_backend_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        seeded["source_sheet_id"],
        seeded["target_sheet_id"],
        idempotency_key="join_semantic@sha256:no-backend",
        child_sheet_name="No Backend Links",
    )


def _disable_backend(project: Project, seeded: dict[str, Any]) -> None:
    del project, seeded
    _BACKEND["available"] = False


def _conflict_child_name_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        seeded["source_sheet_id"],
        seeded["target_sheet_id"],
        child_sheet_name="Different Links",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    source_sheet_id = seeded["source_sheet_id"]
    source_row_ids = seeded["source_row_ids"]
    target_row_ids = seeded["target_row_ids"]
    assert result.run_id is not None
    assert len(result.op_ids) == 2
    assert _EMBED_CALLS

    source_columns = {
        column["name"]: column for column in project.columns(source_sheet_id)
    }
    match_values = project.get_values(
        source_sheet_id, source_columns["company_match_value"]["id"]
    )
    match_scores = project.get_values(
        source_sheet_id, source_columns["company_match_score"]["id"]
    )
    matched_row_ids = project.get_values(
        source_sheet_id, source_columns["company_match_row_id"]["id"]
    )
    assert match_values[source_row_ids[0]] == "Acme Corporation"
    assert match_values[source_row_ids[1]] == "Globex LLC"
    assert match_values[source_row_ids[2]] is None
    assert match_scores[source_row_ids[0]] == 1.0
    assert matched_row_ids[source_row_ids[0]] == target_row_ids[0]
    assert matched_row_ids[source_row_ids[1]] == target_row_ids[1]
    assert matched_row_ids[source_row_ids[2]] is None

    child = next(
        sheet for sheet in project.sheets() if sheet["name"] == "Semantic Links"
    )
    assert child["parent_sheet_id"] == source_sheet_id
    child_rows = project.db.execute(
        "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (child["id"],),
    ).fetchall()
    assert [int(row["parent_row_id"]) for row in child_rows] == [
        source_row_ids[0],
        source_row_ids[1],
    ]
    child_columns = {column["name"]: column for column in project.columns(child["id"])}
    child_target_row_ids = project.get_values(
        child["id"], child_columns["company_match_row_id"]["id"]
    )
    assert sorted(child_target_row_ids.values()) == sorted(target_row_ids[:2])

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "join.semantic"
    assert receipt.run_id == result.run_id
    assert receipt.op_ids == result.op_ids
    assert receipt.provider_use[0]["provider"] == "stub"
    assert receipt.provider_use[0]["engine"] == "stub/semantic-join"
    # The injected non-fastembed backend returned vectors without usage or
    # cost metadata.  Receipt truth is therefore remote + unknown, never the
    # old invented local/zero classification.
    assert receipt.provider_use[0]["external_api"] is True
    assert receipt.provider_use[0]["cost_actual"] is None
    refs = [
        item.ref
        for section in (receipt.inputs, receipt.outputs, receipt.evidence)
        for item in section
    ]
    ref_kinds = {ref["kind"] for ref in refs}
    assert {
        "semantic_join_source_sheet",
        "semantic_join_source_column",
        "semantic_join_target_sheet",
        "semantic_join_target_column",
        "semantic_join_output_column",
        "semantic_join_link_sheet",
        "semantic_join_link_edges",
        "semantic_join_embedding_backend",
        "semantic_join_thresholds",
    } <= ref_kinds
    edge_ref = next(ref for ref in refs if ref["kind"] == "semantic_join_link_edges")
    assert edge_ref["source_row_ids"] == source_row_ids[:2]
    assert edge_ref["target_row_ids"] == target_row_ids[:2]
    assert len(edge_ref["child_row_ids"]) == 2


CASES = [
    ExecutorCase(
        request_style="typed",
        kind="join.semantic",
        catalog=CatalogEntry(
            execution_mode="cross_sheet",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:embed"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "read_target_sheet_rows",
                    "call_model_router",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_model_calls",
                    "write_trace",
                    "create_link_child_sheet",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_input_ref",
                    "output_column_exists",
                    "output_column_busy",
                    "model_cost_requires_confirmation",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="model_metered",
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_embedder,
        gates=(
            Gate(
                "authored_capability_cannot_override_host",
                _authored_capability_override_action,
                "invalid_action_request",
            ),
            Gate("same_sheet_refused", _same_sheet_action, "invalid_params"),
            Gate(
                "embedding_backend_unavailable",
                _no_backend_action,
                "invalid_params",
                prepare=_disable_backend,
            ),
            Gate(
                "idempotency_conflict",
                _conflict_child_name_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            # three match columns on the source sheet + the child link
            # sheet's own columns
            "columns": 7,
            "rows": 2,
            "runs": 1,
            "ops": 2,
            "receipts": 1,
        },
        check_state=_check_state,
        # Replay reconstructs outputs from the receipt with an extra
        # link_edges entry, so neither name nor kind identity round-trips;
        # receipt/run/op identity and count stability remain asserted.
        replay_output_names=False,
        replay_output_kinds=False,
    )
]


def test_join_semantic_replay_does_not_reembed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct reserved replay passes ActionSpec through the four-argument ABI."""

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        embed_call_count = len(_EMBED_CALLS)
        assert embed_call_count > 0

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_EMBED_CALLS) == embed_call_count


@pytest.mark.parametrize("selected_only", [False, True])
def test_join_semantic_carries_source_columns_into_child_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected_only: bool
) -> None:
    """Design card 5b 'Columns to carry over': carry_columns copies additional
    SOURCE-sheet columns onto the child link sheet alongside the match
    output; an unknown carry column fails invalid_input_ref pre-mutation."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _join_action(
            env.seeded["source_sheet_id"],
            env.seeded["target_sheet_id"],
            idempotency_key="join_semantic@sha256:carry-columns",
            child_sheet_name="Carry Links",
        )
        action["params"]["carry"] = ["location"]
        action["output_names"]["carry.location"] = "location"
        source_row_ids = env.seeded["source_row_ids"]
        admitted_rows = source_row_ids[:2] if selected_only else source_row_ids
        if selected_only:
            action["scope"]["row_ids"] = admitted_rows
        result = env.run(action)
        assert result.status == "completed", result.errors

        project = env.project
        source_row_ids = env.seeded["source_row_ids"]
        child = next(
            sheet for sheet in project.sheets() if sheet["name"] == "Carry Links"
        )
        child_columns = {
            column["name"]: column for column in project.columns(child["id"])
        }
        assert "location" in child_columns
        assert child_columns["location"]["type"] == "text"
        child_rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
            (child["id"],),
        ).fetchall()
        locations = project.get_values(child["id"], child_columns["location"]["id"])
        by_parent = {row["parent_row_id"]: locations[row["id"]] for row in child_rows}
        assert by_parent[source_row_ids[0]] == "Boston"
        assert by_parent[source_row_ids[1]] == "NYC"
        assert source_row_ids[2] not in by_parent

        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        source_location_id = next(
            column["id"]
            for column in project.columns(env.seeded["source_sheet_id"])
            if column["name"] == "location"
        )
        carry_ref = next(
            item.ref
            for item in receipt.inputs
            if item.ref.get("kind") == "semantic_join_carry_column"
        )
        assert carry_ref == {
            "kind": "semantic_join_carry_column",
            "role": "carry",
            "sheet_id": env.seeded["source_sheet_id"],
            "column_id": source_location_id,
            "name": "location",
            "type": "text",
            # This projection identifies values copied into the child. The
            # separate source snapshot still pins all admitted carry inputs.
            "row_ids": source_row_ids[:2],
        }

        before = env.counts()
        bad_action = _join_action(
            env.seeded["source_sheet_id"],
            env.seeded["target_sheet_id"],
            idempotency_key="join_semantic@sha256:carry-columns-bad",
            child_sheet_name="Bad Carry Links",
        )
        bad_action["params"]["carry"] = ["not_a_column"]
        bad_result = env.run(bad_action)
        assert bad_result.status == "failed"
        assert bad_result.errors[0].code == "invalid_input_ref"
        assert env.counts() == before

        # An unmatched row is still an admitted input for whole-sheet runs;
        # an unselected row is not. Carry pinning must preserve that boundary.
        project.apply_edits(
            [
                {
                    "row_id": source_row_ids[2],
                    "column_id": source_location_id,
                    "value": "Changed location",
                }
            ]
        )
        calls_before = list(_EMBED_CALLS)
        replay = env.run(action)
        if selected_only:
            assert replay.status == "completed", replay.errors
            assert replay.receipt_id == result.receipt_id
        else:
            assert replay.status == "failed"
            assert replay.errors[0].code == "stale_replay"
        assert _EMBED_CALLS == calls_before


def test_direct_join_semantic_binds_claim_and_attempt_through_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run_primary()
        assert result.status == "completed", result.errors
        assert result.run_id is not None
        assert result.receipt_id is not None

        claims = env.project.db.execute(
            "SELECT output_name, column_id, status, run_id, claim_token "
            "FROM output_column_claims WHERE receipt_id=? ORDER BY output_name",
            (result.receipt_id,),
        ).fetchall()
        assert len(claims) == 3
        assert {row["status"] for row in claims} == {"released"}
        assert {int(row["run_id"]) for row in claims} == {result.run_id}
        assert {row["claim_token"] for row in claims} == {
            f"output-claim:{result.receipt_id}"
        }
        source_columns = {
            row["name"]: int(row["id"])
            for row in env.project.columns(env.seeded["source_sheet_id"])
        }
        assert {(str(row["output_name"]), int(row["column_id"])) for row in claims} == {
            (name, source_columns[name])
            for name in (
                "company_match_value",
                "company_match_score",
                "company_match_row_id",
            )
        }
        attempts = env.project.db.execute(
            "SELECT id, state FROM execution_attempts WHERE run_id=?",
            (result.run_id,),
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0]["state"] == "effected"
        fact_attempts = {
            row["attempt_id"]
            for row in env.project.db.execute(
                "SELECT attempt_id FROM model_calls WHERE run_id=?",
                (result.run_id,),
            ).fetchall()
        }
        assert fact_attempts == {attempts[0]["id"]}


def test_direct_join_semantic_terminal_fence_blocks_replaced_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.runner import MapRunner

    original_run = MapRunner.run

    async def replace_after_last_batch(
        runner: MapRunner,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        progress = await original_run(runner, *args, **kwargs)
        assert kwargs.get("defer_attempt_close") is True
        attempt_id = progress.writer_attempt_id
        assert attempt_id is not None
        old = runner.project.db.execute(
            "SELECT * FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        assert old is not None
        runner.project.db.execute("BEGIN IMMEDIATE")
        runner.project.db.execute(
            "UPDATE execution_attempts SET state='abandoned' WHERE id=?",
            (attempt_id,),
        )
        replacement_id = f"{attempt_id}_replacement"
        runner.project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, "
            "head_route_id, head_promise_set_id, admitted_by_consent_id, "
            "cost_basis_json, price_card_version, evaluation_json, created_at) "
            "VALUES (?, ?, ?, 'dispatching', ?, ?, ?, ?, ?, ?, ?, ?, "
            "datetime('now'))",
            (
                replacement_id,
                old["run_id"],
                int(old["seq"]) + 1,
                old["action_identity_hash"],
                old["scope_json"],
                old["head_route_id"],
                old["head_promise_set_id"],
                old["admitted_by_consent_id"],
                old["cost_basis_json"],
                old["price_card_version"],
                old["evaluation_json"],
            ),
        )
        runner.project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (replacement_id, old["run_id"]),
        )
        runner.project.db.commit()
        return progress

    monkeypatch.setattr(MapRunner, "run", replace_after_last_batch)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        result = env.run_primary()
        assert result.status == "failed"
        assert result.errors[0].code == "stale_attempt_writer"
        assert not any(
            sheet["name"] == "Semantic Links" for sheet in env.project.sheets()
        )
        run = env.project.db.execute(
            "SELECT id, current_attempt_id FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run is not None
        replacement = env.project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (run["current_attempt_id"],),
        ).fetchone()
        assert replacement is not None
        assert replacement["state"] == "dispatching"
        claim = env.project.db.execute(
            "SELECT status FROM output_column_claims WHERE run_id=?",
            (run["id"],),
        ).fetchall()
        assert claim
        assert {row["status"] for row in claim} == {"active"}


def test_direct_join_semantic_claim_conflict_precedes_embedding_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        blocker, conflict = OutputColumnClaimStore(env.project).acquire(
            sheet_id=env.seeded["source_sheet_id"],
            output_names=["company_match_value"],
            action_kind="join.semantic",
            claim_token="output-claim:receipt_join_blocker",
        )
        assert conflict is None
        assert len(blocker) == 1
        before_runs = env.project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        result = env.run_primary()

        assert result.status == "failed"
        assert result.errors[0].code == "output_column_busy"
        assert _EMBED_CALLS == []
        assert (
            env.project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            == before_runs
        )
        assert not any(
            sheet["name"] == "Semantic Links" for sheet in env.project.sheets()
        )
        assert (
            env.project.db.execute(
                "SELECT status FROM output_column_claims "
                "WHERE claim_token='output-claim:receipt_join_blocker'"
            ).fetchone()[0]
            == "active"
        )
