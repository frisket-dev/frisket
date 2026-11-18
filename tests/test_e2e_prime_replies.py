"""The keyless e2e seed's fixture replies against the production classify plan."""

from __future__ import annotations

import csv
import io

import pytest
from jsonschema.exceptions import ValidationError

from frisket.ai.llm import ResponseCache
from scripts.e2e import seed_demo
from scripts.e2e.prime_replies import prime_map_rows_replies


def _stories(make_project):
    project = make_project("Local stories")
    sheet_id = project.add_sheet("stories")
    column_id = project.add_column(sheet_id, "snippet", "text")
    snippets = [
        row["snippet"] for row in csv.DictReader(io.StringIO(seed_demo.STORIES_CSV))
    ]
    project.add_rows(
        sheet_id, [{"snippet": text} for text in snippets], {"snippet": column_id}
    )
    action = seed_demo.typed_action_request(
        "map.classify", sheet_id=sheet_id, params=seed_demo.STORIES_CLASSIFY
    )
    return project, action


def _cached_keys(project) -> int:
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        return cache.count()
    finally:
        cache.close()


def test_local_stories_replies_fit_the_rendered_classify_schema(make_project):
    project, action = _stories(make_project)
    assert len(seed_demo.STORIES_REPLIES) == 8
    assert prime_map_rows_replies(project, action, seed_demo.STORIES_REPLIES) == 8
    assert _cached_keys(project) == 8


def test_a_reply_missing_a_field_is_refused_before_anything_is_stored(make_project):
    project, action = _stories(make_project)
    replies = [dict(reply) for reply in seed_demo.STORIES_REPLIES]
    del replies[0]["newsworthiness"]
    with pytest.raises(ValidationError, match="newsworthiness"):
        prime_map_rows_replies(project, action, replies)
    assert _cached_keys(project) == 0


def test_one_reply_per_scoped_row(make_project):
    project, action = _stories(make_project)
    with pytest.raises(ValueError, match="8 rows in scope, 7 replies given"):
        prime_map_rows_replies(project, action, seed_demo.STORIES_REPLIES[:7])
