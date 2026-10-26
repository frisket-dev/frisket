"""Current-row spreadsheet formulas, with typed bindings and bounded native evaluation."""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import formualizer

MAX_BATCH_ROWS = 1000
MAX_FORMULA_LENGTH = 8192
MAX_SAFE_INTEGER = 2**53
# These functions either expose scratch coordinates or manufacture references.
_FORBIDDEN_FUNCTIONS = {"INDIRECT", "OFFSET", "ROW", "COLUMN"}


class FormulaValidationError(ValueError):
    """A formula cannot be admitted for current-row evaluation."""


@dataclass(frozen=True)
class FormulaResult:
    value: str | float | int | bool | date | datetime | None = None
    error: str | None = None


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _parts(text: str) -> tuple[list[str | tuple[str]], tuple[str, ...]]:
    """Recognize the shared column-token syntax outside Excel string literals."""
    parts: list[str | tuple[str]] = []
    columns: list[str] = []
    literal: list[str] = []
    quoted = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            literal.append(char)
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                literal.append('"')
                i += 2
                continue
            quoted = not quoted
        elif not quoted and text.startswith("{{", i):
            end = text.find("}}", i + 2)
            if end == -1:
                raise FormulaValidationError(
                    "Unclosed column reference; use {{column name}}."
                )
            name = text[i + 2 : end].strip()
            if not name or "{" in name or "}" in name:
                raise FormulaValidationError(
                    "Column references need a nonempty name without braces."
                )
            parts.append("".join(literal))
            literal = []
            parts.append((name,))
            if name not in columns:
                columns.append(name)
            i = end + 2
            continue
        elif not quoted and char in "{}":
            raise FormulaValidationError(
                "Arrays and literal braces are not supported in Formula."
            )
        else:
            literal.append(char)
        i += 1
    if quoted:
        raise FormulaValidationError("Unclosed quoted string in Formula.")
    parts.append("".join(literal))
    for index in range(len(parts) - 2):
        if (
            isinstance(parts[index], tuple)
            and isinstance(parts[index + 2], tuple)
            and not str(parts[index + 1]).strip()
        ):
            raise FormulaValidationError(
                "Separate column references with an operator or function argument comma."
            )
    return parts, tuple(columns)


class CompiledFormula:
    """Compile once per run; reuse at most 1000 scratch rows across calls.

    Results preserve native scalar types. Missing columns and unsupported input
    values are row errors; callers additionally validate source column schemas.
    A caller can provide its run timestamp to every evaluation, or use the single
    UTC instant captured when this object is constructed.
    """

    def __init__(self, text: str):
        if not isinstance(text, str) or not text.strip():
            raise FormulaValidationError("Enter a formula.")
        if len(text) > MAX_FORMULA_LENGTH:
            raise FormulaValidationError(
                f"Formula exceeds {MAX_FORMULA_LENGTH} characters."
            )
        self.text = text
        self._clock = datetime.now(timezone.utc)
        self._parts, self.columns = _parts(text.strip().removeprefix("="))
        neutral = "=" + "".join(
            "(0)" if isinstance(p, tuple) else p for p in self._parts
        )
        # Reject large integer literals before the native parser converts them to
        # doubles. Strings are removed using Excel's doubled-quote convention.
        unquoted = re.sub(r'"(?:[^\"]|\"\")*"', "", neutral)
        nesting = 0
        for char in unquoted:
            if char == "(":
                nesting += 1
                if nesting > 64:
                    raise FormulaValidationError("Formula nesting exceeds 64 levels.")
            elif char == ")":
                nesting -= 1
        for token in re.findall(r"(?<![\w.])\d+(?:\.\d*)?(?:[eE][+-]?\d+)?", unquoted):
            number = Decimal(token)
            if number == number.to_integral_value() and abs(number) > MAX_SAFE_INTEGER:
                raise FormulaValidationError(
                    "Integer literal exceeds exact spreadsheet precision (2**53)."
                )
        self._random = False
        try:
            ast = formualizer.parse(neutral)
            pending = [(ast, 0)]
            while pending:
                node, depth = pending.pop()
                if depth > 64:
                    raise FormulaValidationError("Formula nesting exceeds 64 levels.")
                if node.node_type() == "Reference":
                    raise FormulaValidationError(
                        "Use {{column name}} references; raw cells, ranges and names are not supported."
                    )
                if node.node_type() == "Array":
                    raise FormulaValidationError(
                        "Formula must return a scalar, not an array."
                    )
                function = node.get_function_name()
                if function and function.upper().removeprefix("_XLFN.") in {
                    "RAND",
                    "RANDBETWEEN",
                }:
                    self._random = True
                if (
                    function
                    and function.upper().removeprefix("_XLFN.") in _FORBIDDEN_FUNCTIONS
                ):
                    raise FormulaValidationError(
                        f"{function} is not supported for current-row formulas."
                    )
                pending.extend((child, depth + 1) for child in node.children())
        except FormulaValidationError:
            raise
        except Exception as exc:
            raise FormulaValidationError(f"Invalid formula: {exc}") from exc
        self._output_col = len(self.columns) + 1
        self._output_letter = _column_letter(self._output_col)
        self._new_workbook()

    def _new_workbook(self) -> None:
        config = formualizer.EvaluationConfig()
        config.max_work_units = 1_000_000
        config.max_eval_time_ms = 5000
        config.range_expansion_limit = 100_000
        config.enable_parallel = False
        config.workbook_seed = secrets.randbits(64)
        self._workbook = formualizer.Workbook(
            config=formualizer.WorkbookConfig(
                mode=formualizer.WorkbookMode.Ephemeral,
                enable_changelog=False,
                eval_config=config,
            )
        )
        self._workbook.add_sheet("Formula")
        self._prepared_rows = 0

    def _source(self, row: int) -> str:
        refs = {
            name: f"({_column_letter(i + 1)}{row})"
            for i, name in enumerate(self.columns)
        }
        expression = "".join(
            refs[p[0]] if isinstance(p, tuple) else p for p in self._parts
        )
        return "=" + expression

    def evaluate(
        self, rows: Sequence[Mapping[str, Any]], *, clock: datetime | None = None
    ) -> list[FormulaResult]:
        """Evaluate in bounded batches. The caller may stream batches itself."""
        results: list[FormulaResult] = []
        for offset in range(0, len(rows), MAX_BATCH_ROWS):
            batch = rows[offset : offset + MAX_BATCH_ROWS]
            if self._random:
                # Ephemeral RAND is seeded by cell address and does not advance
                # on recalculation. Fresh bounded batches need independent seeds.
                self._new_workbook()
            self._workbook.set_deterministic_clock(clock or self._clock, "utc")
            values = []
            errors: dict[int, str] = {}
            for index, row in enumerate(batch):
                bound = []
                for column in self.columns:
                    if column not in row:
                        errors[index] = f"Unknown formula column: {column}"
                        value = None
                    else:
                        value = row[column]
                        if (
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and abs(value) > MAX_SAFE_INTEGER
                        ):
                            errors[index] = (
                                f"Column {column}: integer exceeds exact spreadsheet precision (2**53)."
                            )
                            value = None
                        elif not isinstance(
                            value, (str, float, int, bool, date, type(None))
                        ):
                            errors[index] = (
                                f"Column {column}: Formula requires a scalar number, text, boolean, date or blank."
                            )
                            value = None
                        elif isinstance(value, float) and not math.isfinite(value):
                            errors[index] = (
                                f"Column {column}: nonfinite numbers are not supported."
                            )
                            value = None
                    bound.append(value)
                values.append(bound)
            if self.columns:
                self._workbook.set_values_batch("Formula", 1, 1, values)
            if len(batch) > self._prepared_rows:
                start = self._prepared_rows + 1
                self._workbook.set_formulas_batch(
                    "Formula",
                    start,
                    self._output_col,
                    [[self._source(row)] for row in range(start, len(batch) + 1)],
                )
                self._prepared_rows = len(batch)
            try:
                outputs = self._workbook.evaluate_cells(
                    [
                        ("Formula", row, self._output_col)
                        for row in range(1, len(batch) + 1)
                    ]
                )
            except Exception as exc:
                results.extend(
                    FormulaResult(error=f"Formula evaluation failed: {exc}")
                    for _ in batch
                )
                continue
            for index, value in enumerate(outputs):
                if index in errors:
                    results.append(FormulaResult(error=errors[index]))
                elif (
                    self._workbook.inspect_cell(
                        f"Formula!{self._output_letter}{index + 1}"
                    ).cell.spill
                    is not None
                ):
                    results.append(
                        FormulaResult(
                            error="Formula must return a scalar, not an array or spill."
                        )
                    )
                elif isinstance(value, dict) and value.get("type") == "Error":
                    kind = value.get("kind", "Unknown")
                    detail = (
                        " (unknown or unsupported function)" if kind == "Name" else ""
                    )
                    results.append(
                        FormulaResult(error=f"Formula error: {kind}{detail}")
                    )
                elif not isinstance(value, (str, float, int, bool, date, type(None))):
                    results.append(
                        FormulaResult(
                            error="Formula must return a scalar, not an array or object."
                        )
                    )
                elif isinstance(value, float) and not math.isfinite(value):
                    results.append(
                        FormulaResult(error="Formula returned a nonfinite number.")
                    )
                else:
                    results.append(FormulaResult(value=value))
        return results
