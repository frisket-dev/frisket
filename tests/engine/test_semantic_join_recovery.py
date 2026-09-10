"""Stale semantic joins release their outputs for a fresh user request."""

import pytest

from test_semantic_join_publication import case  # noqa: F401


@pytest.mark.parametrize("remote", [False, True])
def test_stale_abort_releases_fresh_names_for_new_key(
    case,  # noqa: F811 - shared pytest fixture
    monkeypatch,
    remote,
):
    calls = []
    mutate = True

    def embed(texts):
        nonlocal mutate
        calls.append(list(texts))
        if mutate:
            mutate = False
            case.project.apply_edits(
                [{"row_id": case.left_row, "column_id": case.donor, "value": "Changed"}]
            )
        vectors = [[1.0, 0.0] for _ in texts]
        return (
            vectors
            if not remote
            else {
                "vectors": vectors,
                "provider_id": "openai",
                "actual_model_id": "text-embedding-3-small",
                "provider_cost_usd": 0.01,
                "provider_reported_cost_usd": 0.01,
                "cost_source": "provider_reported",
                "usage": {"requests": 1},
            }
        )

    model = "openai/text-embedding-3-small" if remote else "fastembed/test"
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *a, **kw: (embed, model)
    )

    def run(**changes):
        result = case.run(**changes)
        if result.status == "needs_confirmation":
            result = case.run(
                **changes, confirmation=result.errors[0].details["promise_set_hash"]
            )
        return result

    failed = run()
    assert failed.status == "failed", failed
    assert failed.errors[0].code == "stale_input"
    assert (
        case.project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    assert (
        case.project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
        == 0
    )
    assert {column["name"] for column in case.project.columns(case.left)} == {"donor"}
    before = len(calls)
    for _ in range(2):
        replay = run()
        assert replay.status == "failed", replay
        assert replay.receipt_id == failed.receipt_id
        assert len(calls) == before

    case.project.apply_edits(
        [{"row_id": case.left_row, "column_id": case.donor, "value": "ACME"}]
    )
    recovered = run(idempotency_key="fresh-after-abort")
    assert recovered.status == "completed", recovered
    assert recovered.receipt_id != failed.receipt_id
    assert any(sheet["name"] == "Matches" for sheet in case.project.sheets())
    replay = run()
    assert replay.status == "failed", replay
    assert replay.receipt_id == failed.receipt_id
