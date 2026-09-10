"""Shared accept/reject fixture parity for the strict-JSON twins.

``strict_json_loads`` (this module) and ``web/src/actions/strictJson.ts``'s
``parseStrictJson`` are two independent implementations of the same rule set
(reject duplicate object keys — comparing decoded key strings, not source
spelling — and reject non-finite numbers), with no shared runtime code and,
until now, no shared test pinning them together. This test and
``web/tests/unit/strictJson.test.ts`` both run
``tests/fixtures/strict_json_accept_reject.json`` through their respective
implementation so a future rule change that only lands on one side fails
here or in the web suite instead of drifting silently.

Does not touch test_api_call_request.py (owned elsewhere) — new file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.ops.api_call_request import strict_json_loads

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests/fixtures/strict_json_accept_reject.json"


def _load_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())["cases"]


CASES = _load_cases()


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_strict_json_loads_matches_fixture(case: dict) -> None:
    if case["expect"] == "accept":
        strict_json_loads(case["input"])  # must not raise
    else:
        assert case["expect"] == "reject"
        with pytest.raises((ValueError, json.JSONDecodeError)):
            strict_json_loads(case["input"])
