"""the regression guard: a replay_strict CacheMiss must surface
user-facing replay copy in the result cell, not CacheMiss's developer
message (which tells real users to run pytest with FRISKET_CACHE_REFRESH).

classify_llm_error had no CacheMiss branch, so
the raw dev/test-fixture message flowed verbatim into result cells via
MapRunner._execute_row — and replay_strict is an operator-settable
FRISKET_HOSTED_CACHE_MODE value, so real end users could see it.
"""

from __future__ import annotations

from frisket.ai.llm.cache import CacheMiss
from frisket.ai.llm.remediation import MODEL_ERROR, classify_llm_error


def test_cachemiss_gets_user_facing_copy_not_dev_fixture_instructions():
    remediated = classify_llm_error(CacheMiss("abcdef0123456789deadbeef"))

    # Outcome taxonomy unchanged: still a model_error-class failure.
    assert remediated.code == MODEL_ERROR

    # The cell-facing message is user copy, not test-harness instructions.
    lowered = remediated.message.lower()
    assert "pytest" not in lowered
    assert "frisket_cache_refresh" not in lowered
    assert "fixture" not in lowered
    # It must explain the actual situation and the way out.
    assert "replay" in lowered
    assert "live" in lowered


def test_non_cachemiss_generic_errors_keep_raw_message():
    remediated = classify_llm_error(RuntimeError("boom from adapter"))
    assert remediated.code == MODEL_ERROR
    assert remediated.message == "boom from adapter"
