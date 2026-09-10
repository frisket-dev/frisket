"""Callable publication revalidates values and pins actual replacement targets."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field, Tag, field_serializer, model_validator

from test_callable_host import ReportParams, _bound, source as source
from frisket.actions.types import SheetCsvExporter
from frisket.engine.executor.callable_action import run_typed_callable_action
from frisket.engine.store.receipts import ReceiptStore


class ConstrainedReport(BaseModel):
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def even_count(self):
        if self.count % 2:
            raise ValueError("count must be even")
        return self


@dataclass
class ConstrainedItem:
    count: Annotated[int, Field(ge=0)]


class NestedReport(BaseModel):
    reports: list[ConstrainedReport]
    item: ConstrainedItem


class SerializedReport(BaseModel):
    when: datetime = Field(
        validation_alias="timestamp", serialization_alias="published"
    )

    @field_serializer("when", when_used="json")
    def display(self, value: datetime) -> str:
        # Intentionally one-way: this string is not a valid datetime input.
        return value.strftime("Report from %Y")


class OpenReport(BaseModel):
    payload: Any


class NonfiniteSerializerReport(BaseModel):
    payload: int

    @field_serializer("payload", when_used="json")
    def nonfinite(self, value: int) -> Any:
        return {"nested": [float("nan")]}


def test_json_only_serializer_cannot_silently_null_nonfinite_output(source):
    project, params, _ = source

    def nonfinite(params: ReportParams) -> NonfiniteSerializerReport:
        return NonfiniteSerializerReport(payload=1)

    bound, _ = _bound(nonfinite, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("shape", ["any", "mapping", "model", "any_model"])
def test_nonfinite_any_values_refuse_without_silent_null_substitution(
    source, value, shape
):
    project, params, _ = source

    def nonfinite(params: ReportParams, csv: SheetCsvExporter) -> Any:
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="raw",
        )
        if shape == "any":
            return value
        nested = {"x": [1, {"y": value}]}
        return OpenReport(payload=nested) if shape in {"model", "any_model"} else nested

    nonfinite.__annotations__["return"] = {
        "any": Any,
        "mapping": dict[str, Any],
        "model": OpenReport,
        "any_model": Any,
    }[shape]
    bound, _ = _bound(nonfinite, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None and len(result.outputs) == 1
    assert run_typed_callable_action(project, "p", bound) == result


TaggedReport = (
    Annotated[ConstrainedReport, Tag("count")]
    | Annotated[SerializedReport, Tag("serialized")]
)


def test_tagged_union_revalidates_its_model_instance(source):
    project, params, _ = source

    def mutated(params: ReportParams) -> TaggedReport:
        result = ConstrainedReport(count=2)
        result.count = -1
        return result

    bound, _ = _bound(mutated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"


@pytest.mark.parametrize("count", [-1, 3])
def test_mutated_declared_model_rechecks_fields_and_model_validators(source, count):
    project, params, _ = source

    def mutated(params: ReportParams, csv: SheetCsvExporter) -> ConstrainedReport:
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="raw",
        )
        result = ConstrainedReport(count=2)
        result.count = count
        return result

    bound, _ = _bound(mutated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None and len(result.outputs) == 1
    assert (Path(params["folder"]) / "saved.csv").exists()
    assert run_typed_callable_action(project, "p", bound) == result
    # Publication policy must never mutate the author's model configuration.
    assert ConstrainedReport.model_config.get("revalidate_instances") is None


@pytest.mark.parametrize("mutate", ["model", "dataclass"])
def test_nested_instances_are_not_trusted(source, mutate):
    project, params, _ = source

    def nested(params: ReportParams) -> NestedReport:
        result = NestedReport(
            reports=[ConstrainedReport(count=2)], item=ConstrainedItem(2)
        )
        if mutate == "model":
            result.reports[0].count = -1
        else:
            result.item.count = -1
        return result

    bound, _ = _bound(nested, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_action_result"


def test_revalidation_preserves_python_values_aliases_and_one_way_serializer(source):
    project, params, _ = source

    def serialized(params: ReportParams) -> SerializedReport:
        return SerializedReport(timestamp=datetime(2026, 9, 8))

    bound, _ = _bound(serialized, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value == {"published": "Report from 2026"}
    assert set(bound.action.catalog_entry()["output_schema"]["properties"]) == {
        "published"
    }


def test_repeated_destination_parent_alias_supersedes_current_not_history(source):
    project, params, _ = source
    folder = Path(params["folder"])
    (folder / "real").mkdir()
    (folder / "alias").symlink_to(folder / "real", target_is_directory=True)

    def repeated(params: ReportParams, csv: SheetCsvExporter) -> None:
        for directory, policy in (("real", "escape"), ("alias", "raw")):
            csv.write_sheet_csv(
                sheet_id=params.source,
                path=str(Path(params.folder) / directory / "out.csv"),
                query=None,
                formula_policy=policy,
            )

    bound, _ = _bound(repeated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert len(result.outputs) == len(receipt.exports) == 1
    assert receipt.exports[0]["path"] == str(folder / "real" / "out.csv")
    artifacts = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "export_artifact"
    ]
    assert len(artifacts) == 2 and artifacts[0]["sha256"] != artifacts[1]["sha256"]
    assert artifacts[0]["path"] == artifacts[1]["path"]
    assert run_typed_callable_action(project, "p", bound) == result
    # The display/download/replay path is the same pinned delivered location.
    (folder / "alias").unlink()
    (folder / "elsewhere").mkdir()
    (folder / "alias").symlink_to(folder / "elsewhere", target_is_directory=True)
    assert run_typed_callable_action(project, "p", bound) == result


def test_leaf_symlink_is_replaced_not_conflated_with_referent(source):
    project, params, _ = source
    folder = Path(params["folder"])
    (folder / "alias.csv").symlink_to(folder / "real.csv")

    def repeated(params: ReportParams, csv: SheetCsvExporter) -> None:
        for filename, policy in (("real.csv", "escape"), ("alias.csv", "raw")):
            csv.write_sheet_csv(
                sheet_id=params.source,
                path=str(Path(params.folder) / filename),
                query=None,
                formula_policy=policy,
            )

    bound, _ = _bound(repeated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert len(result.outputs) == 2
    assert not (folder / "alias.csv").is_symlink()
    assert (folder / "real.csv").read_bytes() != (folder / "alias.csv").read_bytes()
    assert run_typed_callable_action(project, "p", bound) == result
