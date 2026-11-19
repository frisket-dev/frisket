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


def test_council_audio_summary_replies_fit_the_rendered_summarize_schema(
    make_project,
):
    project = make_project("Council audio")
    sheet_id = project.add_sheet("audio")
    column_id = project.add_column(sheet_id, "transcript", "text")
    project.add_rows(
        sheet_id,
        [{"transcript": "The committee will come to order."}],
        {"transcript": column_id},
    )
    action = seed_demo.typed_action_request(
        "map.summarize",
        sheet_id=sheet_id,
        params={
            "model": seed_demo.MODEL,
            "source": ["transcript"],
            "preset": "one_line",
        },
        output_names={"summary": "one_line_summary"},
    )
    assert prime_map_rows_replies(project, action, seed_demo.AUDIO_SUMMARY_REPLIES) == 1


def _countries(make_project, name: str, columns: dict[str, str], rows: list[dict]):
    project = make_project(name)
    sheet_id = project.add_sheet("countries")
    ids = {
        column: project.add_column(sheet_id, column, kind)
        for column, kind in columns.items()
    }
    project.add_rows(sheet_id, rows, ids)
    return project, sheet_id


def test_tariff_summary_replies_fit_the_rendered_summarize_schema(make_project):
    project, sheet_id = _countries(
        make_project,
        "Tariff impacts",
        {"country": "text", "search_results": "json"},
        [
            {"country": country, "search_results": results}
            for country, results in seed_demo.TARIFF_SEARCH_RESULTS.items()
        ],
    )
    summarize = seed_demo.typed_action_request(
        "map.summarize",
        sheet_id=sheet_id,
        params={
            "model": seed_demo.MODEL,
            "source": ["country", "search_results"],
            "instruction": "Summarize.",
        },
        output_names={"summary": "summary"},
    )
    assert (
        prime_map_rows_replies(project, summarize, seed_demo.TARIFF_SUMMARY_REPLIES)
        == 6
    )


def test_tariff_score_replies_fit_the_rendered_score_schema(make_project):
    project, sheet_id = _countries(
        make_project,
        "Tariff impacts",
        {"country": "text", "summary": "text"},
        [{"country": country, "summary": "fixture"} for country in seed_demo.COUNTRIES],
    )
    score = seed_demo.typed_action_request(
        "map.classify",
        sheet_id=sheet_id,
        params={
            "model": seed_demo.MODEL,
            "engine": "llm",
            "source": ["country", "summary"],
            "fields": [{"name": "tariff_impact", "type": "score"}],
            "include_justification": True,
        },
    )
    assert prime_map_rows_replies(project, score, seed_demo.TARIFF_SCORE_REPLIES) == 6
