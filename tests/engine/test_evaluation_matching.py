import json
from pathlib import Path

import pytest

from frisket.engine.evaluation_matching import match_answer


CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures/evaluation-match-cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["note"])
def test_shared_matching_cases(case):
    assert (
        match_answer(
            case["rule"],
            case["expected"],
            case["answer"],
            state=case.get("state", "value"),
            hand_verdict=case.get("handVerdict"),
        )
        == case["verdict"]
    )
