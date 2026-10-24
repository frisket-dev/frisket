import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    MapRows,
    RegisteredAction,
    action,
    map_rows,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionParams, InvocationContext, Row, RowResult
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram


ELIGIBLE = {
    "map.clean_column",
    "map.clean_dates",
    "map.columns_from_json",
    "map.regex_extract",
    "map.template",
    "map.to_geo_point",
}
BUILTIN_MAP_ROWS = tuple(
    registered
    for registered in ACTION_REGISTRY.actions
    if isinstance(registered.definition.run, MapRows)
)


def program(registered, *, runtime_binding=None):
    terminal = registered.definition.run
    return _TypedMapRowsProgram(
        registered,
        terminal.params_model.model_construct(),
        {},
        (),
        (),
        runtime_binding=runtime_binding,
    )


@pytest.mark.parametrize(
    "registered", BUILTIN_MAP_ROWS, ids=lambda item: item.action_id
)
def test_builtin_local_batch_eligibility_is_limited_to_six_data_only_handlers(
    registered,
):
    assert program(registered).batch_local_results == (registered.action_id in ELIGIBLE)
    if registered.definition.run.capabilities or registered.definition.run.injections:
        assert not program(registered).batch_local_results


def test_all_six_eligible_actions_are_covered():
    assert ELIGIBLE <= {registered.action_id for registered in BUILTIN_MAP_ROWS}


class Params(ActionParams):
    pass


class Output(BaseModel):
    value: str


def with_context(
    params: Params, row: Row, context: InvocationContext
) -> RowResult[Output]:
    return RowResult(output=Output(value="context"))


def test_context_injection_alone_is_not_retry_safe():
    registered = RegisteredAction(
        "test.context",
        action(
            name="context",
            title="Context",
            description="Context-bearing local row",
            category=ActionCategory.TEXT,
            run=map_rows(with_context),
        ),
    )
    assert not registered.definition.run.capabilities
    assert registered.definition.run.injections == (InvocationContext,)
    assert not program(registered).batch_local_results


def test_runtime_binding_does_not_inherit_builtin_retry_safety():
    registered = ACTION_REGISTRY.get("map.template")
    assert program(registered).batch_local_results
    assert not program(registered, runtime_binding=object()).batch_local_results
