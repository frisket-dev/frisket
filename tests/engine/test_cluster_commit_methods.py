"""cluster.values commit: method dispatch (ngram / semantic), group edits
(canonical overrides + member exclusions), undo, and knob/collision guards.

In-process (executor.run_action_spec) so the semantic method can inject a stub
embedder — NO live model calls (mirrors tests/test_cluster_semantic.py).
"""

from __future__ import annotations

import uuid
from typing import Any

from frisket.engine.executor import actions as executor_actions
from frisket.engine.store import Project

from tests.engine.test_cluster_semantic import StubEmbedder  # type: ignore

PROJECT_ID = "project-cluster-commit"


def _seed(tmp_path, values, name="org"):
    project = Project.create(tmp_path / "commit.frisket", name="commit")
    sheet_id = project.add_sheet("data")
    col = project.add_column(sheet_id, name, type="text")
    project.add_rows(sheet_id, [{name: v} for v in values], {name: col})
    return project, sheet_id


def _commit(sheet_id, *, input_column="org", method="fingerprint", **params):
    body: dict[str, Any] = {
        "source": input_column,
        "method": method,
        **params,
    }
    return {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"canonical": f"{input_column}_canonical"},
        "params": body,
        "idempotency_key": f"cluster.values@sha256:{uuid.uuid4().hex}",
    }


def _canonical(project, sheet_id, name="org_canonical"):
    col = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    assert col is not None, f"{name} column missing"
    return project.get_values(sheet_id, int(col["id"]))


def test_ngram_commit_merges_spacing_variants(tmp_path):
    project, sid = _seed(tmp_path, ["Sao Paulo", "SaoPaulo", "sao paulo", "Rio"])
    try:
        result = executor_actions.run_action_spec(
            project, _commit(sid, method="ngram_fingerprint"), project_id=PROJECT_ID
        )
        assert result.status == "completed", result.errors
        canonical = _canonical(project, sid)
        vals = list(canonical.values())
        # the three spacing variants collapse to a single canonical
        merged = [v for v in vals if v in ("Sao Paulo", "SaoPaulo", "sao paulo")]
        assert len(set(merged)) == 1
    finally:
        project.close()


def test_semantic_commit_with_injected_embedder(tmp_path, monkeypatch):
    # Resolve the embedder before the transaction so provider setup cannot
    # hold the write lock while doing external initialization.
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (StubEmbedder(), "fastembed/test"),
    )
    project, sid = _seed(
        tmp_path, ["ACME Corp", "ACME Corp", "Acme Corporation", "ACME Inc."]
    )
    try:
        result = executor_actions.run_action_spec(
            project,
            _commit(sid, method="semantic", threshold=0.9),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        canonical = _canonical(project, sid)
        # meaning-level variants share one canonical (fingerprint can't collide)
        assert canonical[list(canonical)[1]] == canonical[list(canonical)[2]]
    finally:
        project.close()


def test_semantic_commit_errors_without_embedder(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: None,
    )
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon"])
    try:
        result = executor_actions.run_action_spec(
            project, _commit(sid, method="semantic"), project_id=PROJECT_ID
        )
        assert result.status == "failed"
        assert result.errors[0].code == "embedding_backend_unavailable"
        # nothing written
        assert "org_canonical" not in [c["name"] for c in project.columns(sid)]
    finally:
        project.close()


def _fingerprint_key(surface: str) -> str:
    from frisket.ops.cluster_fingerprint import fingerprint

    return fingerprint(surface)


def test_canonical_override_is_applied_to_column(tmp_path):
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon", "Jon Smith"])
    try:
        key = _fingerprint_key("Jon Smith")
        result = executor_actions.run_action_spec(
            project,
            _commit(sid, review={"canonical_overrides": {key: "Jonathan Smith"}}),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        canonical = _canonical(project, sid)
        assert set(canonical.values()) == {"Jonathan Smith"}
    finally:
        project.close()


def test_excluded_member_keeps_its_own_value(tmp_path):
    project, sid = _seed(
        tmp_path, ["Jon Smith", "Smith, Jon", "Jon Smith", "jon  smith"]
    )
    try:
        key = _fingerprint_key("Jon Smith")
        result = executor_actions.run_action_spec(
            project,
            _commit(sid, review={"excluded_members": {key: ["Smith, Jon"]}}),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        canonical = _canonical(project, sid)
        rows = sorted(canonical)
        # row 1 ("Smith, Jon") was excluded -> keeps its own value
        assert canonical[rows[1]] == "Smith, Jon"
        # the other Jon Smith variants still share the canonical
        assert canonical[rows[0]] == canonical[rows[2]]
    finally:
        project.close()


def test_commit_is_undoable(tmp_path):
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon", "Jon Smith"])
    try:
        executor_actions.run_action_spec(project, _commit(sid), project_id=PROJECT_ID)
        assert "org_canonical" in [c["name"] for c in project.columns(sid)]
        project.undo()
        assert "org_canonical" not in [c["name"] for c in project.columns(sid)]
    finally:
        project.close()


def test_output_column_exists_guard(tmp_path):
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon"])
    try:
        project.add_column(sid, "org_canonical", type="text")
        result = executor_actions.run_action_spec(
            project, _commit(sid), project_id=PROJECT_ID
        )
        assert result.status == "failed"
        assert result.errors[0].code == "output_column_exists"
    finally:
        project.close()


def test_contract_rejects_irrelevant_knobs():
    from frisket.actions.system import validate_root_action

    bad_threshold = _commit(1, method="fingerprint", threshold=0.9)
    result = validate_root_action(bad_threshold)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"

    bad_ngram = _commit(1, method="semantic", ngram_size=3)
    result2 = validate_root_action(bad_ngram)
    assert result2.ok is False
    assert result2.error.code == "invalid_action_request"


def test_contract_rejects_blank_canonical_override():
    # regression: a supplied-but-blank canonical must fail loudly, never be
    # silently swapped for the computed pick.
    from frisket.actions.system import validate_root_action

    bad = _commit(1, review={"canonical_overrides": {"jon smith": "   "}})
    result = validate_root_action(bad)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_semantic_commit_uses_router_and_embeds_pre_txn(tmp_path, monkeypatch):
    # regression: the project router is threaded to the shared backend resolver,
    # and embeddings are computed PRE-txn (warmed into the cache) so the in-txn
    # clustering does NO network inside BEGIN IMMEDIATE.
    stub = StubEmbedder()
    seen: dict[str, Any] = {}

    def fake_resolve(router=None, **_kw):
        seen["router"] = router
        return (stub, "fastembed/test")

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        fake_resolve,
    )
    project, sid = _seed(
        tmp_path, ["ACME Corp", "ACME Corp", "Acme Corporation", "ACME Inc."]
    )
    from frisket.ai.llm import ModelRouter

    sentinel_router = ModelRouter(cache=None, cache_mode="off", use_env_keys=False)
    try:
        result = executor_actions.run_action_spec(
            project,
            _commit(sid, method="semantic", threshold=0.9),
            project_id=PROJECT_ID,
            router=sentinel_router,
        )
        assert result.status == "completed", result.errors
        # the project's router reached the shared resolver (not router=None)
        assert seen["router"] is sentinel_router
        # each distinct surface embedded EXACTLY once (pre-txn warm); the in-txn
        # clustering re-read from cache — no re-embed => no network in the txn.
        assert sorted(stub.embedded) == ["ACME Corp", "ACME Inc.", "Acme Corporation"]
        assert len(stub.embedded) == len(set(stub.embedded))
    finally:
        project.close()


def _resolve_action(receipt_id, key="resolve@sha256:undone"):
    return {
        "action_id": "resolve.entities",
        "scope": {"kind": "project"},
        "sheet_name": "Entities",
        "output_names": {
            key: key
            for key in ("entity", "key", "variants", "mentions", "source_variants")
        },
        "params": {
            "source": {"kind": "cluster_values", "receipt_id": receipt_id},
        },
        "idempotency_key": key,
    }


def test_undone_commit_replay_is_stale(tmp_path):
    # regression: an undone commit's op is no longer applied and its canonical
    # column is hidden — an idempotency replay must fail loudly, not silently
    # replay the stored receipt as success.
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon", "Jon Smith"])
    try:
        spec = _commit(sid)  # a fixed idempotency key we can replay
        first = executor_actions.run_action_spec(project, spec, project_id=PROJECT_ID)
        assert first.status == "completed", first.errors
        project.undo()
        replay = executor_actions.run_action_spec(project, spec, project_id=PROJECT_ID)
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_resolve_entities_rejects_undone_cluster_commit(tmp_path):
    # regression: resolve.entities consuming an UNDONE cluster.values receipt must
    # fail loudly rather than read the now-hidden canonical column.
    project, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon", "Jon Smith"])
    try:
        first = executor_actions.run_action_spec(
            project, _commit(sid), project_id=PROJECT_ID
        )
        assert first.status == "completed", first.errors
        project.undo()
        resolved = executor_actions.run_action_spec(
            project, _resolve_action(first.receipt_id), project_id=PROJECT_ID
        )
        assert resolved.status == "failed"
        assert resolved.errors[0].code == "stale_replay"
    finally:
        project.close()
