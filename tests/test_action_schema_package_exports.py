"""Public action-schema package exports resolve to live family modules."""

import frisket.contracts.actions.schemas as action_schemas
from frisket.contracts.actions.schemas import *  # noqa: F403


EXPECTED_SCHEMA_MODULES = (
    "imports",
    "media",
    "temporal",
)


def test_action_schema_package_exports_only_live_modules() -> None:
    assert tuple(action_schemas.__all__) == EXPECTED_SCHEMA_MODULES
    for name in EXPECTED_SCHEMA_MODULES:
        module = globals()[name]
        assert module.__name__ == f"{action_schemas.__name__}.{name}"
