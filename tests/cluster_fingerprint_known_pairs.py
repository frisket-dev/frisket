"""Single source of truth for "which strings collide under frisket's cluster
fingerprint algorithms" -- knowledge that was independently re-encoded,
unverified against each other, in four files:
`web/tests/e2e/cluster-action-drawer.spec.ts`,
`web/tests/e2e/cluster-resolve-v1-execution.spec.ts`,
`tests/test_cluster_ngram_method.py`, and `tests/test_cluster_semantic.py`.

One of the four (`cluster-action-drawer.spec.ts`) shipped a WRONG collision
claim for one arc -- it asserted `'NYC'`/`'N.Y.C.'`/`'nyc'` merge into one
cluster under the default fingerprint method, which never happens: the
token-set fingerprint keys on WHOLE tokens, so `'N.Y.C.'` (tokens `{c, n,
y}`) and `'NYC'` (tokens `{nyc}`) land on different keys. The fixture was
fixed at `2c585fa` to use case variants of "New York" instead, independently
re-verified against `frisket.ops.cluster_fingerprint.fingerprint` when this
module was written.

`tests/test_cluster_fingerprint_known_pairs.py` asserts every entry below
against the REAL `fingerprint`/`ngram_fingerprint` functions, so a future
drift in either the algorithm or a claimed pair here is caught by pytest
instead of silently re-diverging the way the NYC fixture did.
`test_cluster_semantic.py` and `test_cluster_ngram_method.py` import the
named constants below rather than re-typing the same literal strings, so
there is exactly one place a colliding/non-colliding pair is spelled out for
the Python side.

The two e2e specs can't import a Python module, so they can't be fully
unified with this one -- each carries a comment pointing back here as the
verified source instead. If a pair's collision behavior ever needs to
change, change it HERE first (and confirm the cross-check test still
passes), then propagate to the e2e spec fixtures -- not the other way
around.
"""

from __future__ import annotations

# Case variants of "New York" -- collide under the default token fingerprint
# (case-insensitive, same token set). Used by
# web/tests/e2e/cluster-action-drawer.spec.ts's CSV fixture.
NEW_YORK_CASE_VARIANTS: list[str] = ["New York", "new york", "NEW YORK"]

# "Jon Smith" reordered as "Smith, Jon" -- collides under the default token
# fingerprint (punctuation stripped, tokens sorted before comparison). Used
# by web/tests/e2e/cluster-resolve-v1-execution.spec.ts's CSV fixture and by
# tests/test_cluster_semantic.py's VECS/ROWS fixtures.
JON_SMITH_COLLISION_PAIR: list[str] = ["Jon Smith", "Smith, Jon"]

# Groups of strings that DO collide under the default (token-set)
# fingerprint -- every string within a group shares one fingerprint key.
FINGERPRINT_COLLIDING_GROUPS: list[list[str]] = [
    NEW_YORK_CASE_VARIANTS,
    JON_SMITH_COLLISION_PAIR,
]

# Pairs that must NOT collide under the default fingerprint -- the exact
# class of claim the cluster-action-drawer.spec.ts NYC fixture got wrong.
# Kept as a live regression guard against re-introducing that mistake.
FINGERPRINT_NON_COLLIDING_PAIRS: list[tuple[str, str]] = [
    ("NYC", "N.Y.C."),
]

# "Sao Paulo"/"SaoPaulo" -- collides under the n-gram fingerprint
# (tests/test_cluster_ngram_method.py) but NOT the default token fingerprint
# (spacing variant the token method structurally can't catch).
SAO_PAULO_NGRAM_PAIR: tuple[str, str] = ("Sao Paulo", "SaoPaulo")

NGRAM_COLLIDING_NOT_TOKEN_PAIRS: list[tuple[str, str]] = [
    SAO_PAULO_NGRAM_PAIR,
]
