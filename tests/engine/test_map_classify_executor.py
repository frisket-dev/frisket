from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    UndoRerun,
    case_env,
)
from frisket.ai.llm import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.engine.store import Project
from frisket.actions.classify import ClassifyField, ClassifyParams
from frisket.actions.types import Row, RowError
from frisket.engine.executor.classify_read import (
    LOCAL_SEMANTIC_CHUNK_CHARS,
    LOCAL_SEMANTIC_MAX_CHUNKS,
    AdmittedClassifier,
    local_semantic_chunks,
)

# Full-schema payload: the Anthropic path validates output against the full
# response schema (include_justification/include_confidence make those
# fields required).
REPLY = {
    "beat": "accountability",
    "beat_justification": "The story involves a no-bid contract.",
    "beat_confidence": 0.91,
    "risk_score": 8,
    "risk_score_justification": "Public spending deserves scrutiny.",
    "risk_score_confidence": 0.8,
}

# Reset by _patch_router at the start of every harness test for this case;
# asserts on it are only meaningful behind that patch.
_REQUESTS: list[LLMRequest] = []


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        _REQUESTS.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=61,
            tokens_out=23,
            cost=0.004,
            model=req.model,
        )


class _FailingAdapter:
    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        _REQUESTS.append(req)
        raise LLMError("provider unavailable", status=503, retryable=False)


def _stub_router(reply: dict[str, Any]) -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _StubAdapter(reply)  # noqa: SLF001
    return router


def _failing_router() -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = _FailingAdapter()  # noqa: SLF001
    return router


def _patch_router(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    del monkeypatch
    _REQUESTS.clear()
    return {"router": _stub_router(REPLY)}


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Stories")
    columns = {
        "story": project.add_column(sheet_id, "story", type="text"),
        "source_url": project.add_column(sheet_id, "source_url", type="link"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "story": "City hall awarded a no-bid contract",
                "source_url": "https://example.com/story/1",
            },
            {
                "story": "Routine road work finished early",
                "source_url": "https://example.com/story/2",
            },
        ],
        columns,
    )
    return {"sheet_id": sheet_id}


def _map_classify_action(
    sheet_id: int,
    *,
    idempotency_key: str = "map_classify@sha256:stable",
) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify city news stories for an accountability desk.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                    "description": "Primary reporting beat.",
                },
                {
                    "name": "risk_score",
                    "type": "score",
                    "description": "Public-interest risk from 0 to 10.",
                },
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": idempotency_key,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_classify_action(seeded["sheet_id"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_classify_action(
        seeded["sheet_id"], idempotency_key="map_classify@sha256:missing-capability"
    )
    del action["params"]["source"]
    return action


def _bad_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_classify_action(
        seeded["sheet_id"], idempotency_key="map_classify@sha256:bad-input"
    )
    action["params"]["source"] = ["missing_column"]
    return action


def _collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _map_classify_action(
        seeded["sheet_id"], idempotency_key="map_classify@sha256:collision"
    )
    action["params"]["fields"][0]["name"] = "story"
    return action


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.store.runs import RunResultStore

    sheet_id = seeded["sheet_id"]
    assert result.run_id is not None
    assert len(_REQUESTS) == 2
    assert {output.name for output in result.outputs} >= {
        "beat",
        "beat_justification",
        "risk_score",
        "risk_score_justification",
        "beat_confidence",
    }
    assert {output.ref["role"] for output in result.outputs} >= {
        "beat",
        "beat_justification",
        "beat_confidence",
    }

    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run is not None
    assert run["action_kind"] == "map.classify"
    assert run["model"] == "anthropic/claude-haiku-4-5"
    assert run["status"] == "completed"
    assert run["total_rows"] == 2
    assert run["completed_rows"] == 2
    assert run["cost_estimate"] is not None
    assert run["cost_actual"] == 0.008

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    assert columns["beat"]["type"] == "category"
    assert columns["beat"]["ai_generated"] == 1
    assert columns["beat"]["current_run_id"] == result.run_id
    assert columns["risk_score"]["type"] == "integer"
    assert columns["beat_confidence"]["type"] == "number"
    beat_values = project.get_values(sheet_id, int(columns["beat"]["id"]))
    assert list(beat_values.values()) == ["accountability", "accountability"]

    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 2
    assert {row["provider"] for row in model_calls} == {"anthropic"}
    assert {row["engine"] for row in model_calls} == {"anthropic/claude-haiku-4-5"}
    assert {row["capability"] for row in model_calls} == {"llm.complete"}
    assert {row["provider_cost_usd"] for row in model_calls} == {0.004}

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] == result.run_id
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "map.classify"
    assert receipt.provider_use
    assert receipt.provider_use[0]["provider"] == "anthropic"
    assert receipt.provider_use[0]["model"] == "anthropic/claude-haiku-4-5"
    assert receipt.provider_use[0]["model_call_count"] == 2
    assert receipt.provider_use[0]["cost_actual"] == 0.008
    assert {item.ref["kind"] for item in receipt.inputs} >= {
        "model_rows_input_column",
    }
    assert {item.ref["kind"] for item in receipt.outputs} == {"map_result_column"}
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "typed_action_request",
        "map_rows_run_counts",
    }


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _make_action(seeded)
    action["params"]["context"] = "Different classification context."
    return action


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='beat'",
        (seeded["sheet_id"],),
    )
    project.db.commit()


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    row = project.db.execute(
        "SELECT id, hidden FROM columns WHERE sheet_id=? AND name='beat'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert row is not None
    assert row["hidden"] == 1
    seeded["beat_column_id"] = int(row["id"])


def _rerun_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _map_classify_action(
        seeded["sheet_id"], idempotency_key="map_classify@sha256:undo-rerun"
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first
    revived = project.db.execute(
        "SELECT id, hidden, current_run_id FROM columns "
        "WHERE sheet_id=? AND name='beat'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert revived is not None
    assert int(revived["id"]) == seeded["beat_column_id"]
    assert revived["hidden"] == 0
    assert revived["current_run_id"] is None


CASES = [
    ExecutorCase(
        kind="map.classify",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_model_router",
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    "write_model_calls",
                    "write_receipt",
                    "write_trace",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "output_column_exists",
                    "model_cost_requires_confirmation",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                    "idempotency_stale_running",
                    "stale_replay",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_router,
        gates=(
            Gate(
                "missing_source",
                _missing_source_action,
                "invalid_action_request",
            ),
            Gate("invalid_input_ref", _bad_input_action, "invalid_input_ref"),
            Gate(
                "output_column_exists",
                _collision_action,
                "output_column_exists",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            # five output columns: beat + justification + confidence,
            # risk_score + justification (score confidence is not routed)
            "columns": 5,
            "runs": 1,
            "results": 10,
            "model_calls": 2,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            make_conflict=_conflict_action,
            go_stale=_go_stale,
        ),
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_rerun_action,
            check_rerun=_check_rerun,
        ),
    )
]


def _patch_local_semantic_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    from frisket.ai.embeddings.capabilities import build_batch_result
    from frisket.ai.embeddings.gateway import EmbeddingGateway

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "2")
    embedded: list[list[str]] = []

    def fake_embed(  # noqa: ANN001
        self, texts, *, provider, model, modality, local_capability=None
    ):
        del self
        assert provider == "fastembed"
        assert model == "BAAI/bge-small-en-v1.5"
        assert modality == "text"
        assert local_capability == "providerless_classify"
        embedded.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "government contracts" in lowered:
                vectors.append([1.0, 0.0])
            elif "roads and public infrastructure" in lowered:
                vectors.append([0.0, 1.0])
            elif "no clear topic" in lowered:
                vectors.append([-1.0, 0.0])
            elif "no-bid contract" in lowered:
                vectors.append([0.95, 0.05])
            elif "road work" in lowered:
                vectors.append([0.1, 0.9])
            else:
                vectors.append([0.0, 0.0])
        return build_batch_result(
            vectors,
            provider_id="fastembed",
            provider_kind="local_process",
            actual_model_id="fastembed/BAAI/bge-small-en-v1.5",
        )

    monkeypatch.setattr(EmbeddingGateway, "embed", fake_embed)
    return embedded


def _local_semantic_action(sheet_id: int, *, idempotency_key: str) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "local_semantic",
            "fields": [
                {
                    "name": "topic",
                    "type": "category",
                    "labels": [
                        "accountability",
                        "infrastructure",
                        "other / unclear",
                    ],
                    "label_descriptions": {
                        "accountability": "Government contracts, procurement, and public accountability.",
                        "infrastructure": "Roads and public infrastructure projects.",
                        "other / unclear": "No clear topic match from the other descriptions.",
                    },
                }
            ],
        },
        "idempotency_key": idempotency_key,
    }


@pytest.mark.parametrize("retired_name", ["minimum_similarity", "minimum_margin"])
def test_map_classify_contract_rejects_retired_review_thresholds(
    retired_name: str,
) -> None:
    from pydantic import ValidationError

    params = _local_semantic_action(
        1, idempotency_key="map_classify_local@sha256:retired-param"
    )["params"]
    params[retired_name] = 0.1

    with pytest.raises(ValidationError, match=retired_name):
        ClassifyParams.model_validate(params)


def test_map_classify_local_semantic_is_provider_free_and_emits_one_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.runner.review import review_queue
    from frisket.engine.store.runs import RunResultStore

    embedded = _patch_local_semantic_embedding(monkeypatch)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        _REQUESTS.clear()
        action = _local_semantic_action(
            env.seeded["sheet_id"],
            idempotency_key="map_classify_local@sha256:one-output",
        )
        result = env.run(action)

        assert result.status == "completed", result.errors
        assert result.run_id is not None
        assert _REQUESTS == []
        assert RunResultStore(env.project).model_calls(result.run_id) == []
        run = env.project.db.execute(
            "SELECT model, cost_actual FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run["model"] == "fastembed/BAAI/bge-small-en-v1.5"
        assert run["cost_actual"] == 0.0
        columns = {
            row["name"]: row
            for row in env.project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (env.seeded["sheet_id"],)
            ).fetchall()
        }
        assert {name for name in columns if name.startswith("topic")} == {"topic"}
        assert list(
            env.project.get_values(
                env.seeded["sheet_id"], int(columns["topic"]["id"])
            ).values()
        ) == ["accountability", "infrastructure"]
        queue = review_queue(env.project, sheet_id=env.seeded["sheet_id"])
        assert len(queue) == 2
        assert {item["column_name"] for item in queue} == {"topic"}
        assert embedded[0] == [
            "Government contracts, procurement, and public accountability.",
            "Roads and public infrastructure projects.",
            "No clear topic match from the other descriptions.",
        ]


def test_map_classify_local_semantic_leaves_unrelated_companion_named_columns_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_local_semantic_embedding(monkeypatch)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        legacy_column_ids: list[int] = []
        for name, column_type in (
            ("topic_similarity", "number"),
            ("topic_margin", "number"),
            ("topic_needs_review", "boolean"),
        ):
            legacy_column_ids.append(
                env.project.add_column(
                    env.seeded["sheet_id"],
                    name,
                    type=column_type,
                    ai_generated=True,
                )
            )
        result = env.run(
            _local_semantic_action(
                env.seeded["sheet_id"],
                idempotency_key="map_classify_local@sha256:retire-companions",
            )
        )

        assert result.status == "completed", result.errors
        columns = {
            row["name"]: row
            for row in env.project.columns(env.seeded["sheet_id"], include_hidden=True)
        }
        assert columns["topic"]["default_hidden"] == 0
        assert {
            name
            for name, column in columns.items()
            if name.startswith("topic_") and column["default_hidden"]
        } == set()
        assert {
            int(columns[name]["id"])
            for name in ("topic_similarity", "topic_margin", "topic_needs_review")
        } == set(legacy_column_ids)
        assert {output.name for output in result.outputs} == {"topic"}


def test_map_classify_providerless_work_stays_deterministically_bounded() -> None:
    chunks = local_semantic_chunks("x" * (LOCAL_SEMANTIC_CHUNK_CHARS * 20))

    assert len(chunks) == LOCAL_SEMANTIC_MAX_CHUNKS == 8
    assert all(len(chunk) <= LOCAL_SEMANTIC_CHUNK_CHARS for chunk in chunks)


def test_providerless_classifier_downloads_pinned_snapshot_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.semantic as semantic
    import huggingface_hub
    from frisket.ai.models import artifact_manifest
    from frisket.engine._workers import parakeet_artifacts

    cache = tmp_path / "hub"
    monkeypatch.setattr(parakeet_artifacts, "huggingface_hub_cache", lambda: cache)
    artifact = artifact_manifest.providerless_classify_artifact()
    assert artifact is not None and artifact.hf_snapshot is not None
    source = artifact.hf_snapshot
    calls: list[dict[str, Any]] = []

    def download(repo_id: str, **kwargs: Any) -> None:
        calls.append({"repo_id": repo_id, **kwargs})
        snapshot = (
            cache
            / f"models--{source.repo_id.replace('/', '--')}"
            / "snapshots"
            / source.revision
        )
        snapshot.mkdir(parents=True)
        for name in source.files:
            (snapshot / name).write_bytes(b"fixture")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)

    first = semantic.ensure_providerless_classifier_installed()
    second = semantic.ensure_providerless_classifier_installed()

    assert first == second
    assert calls == [
        {
            "repo_id": source.repo_id,
            "revision": source.revision,
            "cache_dir": cache,
            "allow_patterns": list(source.files),
            "token": False,
        }
    ]


@pytest.mark.asyncio
async def test_providerless_classifier_reports_first_use_download_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.engine.executor.classify_read as classify_read
    from frisket.semantic import ProviderlessClassifierProvisionError

    def fail() -> None:
        raise ProviderlessClassifierProvisionError(
            "The Local semantic model could not be downloaded. Check the network "
            "connection and retry."
        )

    monkeypatch.setattr(classify_read, "ensure_providerless_classifier_installed", fail)
    classifier = AdmittedClassifier()
    with pytest.raises(RowError) as raised:
        await classifier.classify(
            Row({}),
            "A story requiring classification",
            (ClassifyField(name="topic", labels=["news", "other"]),),
        )
    assert raised.value.code == "model_unavailable"
    assert "could not be downloaded" in raised.value.message


@pytest.mark.asyncio
async def test_map_classify_global_fence_refuses_before_fastembed_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.embeddings import EmbeddingBackendUnavailable

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.delenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", raising=False)
    real_import = builtins.__import__

    def no_fastembed_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "fastembed" or name.startswith("fastembed."):
            raise AssertionError("the disabled Classify path must not import FastEmbed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastembed_import)
    with pytest.raises(EmbeddingBackendUnavailable):
        await AdmittedClassifier().classify(
            Row({}),
            "A story requiring classification",
            (ClassifyField(name="topic", labels=["news", "other"]),),
        )


def test_map_classify_replay_and_refusals_never_reenter_the_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert len(_REQUESTS) == 2

        confirmed_replay = env.case.make_action(env.seeded)
        replay = env.run(confirmed_replay)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert len(_REQUESTS) == 2

        conflict = env.run(_conflict_action(env.seeded))
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert len(_REQUESTS) == 2


def test_map_classify_stale_running_reservation_returns_recovery_without_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running reservation with an ancient created_at is treated as
    abandoned: the run is refused with a retryable idempotency_stale_running
    and the stale receipt is deleted — never billed model work."""
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_request_hash

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _map_classify_action(
            env.seeded["sheet_id"],
            idempotency_key="map_classify@sha256:stale-running",
        )
        params_hash = typed_request_hash(typed_action_for_request(action))
        stale_receipt = Receipt(
            receipt_id="receipt_stale_map_classify",
            project_id=env.project_id,
            action_id="act_stale_map_classify",
            action_kind="map.classify",
            idempotency_key=action["idempotency_key"],
            params_hash=params_hash,
            status="running",
            inputs=[
                ReceiptIO(
                    name="idempotency",
                    ref={
                        "kind": "map_classify_idempotency_reservation",
                        "params_hash": params_hash,
                    },
                )
            ],
        )
        env.project.db.execute(
            "INSERT INTO receipts (id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stale_receipt.receipt_id,
                stale_receipt.action_kind,
                stale_receipt.action_id,
                stale_receipt.idempotency_key,
                stale_receipt.params_hash,
                stale_receipt.status,
                json.dumps(stale_receipt.model_dump(mode="json"), sort_keys=True),
                "2000-01-01 00:00:00",
            ),
        )
        env.project.db.commit()
        before = env.counts()

        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_stale_running"
        assert result.errors[0].details["receipt_id"] == stale_receipt.receipt_id
        assert result.errors[0].details["retryable"] is True
        assert env.counts() == {**before, "receipts": before["receipts"] - 1}
        assert _REQUESTS == []


def test_map_classify_action_fails_when_every_row_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-row provider failures persist the full failed trail — a completed
    run with failed_rows, per-row error results, a failed receipt — and the
    failed receipt replays stably without model work."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        from frisket.contracts.action import Receipt

        env.run_kwargs["router"] = _failing_router()
        action = _map_classify_action(
            env.seeded["sheet_id"], idempotency_key="map_classify@sha256:all-failed"
        )
        result = env.run(action)
        assert result.status == "failed"
        assert result.receipt_id is not None
        assert result.run_id is not None
        assert result.errors[0].code == "model_run_failed"
        assert len(_REQUESTS) == 2

        run = env.project.db.execute(
            "SELECT * FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run is not None
        assert run["status"] == "completed"
        assert run["total_rows"] == 2
        assert run["completed_rows"] == 2
        assert run["failed_rows"] == 2

        error_rows = int(
            env.project.db.execute(
                "SELECT COUNT(DISTINCT row_id) FROM results "
                "WHERE run_id=? AND error IS NOT NULL",
                (result.run_id,),
            ).fetchone()[0]
        )
        assert error_rows == 2

        receipt_row = env.project.db.execute(
            "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row["status"] == "failed"
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "model_run_failed"
        assert receipt.errors[0].details == {
            "total_rows": 2,
            "completed_rows": 2,
            "failed_rows": 2,
        }

        before = env.counts()
        replay = env.run(action)
        assert replay.status == "failed"
        assert replay.receipt_id == result.receipt_id
        assert replay.run_id == result.run_id
        assert replay.errors[0].code == "model_run_failed"
        assert env.counts() == before
        assert len(_REQUESTS) == 2


def test_map_classify_cache_hits_write_non_billable_model_call_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.engine.store.runs import RunResultStore

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _map_classify_action(
            env.seeded["sheet_id"], idempotency_key="map_classify@sha256:cache-hit"
        )
        params = action["params"]
        plan = build_typed_map_rows_plan(env.project, typed_action_for_request(action))
        cache = ResponseCache(tmp_path / "map-classify-cache.db")
        for row in (
            {"story": "City hall awarded a no-bid contract"},
            {"story": "Routine road work finished early"},
        ):
            call = plan.program.render(row, plan.spec_dict())
            req = LLMRequest(
                model=params["model"],
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            cache.put(
                request_key(req, plan.program.version),
                LLMResponse(
                    content=json.dumps(REPLY),
                    data=dict(REPLY),
                    tokens_in=61,
                    tokens_out=23,
                    cost=0.004,
                    model=params["model"],
                ),
            )
        env.run_kwargs["router"] = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )

        result = env.run(action)
        assert result.status == "completed", result.errors
        assert result.run_id is not None

        run = env.project.db.execute(
            "SELECT * FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run["cost_actual"] == 0.0

        model_calls = RunResultStore(env.project).model_calls(result.run_id)
        assert len(model_calls) == 2
        assert {row["cost_source"] for row in model_calls} == {"cache_hit"}
        assert {row["provider_reported_cost_usd"] for row in model_calls} == {0.0}
        assert {row["provider_cost_usd"] for row in model_calls} == {0.0}
        # The zero-provider-cost + cache-evidence invariant: customer
        # billability and credit_charge_usd are private settlement outputs,
        # not open fact columns; the fact row carries credential provenance.
        assert {row["credential_source"] for row in model_calls} == {"cache"}
        for row in model_calls:
            assert not set(row.keys()) & {
                "billable",
                "credit_charge_usd",
                "billing_owner",
            }
        assert {json.loads(row["units"])["tokens_in"] for row in model_calls} == {61}
        assert {json.loads(row["units"])["tokens_out"] for row in model_calls} == {23}
        assert {json.loads(row["cache"]).get("hit") for row in model_calls} == {True}

        receipt = Receipt.model_validate(
            json.loads(
                env.project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
                ).fetchone()["body"]
            )
        )
        assert receipt.provider_use
        assert receipt.provider_use[0]["model_call_count"] == 2
        assert receipt.provider_use[0]["cost_actual"] == 0.0
