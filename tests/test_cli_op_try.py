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


@pytest.mark.parametrize("typed", [False, True])
def test_try_renders_typed_ask_by_slug(capsys: pytest.CaptureFixture, typed) -> None:
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
                        **(
                            {"response_type": "integer", "include_confidence": True}
                            if typed
                            else {}
                        ),
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
    if typed:
        assert out["schema"]["properties"]["answer"]["anyOf"][0]["type"] == "integer"
        assert out["schema"]["properties"]["answer_confidence"]["maximum"] == 1
    else:
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


def test_try_refuses_model_backed_search_without_unadmitted_egress(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ddgs

    class RefuseDDGS:
        def text(self, *args, **kwargs):
            pytest.fail("op try must not search before admitting the model call")

    monkeypatch.setattr(ddgs, "DDGS", RefuseDDGS)
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
                        "model": "anthropic/claude-haiku-4-5",
                        "max_results": 1,
                    },
                }
            ),
        ]
    )
    assert code == 2
    assert "not supported by op try" in capsys.readouterr().err


def test_try_unknown_op_lists_known_kinds(capsys: pytest.CaptureFixture) -> None:
    code = op(["try", "map.nope", "--input", "{}"])
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown op" in err
    assert "map.summarize" in err
