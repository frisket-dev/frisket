"""Python is the shared schema and registration authority for installed Actions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from frisket.actions.core import RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, discover_references
from frisket.contracts.plugin import PluginManifest
from frisket.plugins.manifest_generate import (
    generate_manifest_text,
    load_generation_source,
)
from frisket.plugins.sdk import Plugin


SOURCE = """
from pydantic import BaseModel, field_validator
from frisket.actions.core import action, map_rows, ActionCategory
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult, RowError
from frisket.plugins.sdk import Plugin
class Params(ActionParams):
    renamed: ColumnRef[str]
    suffix: str = "!"
    @field_validator("suffix")
    @classmethod
    def normalized(cls, value):
        if value == "forbidden":
            raise ValueError("private validation details")
        return value + "!"
class Output(BaseModel):
    text: str | None
def run(params: Params, row: Row) -> RowResult[Output]:
    text = params.renamed.read(row)
    if text == "error":
        raise RowError("bad_text", "Text is not usable.")
    if text == "malformed":
        return RowResult(output={"text": 123})
    return RowResult(output=Output(text=None if text is None else text + params.suffix))
ACTION = action(name="convert", title="Convert", description="Convert text.",
    category=ActionCategory.TEXT, run=map_rows(run))
plugin = Plugin(id="example.typed", version="1.0.0", actions=(ACTION,))
"""


@pytest.fixture
def plugin_root(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.py").write_text(SOURCE)
    return root


def test_manifest_uses_actual_action_projection_without_old_declarations(plugin_root):
    plugin = load_generation_source(plugin_root)
    payload = json.loads(generate_manifest_text(plugin_root))
    binding = payload["runtime"]["actions"][0]
    assert binding["handler_api"] == "typed_action"
    assert (
        not {
            "cost",
            "description",
            "execution",
            "inputs",
            "materialization",
            "params",
            "rate_limit",
            "scope",
            "title",
            "writes",
        }
        & binding.keys()
    )
    assert binding["catalog_entry"] == plugin.actions[0].catalog_entry()
    for field, value in (
        ("description", "Shadow description"),
        ("execution", {"mode": "runtime_plan"}),
        ("inputs", [{"name": "shadow"}]),
        ("params", [{"name": "shadow"}]),
        ("title", "Shadow title"),
        ("writes", [{"name": "shadow", "type": "text"}]),
    ):
        shadowed = json.loads(json.dumps(payload))
        shadowed["runtime"]["actions"][0][field] = value
        with pytest.raises(ValidationError):
            PluginManifest.model_validate(shadowed)


def test_native_params_keep_declared_reference_and_validator(plugin_root):
    action = load_generation_source(plugin_root).actions[0]
    request = ActionRequest(
        action_id=action.action_id,
        scope={"kind": "sheet_rows", "sheet_id": 1},
        params={"renamed": "Source text", "suffix": "value"},
        idempotency_key="native",
    )
    bound = BoundTypedActionRequest.bind(action, request)
    assert bound.params.suffix == "value!"
    assert [reference.column for reference in discover_references(bound.params)] == [
        "Source text"
    ]
    with pytest.raises(ValidationError):
        BoundTypedActionRequest.bind(
            action, request.model_copy(update={"params": {"renamed": 3}})
        )
    with pytest.raises(ValidationError):
        BoundTypedActionRequest.bind(
            action,
            request.model_copy(
                update={"params": {"renamed": "Source text", "suffix": "forbidden"}}
            ),
        )


def test_bundled_transliterate_manifest_is_the_native_projection():
    root = (
        Path(__file__).resolve().parents[2]
        / "src/frisket/authoring/bundled_plugins/frisket.transliterate"
    )
    # rule19: Compare generated manifest against the shipped install artifact.
    assert generate_manifest_text(root) == (root / "plugin.json").read_text()
    action = load_generation_source(root).actions[0]
    assert action.action_id == "frisket.transliterate.transliterate"


def test_duplicate_plugin_action_names_remain_invalid(plugin_root):
    definition = load_generation_source(plugin_root).actions[0].definition
    with pytest.raises(ValueError, match="duplicate"):
        Plugin(id="example.typed", actions=(definition, definition))


def test_registration_requires_a_validated_namespace(plugin_root):
    """The one thing registration still refuses besides duplicate names: a
    plugin cannot register into an unvalidated or reserved namespace, which is
    what keeps its Action ids from colliding with the built-in roster."""
    definition = load_generation_source(plugin_root).actions[0].definition

    assert (
        Plugin(id="example.typed", actions=(definition,)).actions[0].action_id
        == "example.typed.convert"
    )
    for bad_id in (None, "", "not a plugin id"):
        with pytest.raises(ValueError, match="validated namespaced plugin ID"):
            Plugin(id=bad_id, actions=(definition,))


def test_registration_admits_declarations_the_old_pilot_refused(plugin_root):
    """Regression against re-adding a plugin-only restriction: an Action using
    an InvocationContext injection or a hidden output field registers exactly
    as a built-in one does, because it runs on the same native host."""
    from frisket.actions.types import InvocationContext

    definition = load_generation_source(plugin_root).actions[0].definition
    for run in (
        replace(definition.run, injections=(InvocationContext,)),
        replace(
            definition.run,
            output_fields=(replace(definition.run.output_fields[0], hidden=True),),
        ),
    ):
        registered = Plugin(id="example.typed", actions=(replace(definition, run=run),))
        assert registered.actions[0].action_id == "example.typed.convert"


@pytest.mark.parametrize("callback", ["dynamic_outputs", "active_outputs"])
def test_plugin_registration_does_not_add_static_output_restrictions(
    plugin_root, callback
):
    definition = load_generation_source(plugin_root).actions[0].definition
    definition = replace(
        definition, run=replace(definition.run, **{callback: lambda params: ()})
    )
    native = RegisteredAction("example.typed.convert", definition)
    registered = Plugin(id="example.typed", actions=(definition,)).actions[0]
    assert registered.definition is native.definition


@pytest.mark.parametrize("alias_option", ["alias", "serialization_alias"])
def test_plugin_registration_preserves_native_output_aliases(plugin_root, alias_option):
    class AliasedOutput(BaseModel):
        text: str = Field(**{alias_option: "renamed"})

    definition = load_generation_source(plugin_root).actions[0].definition
    definition = replace(
        definition, run=replace(definition.run, output_model=AliasedOutput)
    )
    native = RegisteredAction("example.typed.convert", definition)
    registered = Plugin(id="example.typed", actions=(definition,)).actions[0]
    assert registered.definition is native.definition
