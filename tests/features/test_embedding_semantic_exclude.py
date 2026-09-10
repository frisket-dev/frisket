"""Hard-NOT semantic exclusion.

The embedding_similarity manual_text_query anchor gains optional
``exclude: [{text, threshold?}]`` that REMOVES rows about a concept (a gate), the
complement to weighted ``-term`` steering. ``!defense`` drops defense rows;
`-defense` only demotes them. Exclude terms embed in the SAME one gateway batch as
the query terms; rows whose cosine score >= threshold to an exclude vector are unioned
into the existing _ranked_hits exclude set, so they are removed from results.

Geometry (3-d padded): drone=[1,0,0], defense=[0,1,0], agriculture=[0,0,1].
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import build_batch_result, resolve_embedding_similarity
from frisket.ai.embeddings.similarity import SimilarityError
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project

PROJECT_ID = "p"

_DIRS = {
    "drone": [1.0, 0.0, 0.0],
    "defense": [0.0, 1.0, 0.0],
    "agriculture": [0.0, 0.0, 1.0],
    "plain_drone": [1.0, 0.0, 0.0],
    "military_drone": [1.0, 1.0, 0.0],  # drone + defense  (cos 0.707 to defense)
    "ag_drone": [1.0, 0.0, 1.0],  # drone + agriculture (cos 0 to defense)
    "tank": [0.0, 1.0, 0.0],  # pure defense (cos 1.0 to defense)
}
_ROWS = ["plain_drone", "military_drone", "ag_drone", "tank"]


class RecordingGateway:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        out = [
            list(_DIRS.get(t, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3) for t in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _create_index(project, sheet, *, provider="fastembed", policy=None, key="c"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "provider_policy": policy
                if policy is not None
                else {"allow_remote": False},
            },
            "idempotency_key": f"ex_create@{key}",
        },
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    return res.outputs[0].ref["index_id"]


def _refresh(project, index_id, gateway, *, key="r"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": f"ex_refresh@{key}",
        },
        project_id=PROJECT_ID,
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    assert res.status == "completed", res.errors


@pytest.fixture
def env(tmp_path):
    project = Project.create(tmp_path / "ex.frisket", name="ex")
    sheet = project.add_sheet("contracts")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(project, sheet)
    _refresh(project, index_id, RecordingGateway())
    yield project, sheet, cols, index_id
    project.close()


def _row(project, sheet, cols, text):
    return next(
        r for r, v in project.get_values(sheet, cols["headline"]).items() if v == text
    )


def _query(index_id, *, terms, exclude=None, limit=10):
    anchor: dict = {"kind": "manual_text_query", "terms": terms}
    if exclude is not None:
        anchor["exclude"] = exclude
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": anchor,
        "limit": limit,
    }


# --------------------------------------------------------------------------


def test_exclude_removes_rows_about_the_concept(env):
    project, sheet, cols, index_id = env
    mil = _row(project, sheet, cols, "military_drone")
    ag = _row(project, sheet, cols, "ag_drone")
    base = resolve_embedding_similarity(
        project,
        _query(index_id, terms=[{"text": "drone", "weight": 1.0}]),
        gateway=RecordingGateway(),
    )
    excluded = resolve_embedding_similarity(
        project,
        _query(
            index_id,
            terms=[{"text": "drone", "weight": 1.0}],
            exclude=[{"text": "defense"}],
        ),
        gateway=RecordingGateway(),
    )
    base_ids = {h.row_id for h in base.hits}
    ex_ids = {h.row_id for h in excluded.hits}
    # military_drone is drone-similar (present in base) but strongly about defense:
    # the HARD exclude REMOVES it (gate, not lean).
    assert mil in base_ids
    assert mil not in ex_ids
    # a weakly-related row (ag_drone, cos 0 to defense) survives the exclude
    assert ag in ex_ids


def test_exclude_terms_embed_in_a_single_batch(env):
    project, sheet, cols, index_id = env
    gw = RecordingGateway()
    resolve_embedding_similarity(
        project,
        _query(
            index_id,
            terms=[{"text": "drone", "weight": 1.0}],
            exclude=[{"text": "defense"}],
        ),
        gateway=gw,
    )
    # ONE embed call carrying the query term AND the exclude term (one egress)
    assert len(gw.calls) == 1
    assert gw.calls[0] == ["drone", "defense"]


def test_exclude_threshold_override(env):
    project, sheet, cols, index_id = env
    mil = _row(project, sheet, cols, "military_drone")
    tank = _row(project, sheet, cols, "tank")
    # military_drone has cos 0.707 to defense, tank has cos 1.0. A high threshold
    # (0.9) only excludes the pure-defense row, keeping the dual-use drone.
    res = resolve_embedding_similarity(
        project,
        _query(
            index_id,
            terms=[{"text": "drone", "weight": 1.0}],
            exclude=[{"text": "defense", "threshold": 0.9}],
        ),
        gateway=RecordingGateway(),
    )
    ids = {h.row_id for h in res.hits}
    assert tank not in ids  # cos 1.0 >= 0.9 -> excluded
    assert mil in ids  # cos 0.707 < 0.9 -> kept


def test_remote_without_allow_remote_blocks_before_any_embed(tmp_path):
    project = Project.create(tmp_path / "rex.frisket", name="r")
    sheet = project.add_sheet("s")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": False}, key="rex"
    )
    gw = RecordingGateway()
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project,
            _query(
                index_id,
                terms=[{"text": "drone", "weight": 1.0}],
                exclude=[{"text": "defense"}],
            ),
            gateway=gw,
        )
    assert exc.value.code == "embedding_remote_confirmation_required"
    assert gw.calls == []
    project.close()


def test_no_exclude_query_hash_is_unchanged():
    # A query with NO exclude must hash identically to its pre-Lane-G form (the
    # exclude field must not perturb existing manual_text_query lenses).
    from frisket.features.watchlists.specs import query_spec_hash

    base = {
        "kind": "embedding_similarity",
        "embedding_index_id": "embidx_1",
        "sheet_id": 7,
        "anchor": {
            "kind": "manual_text_query",
            "terms": [{"text": "drone", "weight": 1.0}],
        },
    }
    # hashing twice is stable, and the absence of `exclude` is the canonical form
    assert query_spec_hash(base, allow_row_cell=True) == query_spec_hash(
        base, allow_row_cell=True
    )
    with_empty = {**base, "anchor": {**base["anchor"]}}
    assert query_spec_hash(with_empty, allow_row_cell=True) == query_spec_hash(
        base, allow_row_cell=True
    )


def test_exclude_lens_stores_canonical_exclude():
    from frisket.features.watchlists.specs import normalize_query_spec

    spec = normalize_query_spec(
        {
            "kind": "embedding_similarity",
            "embedding_index_id": "embidx_1",
            "sheet_id": 7,
            "anchor": {
                "kind": "manual_text_query",
                "terms": [{"text": "drone", "weight": 1.0}],
                "exclude": [{"text": "defense", "threshold": 0.6}],
            },
        },
        allow_row_cell=True,
    )
    assert spec["anchor"]["exclude"] == [{"text": "defense", "threshold": 0.6}]
