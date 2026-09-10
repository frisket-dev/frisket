import time

import pytest
from pydantic import ValidationError

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.actions.extract import MAX_REGEX_TIMEOUT_SECONDS, RegexExtractParams
from frisket.engine.store import Project


def test_regex_extract_times_out_per_row_without_pinning_worker(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    try:
        sheet = p.add_sheet("data")
        cols = {"text": p.add_column(sheet, "text")}
        p.add_rows(sheet, [{"text": ("a" * 5000) + "!"}], cols)

        started = time.perf_counter()
        result = run_typed_map_request(
            p,
            typed_map_request(
                "map.regex_extract",
                sheet,
                params={
                    "input_columns": ["text"],
                    "pattern": r"(a+)+$",
                    "timeout_seconds": 0.001,
                },
                output_names={"extracted": "match"},
                idempotency_key="regex-timeout@1",
            ),
            project_id="regex-timeout",
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0
        assert result.status == "failed"
        row = p.db.execute(
            "SELECT error FROM results WHERE error IS NOT NULL"
        ).fetchone()
        assert row is not None
        assert "regex timed out" in row["error"]
    finally:
        p.close()


def test_regex_timeout_setting_is_hard_capped():
    with pytest.raises(ValidationError, match="less than or equal to 2"):
        RegexExtractParams(
            input_columns=["text"],
            pattern="x",
            timeout_seconds=MAX_REGEX_TIMEOUT_SECONDS + 0.1,
        )
