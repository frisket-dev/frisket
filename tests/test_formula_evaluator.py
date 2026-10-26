from datetime import date, datetime, timezone

import pytest

pytest.importorskip("formualizer")

from frisket.authoring.formula import CompiledFormula, FormulaValidationError


def test_typed_inputs_and_horizontal_functions():
    rows = [{"a": 3, "first name": "  Ada  ", "b": 2}]
    assert CompiledFormula("ROUND(SUM({{a}},{{b}})/2,1)").evaluate(rows)[0].value == 2.5
    assert (
        CompiledFormula('TRIM({{first name}})&" {{a}}"').evaluate(rows)[0].value
        == "Ada {{a}}"
    )
    assert CompiledFormula("{{a}} > {{b}}").evaluate(rows)[0].value is True


def test_tokens_quotes_repeated_and_cell_like_names():
    compiled = CompiledFormula(
        '=IF({{ A1 }},{{first name}} & """{{literal}}""",{{first name}})'
    )
    assert compiled.columns == ("A1", "first name")
    assert (
        compiled.evaluate([{"A1": True, "first name": '=HYPERLINK("bad")'}])[0].value
        == '=HYPERLINK("bad")"{{literal}}"'
    )


@pytest.mark.parametrize(
    "value",
    [None, False, 0, "0012", " ", date(2024, 1, 2), datetime(2024, 1, 2, 3, 4, 5)],
)
def test_scalar_bindings(value):
    result = CompiledFormula("{{value}}").evaluate([{"value": value}])[0]
    assert result.error is None
    # Excel evaluates a direct reference to an empty cell as numeric zero.
    assert result.value == (0 if value is None else value)
    if isinstance(value, bool):
        assert result.value is value


def test_error_values_and_unknown_functions():
    assert "Div" in CompiledFormula("1/{{n}}").evaluate([{"n": 0}])[0].error
    assert (
        "unsupported function"
        in CompiledFormula("NOTAFUNCTION(1)").evaluate([{}])[0].error
    )
    result = CompiledFormula('IFERROR(1/{{n}},"")').evaluate([{"n": 0}])[0]
    assert result.value == "" and result.error is None


@pytest.mark.parametrize(
    "text",
    [
        "{{a}}1",
        "{{a}}{{b}}",
        "A1",
        "SUM(A1:B2)",
        "Other!A1",
        "NamedThing",
        'INDIRECT("A1")',
        "OFFSET({{a}},0,0)",
        "ROW()",
        "COLUMN()",
        "{{}}",
        "{{unclosed",
        "{{a{b}}",
        '"unclosed',
        "1+",
        "{1,2}",
        "9007199254740993",
        "1" * 8193,
    ],
)
def test_invalid_formulas(text):
    with pytest.raises(FormulaValidationError):
        CompiledFormula(text)


def test_array_results_are_not_truncated_to_first_value():
    assert "scalar" in CompiledFormula("{{x}}").evaluate([{"x": [1, 2]}])[0].error


def test_missing_columns_and_precision_errors():
    compiled = CompiledFormula("{{x}}")
    assert "Unknown" in compiled.evaluate([{}])[0].error
    assert "precision" in compiled.evaluate([{"x": 2**53 + 1}])[0].error
    assert compiled.evaluate([{"x": 2**53}])[0].value == 2**53


def test_bounded_reuse_and_later_mixed_types():
    formula = CompiledFormula('IF({{x}}>1000,"late",{{x}}*2)')
    output = formula.evaluate([{"x": n} for n in range(2005)])
    assert len(output) == 2005
    assert output[1000].value == 2000
    assert output[1001].value == "late"
    assert formula._prepared_rows == 1000
    assert formula.evaluate([{"x": 2}])[0].value == 4


def test_run_clock_reused_and_override():
    formula = CompiledFormula("NOW()")
    first = formula.evaluate([{}, {}])
    assert first[0].value == first[1].value == formula.evaluate([{}])[0].value
    instant = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    assert formula.evaluate([{}], clock=instant)[0].value == instant.replace(
        tzinfo=None
    )
    assert (
        CompiledFormula("TODAY()").evaluate([{}], clock=instant)[0].value
        == instant.date()
    )


def test_rand_is_available_and_each_row_evaluates():
    formula = CompiledFormula("RAND()")
    results = formula.evaluate([{} for _ in range(8)])
    values = [item.value for item in results]
    assert all(item.error is None for item in results)
    assert all(0 <= value < 1 for value in values)
    assert len(set(values)) > 1
    assert formula.evaluate([{}])[0].value not in values


@pytest.mark.parametrize(
    "text",
    [
        "SUM(SEQUENCE(3000,3000)*SEQUENCE(3000,3000))",
        "INDEX(RANDARRAY(1,1),1,1)",
        "MAKEARRAY(2,2,1)",
        "MUNIT(3000)",
        "EXPAND(1,3000,3000)",
        'CELL("row",{{a}})',
        'CELL("address",{{a}})',
    ],
)
def test_unsafe_array_constructors_and_scratch_metadata_refused(text):
    with pytest.raises(FormulaValidationError):
        CompiledFormula(text)


def test_text_output_limit_preserves_source_text_for_scalar_operations():
    source = "x" * 32768
    formula = CompiledFormula("{{x}}")
    assert "32767" in formula.evaluate([{"x": source}])[0].error
    assert formula._workbook.get_value("Formula", 1, formula._output_col) is None
    assert formula.evaluate([{"x": "small"}])[0].value == "small"
    assert CompiledFormula("LEN({{x}})").evaluate([{"x": source}])[0].value == 32768
    assert (
        CompiledFormula("{{x}}").evaluate([{"x": source[:-1]}])[0].value == source[:-1]
    )


def test_timezone_inputs_normalized_to_utc():
    from datetime import timedelta

    value = datetime(2024, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=5)))
    result = CompiledFormula("{{x}}").evaluate([{"x": value}])[0]
    assert result.value == datetime(2023, 12, 31, 19, 30)
    assert CompiledFormula("YEAR({{x}})").evaluate([{"x": value}])[0].value == 2023


def test_native_failure_is_isolated_to_one_row():
    formula = CompiledFormula("{{x}}*2")
    native = formula._workbook

    class FailingWorkbook:
        def __getattr__(self, name):
            return getattr(native, name)

        def evaluate_cell(self, sheet, row, col):
            if row == 2:
                raise RuntimeError("evaluation deadline")
            return native.evaluate_cell(sheet, row, col)

    formula._workbook = FailingWorkbook()
    result = formula.evaluate([{"x": 1}, {"x": 2}, {"x": 3}])
    assert result[0].value == 2
    assert "deadline" in result[1].error
    assert result[2].value == 6


def test_aggregate_budget_stops_repeated_deadlines(monkeypatch):
    import frisket.authoring.formula as module

    formula = CompiledFormula("{{x}}*2")
    ticks = iter([0.0, 0.0, 6.0, 6.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    result = formula.evaluate([{"x": 1}, {"x": 2}, {"x": 3}])
    assert result[0].value == 2
    assert all("budget exhausted" in item.error for item in result[1:])


@pytest.mark.parametrize(
    "text",
    [
        "{{a}}:{{c}}",
        "SUM({{a}}:{{c}})",
        "SUM(({{a}}),({{c}})) + SUM(({{a}}):({{c}}))",
        "({{a}},{{c}})",
        "(({{a}})) (({{c}}))",
    ],
)
def test_reference_range_union_and_intersection_are_refused(text):
    with pytest.raises(FormulaValidationError):
        CompiledFormula(text)


def test_nested_broadcast_range_probe_refused_without_evaluation():
    expression = "({{a}}:{{c}})"
    for _ in range(5):
        expression = f"(TOCOL({expression})+TOROW({expression}))"
    with pytest.raises(FormulaValidationError, match="Ranges"):
        CompiledFormula(f"COUNT({expression})")


def test_colons_in_literals_and_column_names_are_supported():
    result = CompiledFormula('LEN("12:30")+SUM({{time:zone}},{{c}})').evaluate(
        [{"time:zone": 2, "c": 3}]
    )[0]
    assert result.value == 10


def test_budget_resets_for_each_scratch_batch(monkeypatch):
    import frisket.authoring.formula as module

    monkeypatch.setattr(module, "MAX_BATCH_ROWS", 2)
    ticks = iter([0.0, 0.0, 6.0, 7.0, 7.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    result = CompiledFormula("{{x}}*2").evaluate([{"x": 1}, {"x": 2}, {"x": 3}])
    assert result[0].value == 2
    assert "batch evaluation budget exhausted" in result[1].error
    assert result[2].value == 6
