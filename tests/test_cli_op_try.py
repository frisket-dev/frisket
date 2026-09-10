"""`frisket op try` — the Tier-1 local dev loop (C5 D6).

Runs one row of a first-party op in-process: deterministic ops execute the
real recipe; model-backed ops render the exact request instead of spending
money; params failures surface the same canonical codes the executor returns.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.cli.op import op


def _capture(capsys: Any) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_try_renders_model_calls_instead_of_spending(
    capsys: pytest.CaptureFixture,
) -> None:
    code = op(
        [
            "try",
            "map.summarize",
            "--input",
            json.dumps(
                {
                    "row": {"story": "The council voted 5-2."},
                    "params": {
                        "source": ["story"],
                        "model": "anthropic/claude-haiku-4-5",
                        "preset": "one_line",
                    },
                }
            ),
        ]
    )
    assert code == 0
    out = _capture(capsys)
    assert out["mode"] == "rendered_model_call"
    assert out["messages"][0]["role"] == "system"
    assert out["schema"]["properties"]["summary"]["type"] == "string"


def test_try_renders_typed_ask_by_slug(capsys: pytest.CaptureFixture) -> None:
    code = op(
        [
            "try",
            "ask",
            "--input",
            json.dumps(
                {
                    "row": {"story": "The council voted 5-2."},
                    "params": {
                        "source": {"text": "Vote: {{story}}"},
                        "model": "anthropic/claude-haiku-4-5",
                        "question": "What was the vote?",
                    },
                }
            ),
        ]
    )
    assert code == 0
    out = _capture(capsys)
    assert out["op"] == "map.ask"
    assert (
        out["messages"][1]["content"][0]["text"]
        == "input: Vote: The council voted 5-2."
    )
    assert out["schema"]["properties"]["answer"]["type"] == "string"


def test_try_judge_template_keeps_source_and_input_named_answer(capsys) -> None:
    code = op(
        [
            "try",
            "judge",
            "--input",
            json.dumps(
                {
                    "row": {"story": "Evidence", "input": "Answer", "extra": "Private"},
                    "params": {
                        "source": {"text": "Source: {{story}}"},
                        "judged_column": "input",
                        "model": "anthropic/claude-haiku-4-5",
                        "guidelines": "Be accurate.",
                    },
                }
            ),
        ]
    )
    assert code == 0
    rendered = json.dumps(_capture(capsys)["messages"])
    assert "Source: Evidence" in rendered
    assert "input: Answer" in rendered
    assert "Private" not in rendered


def test_try_executes_web_search_through_host_capability_without_model_routing(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ddgs

    class FakeDDGS:
        def text(self, query: str, max_results: int):
            assert (query, max_results) == ("tariffs Canada", 1)
            return [{"title": "Result", "href": "https://example.test", "body": "Body"}]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    code = op(
        [
            "try",
            "research.web_search",
            "--input",
            json.dumps(
                {
                    "row": {"country": "Canada"},
                    "params": {
                        "query": {"text": "tariffs {{country}}"},
                        "max_results": 1,
                    },
                }
            ),
        ]
    )

    assert code == 0
    out = _capture(capsys)
    assert out == {
        "op": "research.web_search",
        "mode": "executed_external_row",
        "outputs": {
            "search_results": [
                {
                    "title": "Result",
                    "url": "https://example.test",
                    "snippet": "Body",
                }
            ]
        },
    }


def test_try_unknown_op_lists_known_kinds(capsys: pytest.CaptureFixture) -> None:
    code = op(["try", "map.nope", "--input", "{}"])
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown op" in err
    assert "map.summarize" in err
