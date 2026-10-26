"""Calculated-once expressions over named values in each row."""

from collections.abc import Mapping

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_batch
from frisket.actions.types import (
    ActionParams,
    Formula,
    InferredScalar,
    RowError,
    RowResult,
    Rows,
)


class FormulaParams(ActionParams):
    formula: Formula


class FormulaOutput(BaseModel):
    result: InferredScalar


def calculate_formula(
    params: FormulaParams, rows: Rows
) -> Mapping[int, RowResult[FormulaOutput] | RowError]:
    results = params.formula.compiled.evaluate([rows[row_id].values for row_id in rows])
    return {
        row_id: (
            RowError("formula_error", result.error)
            if result.error is not None
            else RowResult(output=FormulaOutput(result=result.value))
        )
        for row_id, result in zip(rows, results, strict=True)
    }


FORMULA = action(
    examples=(FormulaParams(formula=Formula(text="=ROUND({{amount}} * 1.2, 2)")),),
    name="formula",
    title="Formula",
    description="Calculate a new column using Excel-style formulas and named columns.",
    category=ActionCategory.TEXT,
    run=map_batch(calculate_formula, infer_output_types=True, batch_size=1000),
)
