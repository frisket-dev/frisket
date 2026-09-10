"""Cluster option validation shared by authoring and the admitted capability."""

import pytest
from pydantic import ValidationError

from frisket.actions.cluster_types import ClusterColumn, ClusterOptions


def test_cluster_options_defaults_and_source_types():
    assert ClusterOptions().model_dump() == {
        "method": "fingerprint",
        "min_size": 2,
        "threshold": None,
        "ngram_size": None,
        "key_template": None,
        "review": None,
    }
    assert ClusterColumn("name").references()[0].accepted_column_types == (
        "text",
        "category",
        "link",
    )


@pytest.mark.parametrize(
    "options",
    [
        {"method": "unknown"},
        {"min_size": True},
        {"min_size": 1},
        {"threshold": 0.8},
        {"ngram_size": 2},
        {"method": "semantic", "threshold": 1.1},
        {"method": "ngram_fingerprint", "ngram_size": 7},
        {"key_template": "{{value | not_a_transform}}"},
        {"review": {"canonical_overrides": {"key": " "}}},
        {"review": {"canonical_overrides": {"key": "one", " key ": "two"}}},
        {"review": {"excluded_members": {"key": ["name", " name "]}}},
        {"review": {"excluded_members": {"key": [" "]}}},
        {"review": {"excluded_members": {"key": [], " key ": []}}},
        {"review": {"source_hash": "not-a-source-hash"}},
    ],
)
def test_cluster_options_refuse_invalid_or_ambiguous_values(options):
    with pytest.raises(ValidationError):
        ClusterOptions.model_validate(options)


def test_cluster_options_normalize_review_edits_without_mutating_input():
    supplied = {
        "canonical_overrides": {" key ": " New name "},
        "excluded_members": {" key ": [" Old name "]},
        "key_template": " ",
    }
    options = ClusterOptions.model_validate(
        {
            "review": {
                key: value for key, value in supplied.items() if key != "key_template"
            },
            "key_template": supplied["key_template"],
        }
    )
    assert options.review.source_hash is None
    assert options.review.canonical_overrides == {"key": "New name"}
    assert options.review.excluded_members == {"key": ["Old name"]}
    assert options.key_template is None
    assert supplied["excluded_members"] == {" key ": [" Old name "]}
