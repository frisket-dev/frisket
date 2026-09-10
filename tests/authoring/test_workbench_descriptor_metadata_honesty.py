from __future__ import annotations

import re
from pathlib import Path

from frisket.authoring.workbench.contracts import (
    DATA_REQUIREMENT_CONTEXT_KINDS,
    _first_missing_data_requirement,
)

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_HELPER = ROOT / "web" / "src" / "workbench" / "dataRequirements.ts"


def _frontend_context_kinds() -> set[str]:
    # rule19: two-sources: frontend context-kind union diffed against the backend Python constant
    source = FRONTEND_HELPER.read_text(encoding="utf-8")
    match = re.search(
        r"DATA_REQUIREMENT_CONTEXT_KINDS = \[(?P<body>[^\]]+)\] as const",
        source,
    )
    assert match, "frontend DATA_REQUIREMENT_CONTEXT_KINDS list not found"
    return set(re.findall(r"'([A-Za-z]+)'", match.group("body")))


def test_data_requirement_context_kind_unions_match() -> None:
    assert _frontend_context_kinds() == set(DATA_REQUIREMENT_CONTEXT_KINDS)


def test_optional_data_requirements_never_disable() -> None:
    descriptor = {
        "dataRequirements": [
            {"kind": "activeCell", "optional": True},
            {"kind": "sheetHasColumnType", "columnType": "image", "optional": True},
            {"kind": "selectedRows", "min": 2, "optional": True},
        ]
    }
    assert _first_missing_data_requirement(descriptor, {}) is None


def test_missing_context_kinds_report_missing_context() -> None:
    for kind in sorted(DATA_REQUIREMENT_CONTEXT_KINDS):
        descriptor = {"dataRequirements": [{"kind": kind}]}
        unmet = _first_missing_data_requirement(descriptor, {})
        assert unmet is not None, kind
        assert unmet["code"] == "missing_context", kind
        met = _first_missing_data_requirement(descriptor, {kind: True})
        assert met is None, kind


def test_sheet_column_type_requirement_reports_data_requirement_unmet() -> None:
    descriptor = {
        "dataRequirements": [{"kind": "sheetHasColumnType", "columnType": "image"}]
    }
    unmet = _first_missing_data_requirement(descriptor, {"columnTypes": {"c1": "text"}})
    assert unmet is not None
    assert unmet["code"] == "data_requirement_unmet"
    assert (
        _first_missing_data_requirement(descriptor, {"columnTypes": {"c1": "image"}})
        is None
    )


def test_selected_rows_requirement_honors_min() -> None:
    descriptor = {"dataRequirements": [{"kind": "selectedRows", "min": 2}]}
    unmet = _first_missing_data_requirement(descriptor, {"selectedRowIds": ["1"]})
    assert unmet is not None
    assert unmet["code"] == "missing_context"
    assert (
        _first_missing_data_requirement(descriptor, {"selectedRowIds": ["1", "2"]})
        is None
    )
