"""Read-only cluster preview.

Pins the non-action, receipt-free preview helper: it computes duplicate-value
groups for fingerprint / ngram_fingerprint / semantic over one column, returns
a stable ``value_hash`` staleness anchor, rejects irrelevant per-method knobs
loudly, and — critically — errors (never silently falls back to fingerprint)
when semantic is requested with no embedding backend.
"""

from __future__ import annotations

import pytest

from frisket.preview.cluster import (
    ClusterPreviewError,
    cluster_preview_payload,
    compute_method_clusters,
    resolve_cluster_preview,
    source_value_hash,
)
from frisket.engine.store import Project

# reuse the hand-built stub embedder from the semantic op tests
from tests.engine.test_cluster_semantic import StubEmbedder  # type: ignore


def _seed(tmp_path, values, name="org"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {name: p.add_column(sheet, name)}
    p.add_rows(sheet, [{name: v} for v in values], cols)
    return p, sheet


ROWS = [
    "ACME Corp",
    "ACME Corp",
    "acme  corp",
    "Banana Farms",
    "Banana Farms",
    "banana farms",
]


def test_fingerprint_preview_returns_groups_and_hash(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    preview = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="fingerprint"
    )
    assert preview.method == "fingerprint"
    assert preview.count == 2
    assert preview.value_hash.startswith("sha256:")
    payload = cluster_preview_payload(preview)
    assert payload["schema_version"].startswith("frisket.cluster_preview")
    assert payload["value_hash"] == preview.value_hash
    assert payload["semantic"] is False
    # canonical shape the web + commit consume
    for cluster in payload["clusters"]:
        assert set(cluster) == {"key", "canonical", "size", "values", "row_ids"}
    p.close()


def test_value_hash_is_stable_and_changes_with_data(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    col_id = next(c["id"] for c in p.columns(sid) if c["name"] == "org")
    row_ids = [int(r) for r in p.visible_row_ids(sid)]
    h1 = source_value_hash(p, sid, col_id, row_ids)
    h2 = source_value_hash(p, sid, col_id, row_ids)
    assert h1 == h2
    # appending a row changes the visible-values hash
    p.add_rows(sid, [{"org": "Zebra Co"}], {"org": col_id})
    h3 = source_value_hash(p, sid, col_id, [int(r) for r in p.visible_row_ids(sid)])
    assert h3 != h1
    p.close()


def test_preview_reads_the_column_exactly_once(tmp_path):
    """Single-read snapshot (regression): the value-hash and the clustering both
    derive from ONE read of the column, so a concurrent edit cannot tear the
    preview into a hash of one state and cards of another. A get_values proxy
    that mutates every call after the first proves it — there is only one read
    for a mutation to land between, so the clusters always match the hash."""
    p, sid = _seed(tmp_path, ROWS)
    col_id = next(int(c["id"]) for c in p.columns(sid) if c["name"] == "org")
    original = p.get_values
    calls = {"n": 0}

    def counting(sheet_id, column_id, **kw):
        calls["n"] += 1
        values = original(sheet_id, column_id, **kw)
        if column_id == col_id and calls["n"] > 1:
            # if a second read ever happened, hand it torn data
            return {rid: "MUTATED" for rid in values}
        return values

    p.get_values = counting  # type: ignore[assignment]
    preview = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="fingerprint"
    )
    # exactly one read of the org column happened during the preview
    assert calls["n"] == 1
    # and the clusters describe the real (un-mutated) data
    assert preview.count == 2
    assert all(
        "MUTATED" not in v["value"] for c in preview.clusters for v in c["values"]
    )
    p.close()


def test_ngram_preview_uses_ngram_key(tmp_path):
    p, sid = _seed(tmp_path, ["Sao Paulo", "SaoPaulo", "sao paulo"])
    preview = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="ngram_fingerprint"
    )
    assert preview.method == "ngram_fingerprint"
    assert preview.ngram_size == 2
    assert preview.count == 1
    p.close()


def test_semantic_preview_with_injected_embedder(tmp_path):
    p, sid = _seed(tmp_path, ["ACME Corp", "Acme Corporation", "ACME Inc."])
    clusters, envelope = compute_method_clusters(
        p,
        sid,
        "org",
        method="semantic",
        min_size=2,
        embed=StubEmbedder(),
        embed_id="stub/v1",
    )
    assert envelope["semantic"] is True
    assert envelope["method"] == "semantic"
    assert len(clusters) == 1
    p.close()


def test_semantic_preview_never_resolves_a_remote_embedder(tmp_path, monkeypatch):
    """Compare/preview is the FREE surface (blind review 2026-07-26, F1).

    The old test here pinned the OPPOSITE: that the preview forwarded a router
    so "a project's configured REMOTE embedder is seen". That is what made a
    plain POST to /clusters/v1/preview bill OpenAI once per distinct column
    value with no run, no receipt and no spend line. ``resolve_embedder``'s own
    docstring names the obligation an opt-in caller takes on — "surfacing the
    spend through its cost gate BEFORE any embed call" — and a preview has no
    gate. So the preview now asks for a LOCAL backend only.
    """
    seen: dict = {}

    def fake_resolve(router=None, **kw):
        seen["router"] = router
        seen["allow_remote"] = kw.get("allow_remote")
        return (StubEmbedder(), "stub/v1")

    monkeypatch.setattr("frisket.preview.cluster.resolve_embedder", fake_resolve)
    p, sid = _seed(tmp_path, ["ACME Corp", "Acme Corporation", "ACME Inc."])
    preview = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="semantic"
    )
    # No router is handed to the resolver, and remote is not opted into — so
    # the remote branch of resolve_embedder cannot be taken at all.
    assert seen["router"] is None
    assert seen["allow_remote"] is False
    assert preview.count == 1
    p.close()


def test_semantic_preview_entry_points_take_no_router(tmp_path):
    """Structural half: the parameter is gone, so no caller can reintroduce
    the spend by passing one."""
    import inspect

    from frisket.preview import cluster as cluster_preview

    for fn in (
        cluster_preview.resolve_cluster_preview,
        cluster_preview.compute_method_clusters,
    ):
        assert "router" not in inspect.signature(fn).parameters, fn.__name__


def test_semantic_errors_when_no_embedder(tmp_path, monkeypatch):
    """NO silent fingerprint fallback — unavailable is an error."""
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    p, sid = _seed(tmp_path, ["Jon Smith", "Smith, Jon"])
    with pytest.raises(ClusterPreviewError) as exc:
        resolve_cluster_preview(p, sheet_id=sid, input_column="org", method="semantic")
    assert exc.value.code == "embedding_backend_unavailable"
    p.close()


def test_irrelevant_knobs_rejected_loudly(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    with pytest.raises(ClusterPreviewError) as exc:
        resolve_cluster_preview(
            p, sheet_id=sid, input_column="org", method="fingerprint", threshold=0.9
        )
    assert exc.value.code == "invalid_params"
    assert exc.value.field == "threshold"
    with pytest.raises(ClusterPreviewError) as exc2:
        resolve_cluster_preview(
            p, sheet_id=sid, input_column="org", method="semantic", ngram_size=3
        )
    assert exc2.value.field == "ngram_size"
    p.close()


def test_invalid_refs_and_method(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    with pytest.raises(ClusterPreviewError) as missing_col:
        resolve_cluster_preview(p, sheet_id=sid, input_column="nope")
    assert missing_col.value.code == "invalid_input_ref"
    with pytest.raises(ClusterPreviewError) as bad_method:
        resolve_cluster_preview(p, sheet_id=sid, input_column="org", method="vibes")
    assert bad_method.value.code == "invalid_params"
    with pytest.raises(ClusterPreviewError) as bad_min:
        resolve_cluster_preview(p, sheet_id=sid, input_column="org", min_size=1)
    assert bad_min.value.field == "min_size"
    p.close()


# --- cluster-by-key (derived-key clustering) -------------------------------
# The live QA case: text methods correctly see "President" and "President of
# Honduras" as different strings and never merge them. A before:" of " key
# collapses each to its stem so they cluster, while display/merge stay on the
# original forms and "Vice President" stays its own group (subset-containment
# was explicitly rejected — the key transform, not raw containment).
KEYED_ROWS = [
    "President of Honduras",
    "President of France",
    "President",
    "Vice President of Guatemala",
    "Vice President",
]


def test_cluster_by_key_merges_president_forms_display_stays_original(tmp_path):
    p, sid = _seed(tmp_path, KEYED_ROWS)
    preview = resolve_cluster_preview(
        p,
        sheet_id=sid,
        input_column="org",
        method="fingerprint",
        key_template='{{value|before:" of "}}',
    )
    by_forms = {frozenset(v["value"] for v in c["values"]): c for c in preview.clusters}
    president = frozenset({"President of Honduras", "President of France", "President"})
    vice = frozenset({"Vice President of Guatemala", "Vice President"})
    assert president in by_forms, "President stem forms did not cluster together"
    assert vice in by_forms, "Vice President stem forms did not cluster together"
    assert preview.count == 2  # the two families stay distinct groups
    # display + merge operate on the ORIGINAL forms, not the derived key.
    pres_cluster = by_forms[president]
    assert "President" not in {v["value"] for v in by_forms[vice]["values"]}
    # canonical is most-frequent -> shortest: "President" is the short stem.
    assert pres_cluster["canonical"] == "President"
    p.close()


def test_no_key_template_leaves_president_forms_unmerged(tmp_path):
    # passthrough: without a key, the token fingerprint sees five distinct
    # strings (each fingerprint carries the country/"vice" tokens) -> no group.
    p, sid = _seed(tmp_path, KEYED_ROWS)
    preview = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="fingerprint"
    )
    assert preview.count == 0
    # an empty/whitespace key_template is treated identically to no key.
    blank = resolve_cluster_preview(
        p, sheet_id=sid, input_column="org", method="fingerprint", key_template="   "
    )
    assert blank.count == 0
    p.close()


def test_cluster_by_key_unknown_transform_is_invalid_params(tmp_path):
    p, sid = _seed(tmp_path, KEYED_ROWS)
    with pytest.raises(ClusterPreviewError) as exc:
        resolve_cluster_preview(
            p,
            sheet_id=sid,
            input_column="org",
            method="fingerprint",
            key_template="{{value|reverse}}",
        )
    assert exc.value.code == "invalid_params"
    assert exc.value.field == "key_template"
    p.close()
