"""Pins the cluster canonical-selection heuristic: the suggested canonical for
a cluster is the MOST FREQUENT member form, ties broken by the SHORTEST form
(then lexicographically for total determinism).

Live QA bug this guards: an 18-form "President of <country>" cluster — every
surface count 1 — used to suggest the single longest member
("President of the Republic of Trinidad and Tobago") as the canonical for all
18, because the old tie-break took the LONGEST form. Shortest-on-tie surfaces
the shared stem instead.
"""

from __future__ import annotations

from collections import Counter

from frisket.ops.cluster_fingerprint import canonical


def test_most_frequent_wins_outright():
    counts = Counter({"President": 5, "President of Honduras": 1, "El Presidente": 2})
    assert canonical(counts) == "President"


def test_more_frequent_long_form_beats_shorter_rare_form():
    # frequency dominates length: the long form is picked because it is common.
    counts = Counter({"World Health Organization": 9, "WHO": 1})
    assert canonical(counts) == "World Health Organization"


def test_tie_breaks_on_shortest():
    # all count 1 -> shortest surface wins, not the most specific/longest one.
    counts = Counter(
        {
            "President of Honduras": 1,
            "President of the Republic of Trinidad and Tobago": 1,
            "President of France": 1,
            "President": 1,
        }
    )
    assert canonical(counts) == "President"


def test_president_of_x_cluster_never_suggests_the_longest_member():
    # the exact shape of the live bug: many distinct long "President of X"
    # forms, each seen once. The canonical must NOT be the longest member.
    forms = [
        "President of Honduras",
        "President of the Republic of Trinidad and Tobago",
        "President of the United States of America",
        "President of the Bolivarian Republic of Venezuela",
    ]
    counts = Counter(dict.fromkeys(forms, 1))
    picked = canonical(counts)
    assert picked == "President of Honduras"  # shortest of the tied forms
    assert picked != max(forms, key=len)


def test_equal_frequency_equal_length_is_lexicographic():
    # frequency + length tie -> deterministic lexicographic fallback.
    counts = Counter({"bbbb": 1, "aaaa": 1})
    assert canonical(counts) == "aaaa"
