"""Publication must not silently normalize an already-built domain result again."""

from typing import Annotated
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, BeforeValidator, PrivateAttr, field_serializer
from pydantic.dataclasses import dataclass

from test_callable_host import ReportParams, _bound, source as source
from frisket.actions.types import SheetCsvExporter
from frisket.engine.executor.callable_action import run_typed_callable_action


class DoubledReport(BaseModel):
    count: Annotated[int, BeforeValidator(lambda value: value * 2)]

    def __eq__(self, other):
        return True


@dataclass
class DoubledItem:
    count: Annotated[int, BeforeValidator(lambda value: value * 2)]

    def __eq__(self, other):
        return True


def _append_count(values):
    values.append(2)
    return values


class AppendedReport(BaseModel):
    counts: Annotated[list[int], BeforeValidator(_append_count)]


class NestedReport(BaseModel):
    report: AppendedReport


class RoundedReport(DoubledReport):
    @field_serializer("count", when_used="json")
    def rounded(self, value: int) -> str:
        return "less than ten" if value < 10 else "ten or more"


class PrivateStateReport(BaseModel):
    count: int
    _lock: object = PrivateAttr(default_factory=Lock)


def test_model_revalidation_refuses_normalization_drift(source):
    project, params, _ = source
    original = DoubledReport(count=2)
    assert original.count == 4

    def report(params: ReportParams, csv: SheetCsvExporter) -> DoubledReport:
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="raw",
        )
        return original

    bound, _ = _bound(report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None
    assert original.count == 4
    assert len(result.outputs) == 1
    assert (Path(params["folder"]) / "saved.csv").exists()
    assert run_typed_callable_action(project, "p", bound) == result


def test_nested_mutating_validator_cannot_publish_changed_return(source):
    project, params, _ = source
    original = NestedReport(report=AppendedReport(counts=[1]))
    assert original.report.counts == [1, 2]

    def report(params: ReportParams) -> NestedReport:
        return original

    bound, _ = _bound(report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None


def test_private_noncopyable_state_does_not_prevent_publication(source):
    project, params, _ = source
    original = PrivateStateReport(count=1)

    def report(params: ReportParams) -> PrivateStateReport:
        return original

    bound, _ = _bound(report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value == {"count": 1}


def test_stability_compares_published_value_not_lossy_serializer_input(source):
    project, params, _ = source
    original = RoundedReport(count=2)

    def report(params: ReportParams) -> RoundedReport:
        return original

    bound, _ = _bound(report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value == {"count": "less than ten"}
    assert original.count == 4
    assert run_typed_callable_action(project, "p", bound) == result


def test_dataclass_revalidation_refuses_normalization_drift(source):
    project, params, _ = source
    original = DoubledItem(count=2)

    def report(params: ReportParams) -> DoubledItem:
        return original

    bound, _ = _bound(report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None
    assert original.count == 4
