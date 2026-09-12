from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from contextlib import chdir, nullcontext
from pathlib import Path
from typing import Any

import pytest

from helpers import CliResult, run_cli


def _cli(
    *args: str,
    cwd: Path | None = None,
    input_data: str | None = None,
) -> CliResult:
    with chdir(cwd) if cwd is not None else nullcontext():
        return run_cli(*args, input_data=input_data)


def _valid_import_rows_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Rows",
        "params": {
            "columns": [
                {"name": "summary", "type": "text"},
                {"name": "source_url", "type": "url"},
            ],
            "rows": [
                {
                    "summary": "Example row",
                    "source_url": "https://example.com/item/1",
                }
            ],
            "source": {
                "kind": "inline",
                "label": "seed fixture",
                "fingerprint": "sha256:example",
            },
        },
        "idempotency_key": "import_seed_rows@sha256:example",
    }


def _entry(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [action for action in payload["actions"] if action["kind"] == kind]
    assert len(matches) == 1
    return matches[0]


def _canonical_json_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_published_action_catalog_fingerprint() -> None:
    """Protect the full published catalog while its declarations are compacted."""

    from frisket.actions.system import root_action_catalog

    payload = root_action_catalog().model_dump(mode="json")
    errors = {entry["kind"]: entry["errors"] for entry in payload["actions"]}
    assert {
        "catalog": _canonical_json_fingerprint(payload),
        "errors": _canonical_json_fingerprint(errors),
    } == {
        "catalog": "faefefdcbe3364a59ca1e3a664df32efc4c88a98cb4009a998670e76200445bc",
        "errors": "6a15a4c454e27ca52b19b763fa69d8fbed6937253c1a03ec60bd08e989b1974b",
    }


def test_model_actions_catalog_their_potential_trace_write() -> None:
    from frisket.actions.system import root_action_catalog

    payload = root_action_catalog().model_dump(mode="json")
    traced_kinds = {
        "map.ask",
        "map.summarize",
        "map.translate",
        "map.extract",
        "map.classify",
        "map.judge",
        "map.ner",
        "media.ocr",
        "reduce.group_summary",
    }
    for kind in traced_kinds:
        assert "write_trace" in _entry(payload, kind)["side_effects"]


def test_error_specs_preserve_order_and_return_fresh_models() -> None:
    from frisket.contracts.actions.catalog_helpers import error_specs

    first = error_specs(first_error="First.", second_error="Second.")
    second = error_specs(first_error="First.", second_error="Second.")

    assert [(error.code, error.message) for error in first] == [
        ("first_error", "First."),
        ("second_error", "Second."),
    ]
    assert first is not second
    assert all(left is not right for left, right in zip(first, second, strict=True))


def test_public_action_catalog_matches_contract_and_typed_registry() -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import root_action_catalog

    catalog_kinds = [entry.kind for entry in root_action_catalog().actions]
    kind_counts = Counter(catalog_kinds)
    assert all(count == 1 for count in kind_counts.values())
    assert set(catalog_kinds) == set(ACTION_REGISTRY.action_ids)


def test_screenshot_catalog_declares_bounded_viewport_controls() -> None:
    from frisket.actions.system import root_action_catalog

    entry = next(
        item
        for item in root_action_catalog().actions
        if item.kind == "web.capture_screenshot"
    )
    assert entry.ui_hints["form"] == "generated"
    properties = entry.input_schema["properties"]
    assert set(properties) == {
        "source",
        "full_page",
        "viewport_width",
        "viewport_height",
        "max_bytes",
        "timeout_ms",
    }
    for name, default in (
        ("viewport_width", 1280),
        ("viewport_height", 720),
    ):
        assert properties[name]["type"] == "integer"
        assert properties[name]["default"] == default
        assert properties[name]["minimum"] == 1
        assert properties[name]["maximum"] == 4096


def test_temporal_actions_declare_their_timeline_stale_failure() -> None:
    from frisket.actions.system import root_action_catalog

    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    for kind in ("temporal.extract_range", "derive.temporal_segments"):
        assert "timeline_stale" in {error.code for error in entries[kind].errors}


def test_temporal_actions_declare_shared_publication_failure() -> None:
    from frisket.actions.system import root_action_catalog

    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    for kind in ("temporal.extract_range", "derive.temporal_segments"):
        assert "project_write_failed" in {error.code for error in entries[kind].errors}


def test_extract_catalog_promises_every_compatible_timestamped_transcript() -> None:
    from frisket.actions.system import root_action_catalog

    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    description = entries["temporal.extract_range"].description
    assert "compatible transcripts" in description
    assert "unambiguous" not in description


def test_temporal_actions_publish_media_source_requirements() -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import root_action_catalog
    from frisket.actions.types import discover_references

    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    for kind in ("temporal.extract_range", "derive.temporal_segments"):
        requirements = {
            item["param"]: item
            for item in entries[kind].ui_hints["source_requirements"]
        }
        assert set(requirements) == {"source", "selection"}
        source = requirements["source"]
        assert source["mode"] == "column"
        assert source["min"] == 1
        assert set(source["accepted_column_types"]) == {"audio", "video"}
        selection = requirements["selection"]
        assert selection["min"] == 0
        assert selection["accepted_column_types"] == [
            "timeline_point",
            "timeline_points",
            "timeline_range",
            "timeline_ranges",
        ]
        params_model = ACTION_REGISTRY.get(kind).definition.run.params_model
        column_params = params_model.model_validate(
            {
                "source": "media",
                "selection": {"kind": "column", "column": "reviewed"},
            }
        )
        assert {ref.column for ref in discover_references(column_params)} == {
            "media",
            "reviewed",
        }
        literal_params = params_model.model_validate(
            {
                "source": "media",
                "selection": {"kind": "draft_range", "start_ms": 0, "end_ms": 1},
            }
        )
        assert [ref.column for ref in discover_references(literal_params)] == ["media"]


def test_typed_transcript_catalog_projects_semantic_sources_and_row_scope() -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import root_action_catalog
    from frisket.actions.types import discover_references

    entry = next(
        item
        for item in root_action_catalog().actions
        if item.kind == "derive.transcript_segments"
    )
    requirements = {
        item["param"]: item for item in entry.ui_hints["source_requirements"]
    }
    assert set(requirements) == {"source", "selection"}
    assert requirements["source"]["mode"] == "column"
    assert requirements["source"]["min"] == 1
    assert requirements["source"]["accepted_column_types"] == ["timestamped_transcript"]
    assert requirements["selection"]["min"] == 0
    assert requirements["selection"]["accepted_column_types"] == [
        "timeline_point",
        "timeline_points",
        "timeline_range",
        "timeline_ranges",
    ]
    assert entry.row_scope_policy.kind == "sheet_rows"
    assert set(entry.row_scope_policy.selectors) == {"all_rows", "exact_membership"}
    params_model = ACTION_REGISTRY.get(
        "derive.transcript_segments"
    ).definition.run.params_model
    column_params = params_model.model_validate(
        {"source": "transcript", "selection": {"kind": "column", "column": "sections"}}
    )
    assert {ref.column for ref in discover_references(column_params)} == {
        "transcript",
        "sections",
    }
    literal_params = params_model.model_validate(
        {
            "source": "transcript",
            "selection": {"kind": "draft_range", "start_ms": 0, "end_ms": 1},
        }
    )
    assert [ref.column for ref in discover_references(literal_params)] == ["transcript"]


def test_generated_output_catalog_entries_advertise_claim_conflicts() -> None:
    from frisket.actions.system import root_action_catalog

    payload = root_action_catalog().model_dump(mode="json")
    missing_busy = []
    for entry in payload["actions"]:
        error_codes = {error["code"] for error in entry["errors"]}
        if (
            "output_column_exists" in error_codes
            and "output_column_busy" not in error_codes
        ):
            missing_busy.append(entry["kind"])

    assert not missing_busy, (
        "generated-output catalog entries must advertise output_column_busy "
        f"when they can report output_column_exists: {missing_busy}"
    )

    run_backfill = _entry(payload, "run.backfill")
    run_backfill_error_codes = {error["code"] for error in run_backfill["errors"]}
    assert "output_column_busy" in run_backfill_error_codes


def test_import_rows_action_catalog_schema_and_cli_validate(tmp_path: Path) -> None:
    from frisket.actions.system import (
        RootActionValidationResult,
        root_action_catalog,
        validate_root_action,
    )
    from frisket.contracts.action import MAX_IMPORT_ROWS_COLUMNS

    payload = root_action_catalog().model_dump(mode="json")
    entry = _entry(payload, "import.rows")
    assert entry["execution_mode"] == "whole_project"
    assert entry["async_mode"] == "sync"
    assert entry["writes_project"] is True
    assert entry["required_capabilities"] == ["project:write"]
    assert entry["cost_policy"]["kind"] == "none"
    assert entry["receipt_policy"] == "writes_receipt"
    assert entry["ui_hints"]["form"] == "generated"
    assert entry["ui_hints"]["dynamic_outputs"] is True
    assert entry["row_scope_policy"] == {"kind": "project", "selectors": []}
    assert set(entry["side_effects"]) == {
        "create_sheet",
        "create_columns",
        "create_rows",
        "write_op",
        "write_receipt",
    }
    schema = entry["input_schema"]
    assert set(schema["required"]) == {"columns", "rows"}
    assert set(schema["properties"]) == {"columns", "rows", "source"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["columns"]["maxItems"] == MAX_IMPORT_ROWS_COLUMNS
    assert "maxItems" not in schema["properties"]["rows"]

    request = _valid_import_rows_action()
    direct = validate_root_action(request)
    assert direct.ok, direct.error
    assert direct.action.action_id == "import.rows"
    assert direct.params["rows"][0]["source_url"] == "https://example.com/item/1"
    spec_path = tmp_path / "import_rows.json"
    spec_path.write_text(json.dumps(request), encoding="utf-8")
    for args, stdin in [
        (("action", "validate", str(spec_path)), None),
        (("action", "validate", "-"), json.dumps(request)),
    ]:
        result = _cli(*args, input_data=stdin)
        assert result.returncode == 0, result.stderr
        validated = RootActionValidationResult.model_validate(json.loads(result.stdout))
        assert validated.ok
        assert validated.action.action_id == "import.rows"
        assert validated.params["columns"][1]["name"] == "source_url"

    invalid_requests = []
    for field in ("sheet_name", "idempotency_key"):
        invalid = _valid_import_rows_action()
        invalid.pop(field)
        invalid_requests.append(invalid)
    for key, value in [
        ("mode", "append"),
        ("rows_ref", {"kind": "file", "path": "rows.json"}),
        ("rows", [{"summary": "missing url"}]),
        ("columns", [{"name": "summary", "type": "text"}] * 2),
        ("columns", [{"name": "summary", "type": "sentient"}]),
        ("surprise", True),
    ]:
        invalid = _valid_import_rows_action()
        invalid["params"][key] = value
        invalid_requests.append(invalid)
    invalid = _valid_import_rows_action()
    invalid["schema_version"] = "frisket.actions.v1"
    invalid_requests.append(invalid)
    for invalid in invalid_requests:
        result = validate_root_action(invalid)
        assert not result.ok
        assert result.error.code == "invalid_action_request"

    rejected = _cli(
        "action", "validate", "-", input_data=json.dumps(invalid_requests[0])
    )
    assert rejected.returncode == 1
    assert "invalid_action_request" in rejected.stderr
    before = sorted(p.name for p in tmp_path.iterdir())
    empty = _cli("action", "validate", "-", cwd=tmp_path, input_data="")
    assert empty.returncode == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_cli_schema_validate_and_run_use_the_typed_root_registry(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import RootActionValidationResult
    from frisket.contracts.action import ActionResult
    from frisket.engine.store import Project

    project_path = tmp_path / "typed-cli.frisket"
    project = Project.create(project_path, name="Typed CLI")
    sheet_id = project.add_sheet("people")
    source_id = project.add_column(sheet_id, "name")
    project.add_rows(sheet_id, [{"name": "Ada"}], {"name": source_id})
    project.close()
    request = {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"template": {"text": "Hello {{name}}"}},
        "output_names": {"rendered": "greeting"},
        "idempotency_key": "typed-cli-template@sha256:stable",
    }
    request_path = tmp_path / "typed-template.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    schema = json.loads(_cli("action", "schema").stdout)
    assert _entry(schema, "map.template")["ui_hints"]["form"] == "generated"

    validated = _cli("action", "validate", str(request_path))
    assert validated.returncode == 0, validated.stderr
    validation = RootActionValidationResult.model_validate(json.loads(validated.stdout))
    assert validation.ok is True
    assert validation.action is not None
    assert validation.action.action_id == "map.template"
    assert validation.params == {"template": {"text": "Hello {{name}}"}}

    ran = _cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "typed-cli",
        str(request_path),
    )
    assert ran.returncode == 0, ran.stderr
    result = ActionResult.model_validate(json.loads(ran.stdout))
    assert result.status == "completed"
    assert result.action.kind == "map.template"

    project = Project(project_path)
    try:
        output = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "greeting"
        )
        assert list(project.get_values(sheet_id, output["id"]).values()) == [
            "Hello Ada"
        ]
    finally:
        project.close()


def test_generic_typed_row_catalogs_declare_temporal_ingress_errors() -> None:
    from frisket.actions.system import root_action_catalog
    from frisket.contracts.actions.catalog_helpers import (
        TEMPORAL_PERSISTENCE_ERROR_CODES,
        temporal_persistence_errors,
    )

    payload = root_action_catalog().model_dump(mode="json")
    action_errors = {
        action["kind"]: {error["code"] for error in action["errors"]}
        for action in payload["actions"]
    }
    typed_row_writers = {
        "import.rows",
        "import.runtime",
        "import.csv",
        "import.xlsx",
        "import.pdf",
        "import.files",
        "import.ndjson",
        "import.geojson",
        "import.kml",
        "import.urls",
        "derive.table_from_list",
        "cell.edit",
        "cell.edit_query",
        "column.set_type",
    }
    for action_kind in typed_row_writers:
        assert TEMPORAL_PERSISTENCE_ERROR_CODES <= action_errors[action_kind]

    first = temporal_persistence_errors()
    second = temporal_persistence_errors()
    assert first is not second
    assert all(left is not right for left, right in zip(first, second, strict=True))


def test_import_urls_catalog_has_no_deployment_url_count_ceiling() -> None:
    from frisket.actions.system import root_action_catalog

    urls = _entry(root_action_catalog().model_dump(mode="json"), "import.urls")[
        "input_schema"
    ]["properties"]["urls"]
    assert "maxItems" not in urls


def test_file_backed_import_validation_has_no_default_byte_or_file_count_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solo may accept large sources; parser semantics still apply.

    ``Path.stat`` reports a source just over each retired ceiling while the
    fixture remains tiny and valid. This exercises public workload policy
    without allocating several 50 MB files for every test run.
    """
    from openpyxl import Workbook
    from pypdf import PdfWriter

    from frisket.actions.system import validate_root_action

    csv_path = tmp_path / "records.csv"
    csv_path.write_text("name\nAda\n", encoding="utf-8")
    xlsx_path = tmp_path / "records.xlsx"
    workbook = Workbook()
    workbook.active.append(["name"])
    workbook.active.append(["Ada"])
    workbook.save(xlsx_path)
    ndjson_path = tmp_path / "records.ndjson"
    ndjson_path.write_text('{"name":"Ada"}\n', encoding="utf-8")
    geojson_path = tmp_path / "records.geojson"
    geojson_path.write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    kml_path = tmp_path / "records.kml"
    kml_path.write_text(
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>',
        encoding="utf-8",
    )
    pdf_path = tmp_path / "records.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    file_path = tmp_path / "card.bin"
    file_path.write_bytes(b"x")

    targets = {
        csv_path,
        xlsx_path,
        ndjson_path,
        geojson_path,
        kml_path,
        pdf_path,
        file_path,
    }
    original_stat = Path.stat

    def oversized_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path not in targets:
            return result
        values = list(result)
        values[6] = 50_000_001
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", oversized_stat)

    def action(kind: str, *, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_id": kind,
            "scope": {"kind": "project"},
            "sheet_name": f"{kind} source",
            "params": params,
            "idempotency_key": f"{kind}@sha256:oversized-fixture",
        }

    single_text_column = [{"name": "name", "type": "text"}]
    cases = [
        {
            "action_id": "import.csv",
            "scope": {"kind": "project"},
            "sheet_name": "CSV source",
            "params": {
                "sources": [{"path": str(csv_path)}],
                "columns": single_text_column,
            },
            "idempotency_key": "import.csv@sha256:oversized-fixture",
        },
        action(
            "import.xlsx",
            params={
                "source": {"kind": "file", "path": str(xlsx_path)},
                "columns": single_text_column,
                "header": "present",
            },
        ),
        {
            "action_id": "import.ndjson",
            "scope": {"kind": "project"},
            "sheet_name": "NDJSON source",
            "params": {
                "source": {"kind": "file", "path": str(ndjson_path)},
                "columns": single_text_column,
            },
            "idempotency_key": "import.ndjson@sha256:oversized-fixture",
        },
        action(
            "import.geojson",
            params={
                "source": {"kind": "file", "path": str(geojson_path)},
                "property_columns": [],
            },
        ),
        action(
            "import.kml",
            params={
                "source": {"kind": "file", "path": str(kml_path)},
                "property_columns": [],
            },
        ),
        action(
            "import.pdf",
            params={"source": {"kind": "file", "path": str(pdf_path)}},
        ),
        action(
            "import.files",
            params={
                # 101 is one above the retired deployment cap. They reuse a
                # tiny readable source because this is a policy test, not a
                # blob-copy throughput benchmark.
                "files": [
                    {
                        "path": str(file_path),
                        "filename": f"card-{index}.bin",
                    }
                    for index in range(101)
                ],
            },
        ),
    ]

    for candidate in cases:
        result = validate_root_action(candidate)
        assert result.ok is True, result.error
