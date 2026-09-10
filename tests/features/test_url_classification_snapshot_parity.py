from __future__ import annotations

import json

from frisket.features.url_classification.contract import (
    SNAPSHOT_PATH,
    SNAPSHOT_SCHEMA_VERSION,
    serialize_snapshot,
)


def test_snapshot_file_matches_the_serializer_output() -> None:
    # If this fails, run the regen command in the contract module docstring and
    # commit the result. The check.cmd's git-diff guard enforces it is committed.
    on_disk = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert on_disk == serialize_snapshot(), (
        "snapshot drifted from the registered first-party matchers; regenerate "
        "with `uv run python -m frisket.url_classification`"
    )


def test_snapshot_is_a_faithful_projection_of_first_party_matchers() -> None:
    from frisket.features.url_classification import registered_matchers

    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert "psl_snapshot_version" in payload

    snapshot_ids = {m["matcher_id"] for m in payload["matchers"]}
    first_party_ids = {
        m.matcher_id for m in registered_matchers() if m.source == "first_party"
    }
    assert snapshot_ids == first_party_ids

    # every projected matcher carries only metadata (no executable predicate),
    # and the required routing fields are present.
    for entry in payload["matchers"]:
        assert entry["source"] == "first_party"
        assert entry["kind"] in {"media", "collection", "scraper", "page"}
        assert isinstance(entry["registered_domains"], list)
        assert "handler_action_kind" in entry
