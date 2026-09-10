from __future__ import annotations

from pathlib import Path

from frisket.ai.map.find_source import (
    AddressedUnit,
    FindCandidate,
    accepted_window_candidates,
    plan_find_windows,
)


def _units(count: int) -> list[AddressedUnit]:
    return [
        AddressedUnit(
            unit_id=index,
            text=f"unit {index}",
            span_ids=(100 + index,),
        )
        for index in range(count)
    ]


def test_find_windows_cover_every_unit_once_and_expose_overlap_context() -> None:
    windows = plan_find_windows(_units(7), max_core_units=3, overlap_units=1)

    assert [window.core_unit_ids for window in windows] == [
        (0, 1, 2),
        (3, 4, 5),
        (6,),
    ]
    assert [tuple(unit.unit_id for unit in window.units) for window in windows] == [
        (0, 1, 2, 3),
        (2, 3, 4, 5, 6),
        (5, 6),
    ]
    assert sorted(
        unit_id for window in windows for unit_id in window.core_unit_ids
    ) == list(range(7))


def test_overlap_match_emits_only_from_window_owning_earliest_cited_unit() -> None:
    first, second = plan_find_windows(_units(6), max_core_units=3, overlap_units=1)
    candidate = FindCandidate(
        match="unit 2",
        source_unit_ids=(2, 3),
        details={"sentiment": "neutral"},
    )

    first_result = accepted_window_candidates(first, [candidate])
    second_result = accepted_window_candidates(second, [candidate])

    assert first_result.accepted == (candidate,)
    assert first_result.rejected == ()
    assert second_result.accepted == ()
    assert [item.reason for item in second_result.rejected] == ["not_owner"]


def test_find_rejects_uncited_unknown_and_blank_candidates() -> None:
    (window,) = plan_find_windows(_units(2), max_core_units=2, overlap_units=1)

    result = accepted_window_candidates(
        window,
        [
            FindCandidate(match="", source_unit_ids=(0,), details={}),
            FindCandidate(match="uncited", source_unit_ids=(), details={}),
            FindCandidate(match="wrong source", source_unit_ids=(999,), details={}),
        ],
    )

    assert result.accepted == ()
    assert [item.reason for item in result.rejected] == [
        "blank_match",
        "evidence_required",
        "unknown_source_unit",
    ]


def test_find_requires_one_verbatim_match_location_in_cited_units() -> None:
    window = plan_find_windows(
        [
            AddressedUnit(
                unit_id=0,
                text="China first. China second.",
                span_ids=(100,),
            )
        ],
        max_core_units=1,
        overlap_units=0,
    )[0]

    result = accepted_window_candidates(
        window,
        [
            FindCandidate(match="fabricated", source_unit_ids=(0,), details={}),
            FindCandidate(match="China", source_unit_ids=(0,), details={}),
            FindCandidate(match="China first", source_unit_ids=(0,), details={}),
        ],
    )

    assert [item.candidate.match for item in result.rejected] == [
        "fabricated",
        "China",
    ]
    assert [item.reason for item in result.rejected] == [
        "match_not_unique_in_evidence",
        "match_not_unique_in_evidence",
    ]
    assert [item.match for item in result.accepted] == ["China first"]


def test_markdown_formatted_text_is_planned_as_text_with_inline_detail_schema(
    tmp_path: Path,
) -> None:
    from frisket.actions.types import ActionRequest
    from test_map_find_runtime import _operation_for_request
    from frisket.engine.executor.map_find_planning import (
        find_response_schema,
        plan_find_scan,
    )
    from frisket.engine.executor.map_find_source import resolve_find_sources
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "find.frisket", name="Find")
    try:
        sheet_id = project.add_sheet("Sources")
        column_id = project.add_column(sheet_id, "body", type="text", format="markdown")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "China appears here.\n\nA distractor.\n\nChina appears again."}],
            {"body": column_id},
        )[0]
        params = _operation_for_request(
            ActionRequest(
                action_id="map.find",
                scope={"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
                sheet_name="Findings",
                output_names={"match": "Match"},
                idempotency_key="find-source-test",
                params={
                    "source": "body",
                    "instruction": "Find every discussion of China.",
                    "fields": [
                        {
                            "name": "sentiment",
                            "type": "category",
                            "labels": ["positive", "neutral", "negative"],
                        }
                    ],
                    "model": "anthropic/claude-haiku-4-5",
                },
            )
        )

        resolved = resolve_find_sources(project, params)
        assert isinstance(resolved, tuple)
        assert resolved[0].column_type == "text"
        assert resolved[0].source_kind == "text"
        assert [source.row_id for source in resolved] == [row_id]
        assert "".join(unit.text for unit in resolved[0].units).startswith("China")
        assert all(
            span_id < 0 for unit in resolved[0].units for span_id in unit.span_ids
        )

        plans = plan_find_scan(params, resolved)
        assert plans
        assert {unit.unit_id for plan in plans for unit in plan.window.units} == {
            unit.unit_id for unit in resolved[0].units
        }
        details = find_response_schema(params)["properties"]["matches"]["items"][
            "properties"
        ]["details"]
        assert details["required"] == []
        sentiment = details["properties"]["sentiment"]["anyOf"]
        assert sentiment[0]["enum"] == [
            "positive",
            "neutral",
            "negative",
        ]
        assert sentiment[1] == {"type": "null"}
    finally:
        project.close()


def test_find_skips_empty_cells_in_the_selected_row_scope(tmp_path: Path) -> None:
    from frisket.actions.types import ActionRequest
    from test_map_find_runtime import _operation_for_request
    from frisket.engine.executor.map_find_source import resolve_find_sources
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "find-empty.frisket", name="Find")
    try:
        sheet_id = project.add_sheet("Sources")
        column_id = project.add_column(sheet_id, "body", "text")
        row_ids = project.add_rows(
            sheet_id,
            [{"body": "China appears here."}, {"body": "   "}],
            {"body": column_id},
        )
        resolved = resolve_find_sources(
            project,
            _operation_for_request(
                ActionRequest(
                    action_id="map.find",
                    scope={"kind": "sheet_rows", "sheet_id": sheet_id},
                    sheet_name="Findings",
                    output_names={"match": "Match"},
                    idempotency_key="find-source-test",
                    params={
                        "source": "body",
                        "instruction": "Find China.",
                        "model": "anthropic/claude-haiku-4-5",
                    },
                )
            ),
        )

        assert isinstance(resolved, tuple)
        assert [source.row_id for source in resolved] == [row_ids[0]]
    finally:
        project.close()
