"""Canonical params error codes (C5 D2) at the real validation boundary.

The standard validators (`contracts/actions/schemas/_validators.py`) normalize
the audited drift: ONE row-scope acceptance set under `invalid_input_ref`, ONE
code for output-name-non-blank (`invalid_params`), `duplicate_output_column`
for duplicate EMITTED output columns, and `invalid_model` for the
`provider/model` id shape including ner's llm engine. These tests drive the
owning public boundary: canonical `validate_root_action` for migrated actions,
and `validate_action_spec` for remaining legacy actions.

map.translate, reduce.group_summary and map.ner are typed now: their
output-name and model-shape refusals surface as the typed boundary's single
`invalid_action_request` (with the pydantic reason in the message), and their
retired v2 `kind:` envelopes no longer enter legacy validation at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from frisket.actions.system import validate_root_action
from frisket.actions.types import ActionRequest
from frisket.contracts.action_validation import validate_action_spec
from frisket.contracts.actions.validation_helpers import (
    VALIDATION_ERROR_CODES,
    VALIDATION_FALLBACK_CODE,
)
from frisket.contracts.actions.schemas._validators import (
    before_rule,
    cross_rule,
    rule,
    validate_optional_row_ids,
)


def _action(kind: str, params: dict[str, Any], **extra: Any) -> dict[str, Any]:
    action = {
        "schema_version": "frisket.action.v2",
        "kind": kind,
        "capabilities": ["project:write", "model:complete", "external:web_search"],
        "params": params,
        "idempotency_key": f"{kind}@sha256:test",
    }
    action.update(extra)
    return action


def _code(action: dict[str, Any]) -> str:
    result = validate_action_spec(action)
    assert result.ok is False, "expected validation failure"
    assert result.error is not None
    return result.error.code


def _source_poll_request(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": "source-poll-validation",
    }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"source_id": 1},
        {"source": None},
        {"source": True},
        {"source": 0},
        {"source": -1},
        {"source": "1"},
        {"source": []},
        {"source": {}},
        {"source": {"name": " "}},
        {"source": {"name": "Feed", "enabled": 1}},
        {"source": {"name": "Feed", "bogus": True}},
        {"source": 1, "bogus": True},
        {"source": 1, "consented_promise_set_hash": "sha256:golden"},
    ],
)
def test_source_poll_typed_params_and_selectors_are_strict(
    params: dict[str, Any],
) -> None:
    result = validate_root_action(_source_poll_request(params))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
    assert result.error.action_kind == "source.poll"


@pytest.mark.parametrize(
    "patch",
    [
        {"idempotency_key": None},
        {"capabilities": []},
        {"capabilities": ["project:write", "external:source_poll"]},
        {"confirmed": True},
        {"consented_promise_set_hash": "sha256:golden"},
        {"scope": {"kind": "sheet_rows", "sheet_id": 1}},
    ],
)
def test_source_poll_typed_envelope_refuses_legacy_and_row_controls(patch) -> None:
    # Caller-authored capabilities are forbidden envelope data, not a grant
    # or a simulation of the host's permission policy.
    request = _source_poll_request({"source": 1})
    request.update(patch)
    result = validate_root_action(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


@pytest.mark.parametrize(
    "source",
    [1, {"name": "Feed", "kind": "rss", "url": "https://example.test/feed.xml"}],
)
def test_source_poll_typed_selectors_validate_without_reading_project_or_source(
    source: Any,
) -> None:
    # Existence, registered-poller configuration and network policy are host
    # checks; root validation accepts either typed selector without performing IO.
    result = validate_root_action(_source_poll_request({"source": source}))

    assert result.ok is True
    assert result.error is None
    if isinstance(source, int):
        assert result.params == {"source": source}
    else:
        for field, value in source.items():
            assert result.params["source"][field] == value


def test_source_poll_retired_envelope_does_not_enter_legacy_validation() -> None:
    result = validate_root_action(
        _action(
            "source.poll",
            {"source_id": 1},
            capabilities=["project:write", "external:source_poll"],
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def _typed_translate_request(output_name: str) -> dict[str, Any]:
    return {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["a"],
            "model": "anthropic/claude-haiku-4-5",
            "target_language": "Spanish",
        },
        "output_names": {"translation": output_name},
        "idempotency_key": "translate@blank-output",
    }


def test_output_name_non_blank_is_rejected_at_each_action_boundary() -> None:
    assert validate_root_action(_typed_translate_request("es")).ok is True

    blank = validate_root_action(_typed_translate_request("   "))
    assert blank.ok is False
    assert blank.error is not None
    assert blank.error.code == "invalid_action_request"
    assert blank.error.action_kind == "map.translate"
    assert "output names must be non-empty" in blank.error.message

    # The retired v2 envelope (params.output_name) has no legacy validator
    # left to reach: it is refused as an unsupported kind, not re-validated.
    retired = _action(
        "map.translate",
        {
            "sheet_id": 1,
            "input_columns": ["a"],
            "model": "anthropic/claude-haiku-4-5",
            "output_name": "   ",
        },
    )
    assert _code(retired) == "unsupported_action_kind"

    with pytest.raises(ValidationError, match="output names must be non-empty"):
        ActionRequest(
            action_id="research.web_search",
            scope={"kind": "sheet_rows", "sheet_id": 1},
            params={"query": {"text": "tariffs {{country}}"}},
            output_names={"search_results": " "},
            idempotency_key="web-search@blank-output",
        )


def test_duplicate_emitted_output_columns_refuse_at_typed_binder() -> None:
    # This pins only ActionRequest._valid_output_names (actions/types.py): a
    # generic values-distinct check on the output_names mapping that fires for
    # ANY action_id. It is NOT the reduce-specific emitted-column collision the
    # legacy `duplicate_output_column` code proved -- that check lives at
    # prepare time now and is pinned end to end by
    # test_reduce_group_summary_emitted_output_collisions_refuse_at_prepare.
    request = {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "sheet_name": "Groups",
        "params": {
            "source": ["notes"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Summarize.",
        },
        "idempotency_key": "group-summary@duplicate-outputs",
    }
    assert validate_root_action(request).ok is True

    request["output_names"] = {"group": "same", "rows": "same"}
    result = validate_root_action(request)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
    assert result.error.action_kind == "reduce.group_summary"
    assert "final output names must be unique" in result.error.message

    retired = _action(
        "reduce.group_summary",
        {
            "sheet_id": 1,
            "input_columns": ["notes"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Summarize.",
            "target_sheet_name": "Groups",
            "group_column_name": "same",
            "row_count_column_name": "same",
        },
    )
    assert _code(retired) == "unsupported_action_kind"


@pytest.mark.parametrize(
    ("output_names", "root_message", "prepare_message"),
    [
        # A collision onto a DEFAULT column name (`summary` / `group`): the
        # mapping's values are distinct, but the final materialized names are
        # not. Root binding (frisket.actions.core bind_values) refuses it, and
        # the reduce prepare seam refuses it independently.
        (
            {"group": "summary"},
            "final output names must be unique",
            "group summary output names must be distinct",
        ),
        (
            {"rows": "group"},
            "final output names must be unique",
            "group summary output names must be distinct",
        ),
        (
            {"bogus": "x"},
            "unknown output names: bogus",
            "output_names contains an unknown group summary output",
        ),
    ],
)
def test_reduce_group_summary_emitted_output_collisions_refuse_at_prepare(
    tmp_path, output_names, root_message, prepare_message
) -> None:
    """The reduce-specific emitted-column fact the legacy
    `duplicate_output_column` code carried is refused twice: at ROOT binding
    (unknown names and default-column collisions, `invalid_action_request`)
    and again at prepare time (src/frisket/engine/executor/
    group_summary_action.py, the `fields` roster check). Drive the root
    refusal end to end, then bypass root binding to prove the prepare seam
    still holds on its own; neither path writes anything."""

    from frisket.actions.system import BoundTypedActionRequest, typed_action_for_request
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.group_summary_action import (
        prepare_group_summary_action,
    )
    from frisket.engine.store import Project

    def _counts(project: Project) -> dict[str, int]:
        return {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sheets", "columns", "runs", "ops", "receipts", "model_calls")
        }

    project = Project.create(tmp_path / "group-summary.frisket", name="Ledger")
    try:
        sheet_id = project.add_sheet("Ledger")
        columns = {
            "notes": project.add_column(sheet_id, "notes", type="text"),
            "beat": project.add_column(sheet_id, "beat", type="text"),
        }
        project.add_rows(
            sheet_id,
            [{"notes": "a", "beat": "x"}, {"notes": "b", "beat": "x"}],
            columns,
        )
        request = {
            "action_id": "reduce.group_summary",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "sheet_name": "Groups",
            "params": {
                "source": ["notes"],
                "group_by": "beat",
                "model": "anthropic/claude-haiku-4-5",
                "instruction": "Summarize.",
            },
            "output_names": output_names,
            "idempotency_key": "group-summary@emitted-output-collision",
        }
        # Root binding refuses every case before any executor seam is reached.
        validation = validate_root_action(request)
        assert validation.ok is False
        assert validation.error is not None
        assert validation.error.code == "invalid_action_request"
        assert validation.error.message == root_message
        assert validation.error.action_kind == "reduce.group_summary"
        before = _counts(project)

        result = run_action_spec(project, request, project_id="p")

        assert result.status == "failed"
        assert result.run_id is None
        assert [
            (error.code, error.message, error.action_kind) for error in result.errors
        ] == [("invalid_action_request", root_message, "reduce.group_summary")]
        assert _counts(project) == before

        # Prepare seam on its own: bind the request with clean output names so
        # root admits it, then hand prepare a bound request carrying the
        # colliding names. The reduce roster check must refuse independently.
        clean = typed_action_for_request({**request, "output_names": {}})
        bound = BoundTypedActionRequest(
            clean.action,
            ActionRequest.model_validate(request),
            clean.params,
            clean.output_fields,
        )
        with pytest.raises(ValueError, match=prepare_message):
            prepare_group_summary_action(project, bound)
        assert _counts(project) == before
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Groups'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


@pytest.mark.parametrize("duplicate", ["projection", "rename"])
def test_typed_list_table_duplicate_output_columns_refuse_at_public_binder(duplicate):
    request = {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Items",
        "idempotency_key": "list-table-duplicate-columns",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": 1,
                "column_id": 1,
                "run_id": 1,
                "route": "items",
                "schema": "items.v1",
            },
            "item_schema": {"type": "object"},
            "columns": [
                {"name": "a", "type": "text", "path": "$.a"},
                {"name": "b", "type": "text", "path": "$.b"},
            ],
        },
    }
    assert validate_root_action(request).ok
    if duplicate == "projection":
        request["params"]["columns"][1]["name"] = "a"
        message = "duplicate_column_name"
    else:
        request["output_names"] = {"a": "same", "b": "same"}
        message = "final output names must be unique"
    result = validate_root_action(request)
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert message in result.error.message


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (None, "the LLM engine requires a model"),
        ("", "value must be non-empty and trimmed"),
        ("claude-haiku", "model must use provider/model form"),
    ],
)
def test_ner_llm_model_shape_refuses_at_typed_binder(model, reason) -> None:
    params: dict[str, Any] = {
        "source": ["text"],
        "labels": ["person"],
        "engine": "llm",
    }
    if model is not None:
        params["model"] = model
    result = validate_root_action(
        {
            "action_id": "map.ner",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": params,
            "output_names": {"entities": "entities"},
            "idempotency_key": "ner@llm-model-shape",
        }
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
    assert result.error.action_kind == "map.ner"
    assert reason in result.error.message

    retired_params = {**params, "input_columns": ["text"]}
    retired_params.pop("source")
    retired = _action(
        "map.ner",
        retired_params,
        row_scope={"sheet_id": 1, "selector": {"kind": "all_rows"}},
    )
    assert _code(retired) == "unsupported_action_kind"


def test_validate_optional_row_ids_contract() -> None:
    assert validate_optional_row_ids(None) is None
    assert validate_optional_row_ids([3, 1]) == [3, 1]
    for bad in ([], [0], [True], [1, 1]):
        with pytest.raises(ValueError, match="invalid_input_ref"):
            validate_optional_row_ids(bad)


def test_rule_helpers_recode_with_required_code() -> None:
    from typing import Annotated

    from pydantic import BaseModel, ValidationError, model_validator

    def non_blank(value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("blank")
        return stripped

    def coerce_int(value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("bool")
        return value

    def _conflict(model: Any) -> None:
        if model.a == str(model.b):
            raise ValueError("same")

    check = cross_rule("fields_conflict", _conflict)

    class Model(BaseModel):
        a: Annotated[str, rule("invalid_demo_field", non_blank)]
        b: Annotated[int, before_rule("invalid_demo_int", coerce_int)] = 1

        @model_validator(mode="after")
        def _cross(self) -> "Model":
            return check(self)

    assert Model(a="  x ", b=2).a == "x"
    with pytest.raises(ValidationError, match="invalid_demo_field"):
        Model(a="   ", b=2)
    with pytest.raises(ValidationError, match="invalid_demo_int"):
        Model(a="x", b=True)
    with pytest.raises(ValidationError, match="fields_conflict"):
        Model(a="1", b=1)


def _declared_wire_codes(*, include_typed: bool = False) -> dict[str, list[str]]:
    """Every wire code raised anywhere in ``frisket.contracts.actions``.

    Read from the source rather than trusted to a list: a code is a
    ``raise ValueError("<literal>")`` whose literal is a bare snake_case
    identifier (the package convention -- the string IS the wire code), or the
    explicit first argument of a ``rule``/``before_rule``/``cross_rule``
    declaration. Prose ValueErrors ("workbook has no worksheet") carry spaces
    and are not codes.
    """
    import ast
    import pathlib
    import re

    import frisket.contracts.actions as pkg

    identifier = re.compile(r"^[a-z][a-z0-9_]*$")
    rule_helpers = {"rule", "before_rule", "cross_rule"}
    found: dict[str, list[str]] = {}
    root = pathlib.Path(pkg.__file__).parent
    paths = list(root.rglob("*.py"))
    if include_typed:
        # CLI op.try also normalizes errors from typed Params; HTTP typed
        # validation instead owns the invalid_action_request boundary.
        paths.extend((root.parent.parent / "actions").rglob("*.py"))
    for path in sorted(paths):
        # rule19: two-sources — AST-enumerated raised codes vs VALIDATION_ERROR_CODES
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            code = None
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "ValueError"
                and node.exc.args
                and isinstance(node.exc.args[0], ast.Constant)
                and isinstance(node.exc.args[0].value, str)
                and identifier.match(node.exc.args[0].value)
            ):
                code = node.exc.args[0].value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_canonical_engine"
                and len(node.args) == 4
                and isinstance(node.args[3], ast.Constant)
                and isinstance(node.args[3].value, str)
            ):
                # Media's shared engine validator raises its caller-supplied
                # code, just as rule/before_rule/cross_rule do.
                code = node.args[3].value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in rule_helpers
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                code = node.args[0].value
            if code is not None:
                found.setdefault(code, []).append(
                    f"{path.relative_to(root.parent.parent)}:{node.lineno}"
                )
    return found


def test_every_raised_wire_code_is_enumerated() -> None:
    """Closure: a validator cannot raise a code the wire mapping ignores.

    ``_code_from_validation_error`` scans an ordered enumeration and falls back
    to ``invalid_params``; fifteen codes were being raised and silently
    downgraded that way. This is the fence that stops it regrowing -- it goes
    red the day a new ``raise ValueError("some_new_code")`` lands unlisted.
    """
    raised = _declared_wire_codes()
    unenumerated = sorted(
        set(raised) - set(VALIDATION_ERROR_CODES) - {VALIDATION_FALLBACK_CODE}
    )
    assert unenumerated == [], (
        "codes raised in frisket.contracts.actions but not enumerated in "
        "VALIDATION_ERROR_CODES (they downgrade to "
        f"{VALIDATION_FALLBACK_CODE!r} on the wire): "
        + ", ".join(f"{code} at {raised[code][0]}" for code in unenumerated)
    )


def test_no_enumerated_wire_code_is_dead() -> None:
    """The other direction: an enumerated code nothing raises is dead weight
    (and a match candidate that can only ever shadow a real one)."""
    raised = set(_declared_wire_codes(include_typed=True))
    assert [code for code in VALIDATION_ERROR_CODES if code not in raised] == []


@pytest.mark.parametrize(
    ("action_id", "params", "code"),
    [
        ("row.delete", {"sheet_id": 1, "row_ids": [0]}, "invalid_row_ref"),
        (
            "export.google_sheets",
            {"source": {"kind": "current_sheet"}},
            "invalid_sheet_ref",
        ),
        (
            "export.google_sheets",
            {"source": {"kind": "current_view", "sheet_id": 1}},
            "invalid_query_spec",
        ),
        (
            "export.google_sheets",
            {"destination": {"kind": "google_sheets", "mode": "update_existing"}},
            "invalid_export_destination",
        ),
    ],
)
def test_typed_params_keep_cli_codes_and_canonical_http_refusal(
    action_id, params, code, capsys
):
    import json

    from frisket.cli.op import _try_op

    if action_id == "export.google_sheets":
        params = {
            "connection_id": "chosen",
            "source": {"kind": "all_sheets"},
            "destination": {"kind": "google_sheets", "mode": "new_spreadsheet"},
            **params,
        }
    assert _try_op(action_id, json.dumps({"params": params})) == 1
    assert f"params did not validate ({code})" in capsys.readouterr().err
    result = validate_root_action(
        {
            "action_id": action_id,
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": "typed-invalid-params",
        }
    )
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_enumeration_order_cannot_shadow_a_longer_code() -> None:
    """``_code_from_validation_error`` matches by SUBSTRING, so a code that is
    contained in another must be tried after it."""
    shadowed = [
        (short, long)
        for i, short in enumerate(VALIDATION_ERROR_CODES)
        for long in VALIDATION_ERROR_CODES[i + 1 :]
        if short in long
    ]
    assert shadowed == []
