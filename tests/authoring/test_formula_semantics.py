import pytest
from pydantic import ValidationError

from frisket.actions.formula import FormulaParams, calculate_formula
from frisket.actions.types import (
    Formula,
    Row,
    RowError,
    Rows,
    discover_references,
    source_kind,
)


def test_formula_discovers_only_real_named_inputs_and_preserves_wire_shape():
    params = FormulaParams(
        formula=Formula(text='IF({{ a }} > 0, "{{literal}}", {{A1}})')
    )
    assert [ref.column for ref in discover_references(params)] == ["a", "A1"]
    assert params.model_dump() == {
        "formula": {"text": 'IF({{ a }} > 0, "{{literal}}", {{A1}})'}
    }
    assert source_kind(Formula) == "formula"
    assert params.formula.compiled is params.formula.compiled


def test_formula_validates_before_execution():
    with pytest.raises(ValidationError, match="reference"):
        FormulaParams(formula={"text": "A1 + 2"})


def test_formula_action_preserves_row_ids_and_error_results():
    params = FormulaParams(formula=Formula(text="1 / {{denominator}}"))
    result = calculate_formula(
        params,
        Rows({90: Row({"denominator": 2}), 17: Row({"denominator": 0})}),
    )
    assert list(result) == [90, 17]
    assert result[90].output.result == 0.5
    assert isinstance(result[17], RowError)


def test_formula_schema_has_only_formula_input():
    schema = FormulaParams.model_json_schema()
    assert set(schema["properties"]) == {"formula"}
    assert set(schema["$defs"]["Formula"]["properties"]) == {"text"}
