"""``iter_pull_events`` against the live-captured Ollama ``/api/pull``
fixtures -- cassette-pinned, since the
stream format is proprietary and unversioned (design doc risk section).

Shapes (see ``tests/fixtures/ollama_pull/``):
- ``pull-stream-full.jsonl``: a real full download, 5 distinct digests.
  ``{"status": "pulling manifest"}`` -> per-digest lines (``total`` appears
  before ``completed`` does) -> ``verifying`` -> ``writing`` -> ``success``.
- ``pull-stream-already-installed.jsonl``: same shape, every digest's
  ``total``==``completed`` immediately (instant).
- ``pull-stream-unknown-model.jsonl``: errors arrive as
  ``{"error": "..."}`` lines inside an HTTP 200 stream, never as an HTTP
  error status -- the parser must treat this as the terminal signal.
"""

from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.jobs.model_pull import iter_pull_events

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ollama_pull"


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def test_full_download_aggregates_five_digests_and_ends_success() -> None:
    events = list(iter_pull_events(_lines("pull-stream-full.jsonl")))

    assert events[0].phase == "manifest"
    assert events[-1].phase == "done"

    download_events = [e for e in events if e.phase == "downloading"]
    assert download_events, "expected at least one downloading event"

    seen_digests_by_total = {
        json.loads(line)["digest"]
        for line in _lines("pull-stream-full.jsonl")
        if "digest" in json.loads(line)
    }
    assert len(seen_digests_by_total) == 5

    # totals only grow (new digests appear as the stream progresses) --
    # aggregate total_bytes across downloading events is non-decreasing, and
    # the final aggregate is the sum of every distinct digest's own total.
    totals = [e.total_bytes for e in download_events if e.total_bytes is not None]
    assert totals == sorted(totals)
    per_digest_total: dict[str, int] = {}
    for line in _lines("pull-stream-full.jsonl"):
        data = json.loads(line)
        if "digest" in data and "total" in data:
            per_digest_total[data["digest"]] = data["total"]
    assert totals[-1] == sum(per_digest_total.values())

    # completed_bytes is monotonically non-decreasing once digests are known
    completed = [
        e.completed_bytes for e in download_events if e.completed_bytes is not None
    ]
    assert completed == sorted(completed)

    # phases progress in the documented order (manifest -> downloading ->
    # verifying -> writing -> done), never backwards
    order = {"manifest": 0, "downloading": 1, "verifying": 2, "writing": 3, "done": 4}
    seen_order = [order[e.phase] for e in events]
    assert seen_order == sorted(seen_order)


def test_already_installed_completes_immediately() -> None:
    events = list(iter_pull_events(_lines("pull-stream-already-installed.jsonl")))

    assert events[0].phase == "manifest"
    assert events[-1].phase == "done"

    download_events = [e for e in events if e.phase == "downloading"]
    assert download_events
    # every digest line in this fixture already has total == completed
    for event in download_events:
        assert event.total_bytes is not None
        assert event.completed_bytes == event.total_bytes


def test_unknown_model_yields_terminal_error_event() -> None:
    events = list(iter_pull_events(_lines("pull-stream-unknown-model.jsonl")))

    assert events, "expected at least the terminal error event"
    assert events[-1].phase == "error"
    assert events[-1].error_message == "pull model manifest: file does not exist"
    # the parser stops at the terminal error line -- nothing after it, and
    # no success ever gets reported for this stream
    assert all(e.phase != "done" for e in events)


def test_tolerant_of_unparseable_and_unknown_lines() -> None:
    lines = [
        "not json at all",
        "",
        "   ",
        '{"status": "pulling manifest"}',
        '{"status": "some future status we have never seen"}',
        "[1, 2, 3]",  # valid JSON, not a dict
        '{"status": "success"}',
    ]
    events = list(iter_pull_events(lines))
    assert [e.phase for e in events] == ["manifest", "done"]
