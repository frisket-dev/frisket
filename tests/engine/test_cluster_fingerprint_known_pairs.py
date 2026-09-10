"""Cross-checks every claimed collision/non-collision pair in
`tests/cluster_fingerprint_known_pairs.py` against the REAL fingerprint
algorithms -- the self-verifying-assertion pattern
`tests/test_cluster_semantic.py:99` (`compute_clusters(...) == []`) already
uses for one pair, generalized here to the shared fixture. This is exactly
what would have caught the retired `cluster-action-drawer.spec.ts` NYC
fixture bug -- a WRONG claimed collision -- before it shipped, had the e2e
side been checked against this module instead of hand-verified once and left
free to drift.
"""

from __future__ import annotations

from cluster_fingerprint_known_pairs import (
    FINGERPRINT_COLLIDING_GROUPS,
    FINGERPRINT_NON_COLLIDING_PAIRS,
    NGRAM_COLLIDING_NOT_TOKEN_PAIRS,
)

from frisket.ops.cluster_fingerprint import fingerprint, ngram_fingerprint


def test_claimed_collision_groups_share_one_fingerprint_key():
    for group in FINGERPRINT_COLLIDING_GROUPS:
        keys = {fingerprint(value) for value in group}
        assert len(keys) == 1, f"{group} does not collide: keys={keys}"


def test_claimed_non_colliding_pairs_have_different_fingerprint_keys():
    for a, b in FINGERPRINT_NON_COLLIDING_PAIRS:
        assert fingerprint(a) != fingerprint(b), (
            f"{a!r} and {b!r} were claimed non-colliding but share a "
            f"fingerprint key: {fingerprint(a)!r}"
        )


def test_ngram_pairs_need_ngram_not_token_fingerprint():
    for a, b in NGRAM_COLLIDING_NOT_TOKEN_PAIRS:
        assert fingerprint(a) != fingerprint(b), (
            f"{a!r}/{b!r} were claimed to need the n-gram method, but the "
            "token fingerprint already collides them -- the pair no longer "
            "demonstrates what it's cited for"
        )
        assert ngram_fingerprint(a) == ngram_fingerprint(b), (
            f"{a!r} and {b!r} do not collide under the n-gram fingerprint"
        )
