"""saved similarity lens.

A row-anchor embedding_similarity query persisted as a LensSpec and resolved through
the query-preview spine into a current row-set WITH per-row distance/score (the numeric
fields the watch path drops). Row anchors make no provider call; a stale anchor fails
loud.
"""

from __future__ import annotations


from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from helpers import replace_test_source_cell

_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class _Gateway:
    def embed(self, texts, *, provider, model, modality):
        out = [list(_VECTORS.get(t, [0.0, 0.0, 0.0])) + [0.0] * 381 for t in texts]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def _seed(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet,
        [{"headline": t} for t in ("cat", "kitten", "airplane")],
        {"headline": col},
    )
    return pid, project, sheet, col


def _create(project, pid, sheet):
    r = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "c@1",
        },
        project_id=pid,
    )
    return r.outputs[0].ref["index_id"]


def _refresh(project, pid, index_id, key="r@1"):
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": key,
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )


def _row(project, sheet, col, text):
    return next(r for r, v in project.get_values(sheet, col).items() if v == text)


def _lens_body(index_id, sheet, row_id, name="Similar to cat"):
    return {
        "name": name,
        "query": {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "sheet_id": sheet,
            "anchor": {"kind": "row", "row_id": row_id},
            "limit": 5,
        },
    }


def _row_cell_lens_body(index_id, sheet, row_id, name="Similar to cat (cell)"):
    return {
        "name": name,
        "query": {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "sheet_id": sheet,
            # a row_cell anchor: column-scoped provenance, but the resolver reads
            # the stored WHOLE-ROW vector (no provider call), so a lens may carry it.
            "anchor": {"kind": "row_cell", "row_id": row_id, "column": "headline"},
            "limit": 5,
        },
    }


def _setup(client):
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    return pid, project, sheet, col, index_id


def test_save_row_similarity_lens_persists_and_lists(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")

    resp = client.post(
        f"/api/projects/{pid}/lenses", json=_lens_body(index_id, sheet, cat)
    )
    assert resp.status_code == 200, resp.text
    lens = resp.json()
    assert lens["spec"]["query"]["kind"] == "embedding_similarity"
    assert lens["sheet_id"] == sheet

    listed = client.get(f"/api/projects/{pid}/lenses").json()
    assert any(line["id"] == lens["id"] for line in listed)
    # provenance: an ops row records the lens
    kinds = [r["kind"] for r in project.db.execute("SELECT kind FROM ops").fetchall()]
    assert "lens" in kinds


def test_open_lens_resolves_to_rowset_with_distance_and_score(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")
    lens_id = client.post(
        f"/api/projects/{pid}/lenses", json=_lens_body(index_id, sheet, cat)
    ).json()["id"]

    kitten = _row(project, sheet, col, "kitten")
    airplane = _row(project, sheet, col, "airplane")
    resolved = client.get(f"/api/projects/{pid}/lenses/{lens_id}/resolve")
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["row_ids"]  # ranked similar rows
    assert cat not in body["row_ids"]  # the anchor excludes itself
    scores = body["scores"]
    # the numeric fields the watch path drops are preserved
    for rid in body["row_ids"]:
        entry = scores[str(rid)]
        assert "distance" in entry and "score" in entry
    # kitten (cos 0.8 to cat) ranks ahead of airplane (cos 0)
    assert body["row_ids"][0] == kitten
    assert scores[str(kitten)]["distance"] < scores[str(airplane)]["distance"]


def test_lens_row_anchor_makes_no_provider_call(tmp_path):
    # FRISKET_DISABLE_LOCAL_EMBED=1 + an empty router => no embedding backend at all.
    # A row anchor must still resolve (it reads the stored vector, never embeds).
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")
    lens_id = client.post(
        f"/api/projects/{pid}/lenses", json=_lens_body(index_id, sheet, cat)
    ).json()["id"]
    assert (
        client.get(f"/api/projects/{pid}/lenses/{lens_id}/resolve").status_code == 200
    )


def test_open_lens_with_stale_anchor_returns_typed_400(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")
    lens_id = client.post(
        f"/api/projects/{pid}/lenses", json=_lens_body(index_id, sheet, cat)
    ).json()["id"]

    # edit the anchor row's source -> its stored vector is now stale
    replace_test_source_cell(
        project,
        row_id=cat,
        column_id=col,
        value="panther",
    )
    resolved = client.get(f"/api/projects/{pid}/lenses/{lens_id}/resolve")
    assert resolved.status_code == 400
    # a stale anchor fails loud (the resolver's typed staleness code)
    assert resolved.json()["detail"]["code"] in (
        "embedding_anchor_stale",
        "embedding_source_stale",
    )


def test_lens_accepts_manual_text_query_anchor_and_stores_canonical_terms(tmp_path):
    # (CONTRACT CHANGE): a composed/text "Similar to phrase" search is
    # now SAVEABLE as a lens (it re-embeds its terms on each open, egress-gated like
    # Show-Similar). The single `text` form canonicalizes to terms:[{text,weight:1.0}]
    # so the raw string never leaks into the stored spec. (Watches still reject it —
    # see tests/test_watchlists_embedding_similarity.py::test_manual_text_query_anchor_is_rejected.)
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    body = {
        "name": "rows like 'kitty'",
        "query": {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "sheet_id": sheet,
            "anchor": {"kind": "manual_text_query", "text": "kitty"},
        },
    }
    resp = client.post(f"/api/projects/{pid}/lenses", json=body)
    assert resp.status_code == 200, resp.text
    anchor = resp.json()["spec"]["query"]["anchor"]
    assert anchor["kind"] == "manual_text_query"
    assert anchor["terms"] == [{"text": "kitty", "weight": 1.0}]
    assert "text" not in anchor  # canonicalized to terms, no raw string left


def test_lens_accepts_composed_weighted_terms(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    body = {
        "name": "drone -defense",
        "query": {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "sheet_id": sheet,
            "anchor": {
                "kind": "manual_text_query",
                "terms": [
                    {"text": "drone", "weight": 1.0},
                    {"text": "defense", "weight": -0.4},
                ],
            },
        },
    }
    resp = client.post(f"/api/projects/{pid}/lenses", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["spec"]["query"]["anchor"]["terms"] == [
        {"text": "drone", "weight": 1.0},
        {"text": "defense", "weight": -0.4},
    ]


def test_row_cell_lens_resolves_with_no_provider_call_and_ranks_rows(tmp_path):
    # a lens whose query anchor.kind is "row_cell" must be
    # ACCEPTED at save (resolver-safe) and resolve to a ranked row-set making NO
    # provider call. FRISKET_DISABLE_LOCAL_EMBED=1 + an empty router => no
    # embedding backend; a row_cell anchor reads the stored whole-row vector, so
    # it resolves anyway (a manual_text anchor would fail with no embedder).
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")

    save = client.post(
        f"/api/projects/{pid}/lenses",
        json=_row_cell_lens_body(index_id, sheet, cat),
    )
    assert save.status_code == 200, save.text
    saved_anchor = save.json()["spec"]["query"]["anchor"]
    assert saved_anchor["kind"] == "row_cell"
    assert saved_anchor["row_id"] == cat
    assert saved_anchor["column"] == "headline"  # provenance preserved
    lens_id = save.json()["id"]

    kitten = _row(project, sheet, col, "kitten")
    airplane = _row(project, sheet, col, "airplane")
    resolved = client.get(f"/api/projects/{pid}/lenses/{lens_id}/resolve")
    assert resolved.status_code == 200, resolved.text  # NO provider call needed
    body = resolved.json()
    assert body["row_ids"]
    assert cat not in body["row_ids"]  # the anchor excludes itself
    scores = body["scores"]
    for rid in body["row_ids"]:
        assert "distance" in scores[str(rid)] and "score" in scores[str(rid)]
    # kitten (cos 0.8 to cat) ranks ahead of airplane (cos 0) — same ranking the
    # whole-row anchor gives, confirming row_cell reuses the row vector.
    assert body["row_ids"][0] == kitten
    assert scores[str(kitten)]["distance"] < scores[str(airplane)]["distance"]


def test_row_cell_anchor_rejected_for_watches(tmp_path):
    # The widened anchor set is LENS-only: a watch with a row_cell anchor still
    # fails (it is not column-specific yet, so re-evaluating it as a watch would
    # be a misleading contract).
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")
    resp = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "bad watch",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "query": {
                "kind": "embedding_similarity",
                "embedding_index_id": index_id,
                "anchor": {"kind": "row_cell", "row_id": cat},
            },
        },
    )
    assert resp.status_code == 400, resp.text


def test_lens_resolve_after_append_blocks_incomplete(tmp_path):
    # A saved lens must NOT silently resolve against a partial index (coordinator
    # review): an appended, unembedded row -> typed 400 embedding_index_incomplete.
    client = _client(tmp_path)
    pid, project, sheet, col, index_id = _setup(client)
    cat = _row(project, sheet, col, "cat")
    lens_id = client.post(
        f"/api/projects/{pid}/lenses", json=_lens_body(index_id, sheet, cat)
    ).json()["id"]
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})  # unembedded
    resolved = client.get(f"/api/projects/{pid}/lenses/{lens_id}/resolve")
    assert resolved.status_code == 400
    assert resolved.json()["detail"]["code"] == "embedding_index_incomplete"


class _ManyGateway:
    """61 rows all near the cat anchor (distinct perturbations) so they all rank as
    'similar' and the lens can return more than the default-50 candidate cap."""

    def embed(self, texts, *, provider, model, modality):
        out = []
        for t in texts:
            if t == "cat":
                v = [1.0, 0.0, 0.0]
            else:
                i = int(t[3:]) if t.startswith("row") else 0
                v = [1.0, 0.0001 * (i + 1), 0.0]
            out.append(v + [0.0] * 381)
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def test_ui_saved_lens_opens_more_than_50_rows_with_explicit_limit(tmp_path):
    # A "Similar to row" lens saved by the UI must open more
    # than 50 ranked rows. The UI now saves an EXPLICIT QuerySpec limit (the web
    # MAX_LENS_VIEW_ROWS = 500); without it the embedding_similarity default (50)
    # silently caps the grid at 50 while the grid-open path claims a 500 window.
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "many"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet,
        [{"headline": "cat"}] + [{"headline": f"row{i}"} for i in range(60)],
        {"headline": col},
    )
    index_id = _create(project, pid, sheet)
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "many@1",
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_ManyGateway()),
    )
    cat = _row(project, sheet, col, "cat")

    def _save(name, query_extra):
        return client.post(
            f"/api/projects/{pid}/lenses",
            json={
                "name": name,
                "query": {
                    "kind": "embedding_similarity",
                    "embedding_index_id": index_id,
                    "sheet_id": sheet,
                    "anchor": {"kind": "row", "row_id": cat},
                    **query_extra,
                },
            },
        ).json()["id"]

    # UI-equivalent save (explicit limit = the 500 grid window) opens > 50 ranked rows
    big = _save("Similar to cat (windowed)", {"limit": 500})
    opened = client.get(f"/api/projects/{pid}/lenses/{big}/resolve?limit=500").json()
    assert opened["total"] > 50, opened["total"]
    assert len(opened["row_ids"]) > 50

    # the bare default (no QuerySpec limit) is capped at 50 — the silent cap the fix
    # avoids (and the watch path still relies on this default unchanged)
    default = _save("Similar to cat (default)", {})
    capped = client.get(
        f"/api/projects/{pid}/lenses/{default}/resolve?limit=500"
    ).json()
    assert capped["total"] == 50
