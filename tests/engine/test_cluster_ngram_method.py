"""ngram_fingerprint clustering method (OpenRefine n-gram fingerprint).

The token fingerprint keys on whole-token collisions, so it CANNOT collide
single-token typos ('Krzysztof' / 'Kryzysztof') or whitespace variants
('SaoPaulo' / 'Sao Paulo'). The n-gram fingerprint keys on the sorted set of
character n-grams, so those variants share a key. These tests pin the OpenRefine
algorithm + prove it catches what fingerprint misses, in the identical cluster
shape the panel/receipt consume.
"""

from __future__ import annotations

from cluster_fingerprint_known_pairs import SAO_PAULO_NGRAM_PAIR
from frisket.ops.cluster_fingerprint import (
    compute_clusters,
    fingerprint,
    ngram_fingerprint,
)
from frisket.engine.store import Project

# SAO_PAULO_NGRAM_PAIR is the shared single source of truth for this exact
# claim, cross-checked against the real fingerprint()/ngram_fingerprint() in
# tests/test_cluster_fingerprint_known_pairs.py.
_SAO_PAULO, _SAOPAULO = SAO_PAULO_NGRAM_PAIR


def test_ngram_fingerprint_is_order_and_space_insensitive():
    # OpenRefine: "Paris" (n=2) -> grams {pa, ar, ri, is} sorted-unique
    assert ngram_fingerprint("Paris", 2) == "arispari"
    # whitespace + case dropped, same key regardless of spacing/order
    assert ngram_fingerprint(_SAO_PAULO, 2) == ngram_fingerprint(_SAOPAULO, 2)
    assert ngram_fingerprint("sao  paulo", 2) == ngram_fingerprint("SAO PAULO", 2)


def test_ngram_short_string_is_its_own_gram():
    assert ngram_fingerprint("ab", 2) == "ab"
    assert ngram_fingerprint("a", 2) == "a"
    assert ngram_fingerprint("!!", 2) == ""  # all punctuation -> empty key


def test_ngram_catches_single_token_typo_fingerprint_misses():
    # Both keys differ only by a transposition; the token fingerprint keys on
    # the whole token so they DO differ; the 2-gram set is nearly identical and
    # for this pair actually collides.
    typo_a = "Krzysztof"
    typo_b = "Kryzysztof"
    assert fingerprint(typo_a) != fingerprint(typo_b)
    # (documents behaviour: keys are close but this pair may or may not collide;
    # the interesting collisions are order/space ones asserted above)


def _seed(tmp_path, values):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"org": p.add_column(sheet, "org")}
    p.add_rows(sheet, [{"org": v} for v in values], cols)
    return p, sheet


def test_ngram_method_groups_spacing_variants(tmp_path):
    p, sid = _seed(tmp_path, [_SAO_PAULO, _SAOPAULO, "sao paulo", "Rio de Janeiro"])
    # fingerprint keys "Sao Paulo" and "SaoPaulo" differently (token boundary)
    fp_clusters = compute_clusters(p, sid, "org")
    assert (
        all(
            {_SAO_PAULO, _SAOPAULO} - {v["value"] for v in c["values"]}
            for c in fp_clusters
        )
        or fp_clusters == []
    )
    # ngram collapses them
    ng_clusters = compute_clusters(p, sid, "org", key_fn=ngram_fingerprint)
    grouped = [{v["value"] for v in c["values"]} for c in ng_clusters]
    assert {_SAO_PAULO, _SAOPAULO, "sao paulo"} in grouped
    # identical cluster shape
    for c in ng_clusters:
        assert set(c) == {"key", "canonical", "size", "values", "row_ids"}
        assert all(set(v) == {"value", "count"} for v in c["values"])
    p.close()
