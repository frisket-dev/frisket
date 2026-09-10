from __future__ import annotations

from plugin_clean_cutover_assertions import (
    assert_op_declaration_materialization_rejected,
)


def test_generated_map_plugins_must_declare_plugin_owned_output_schema() -> None:
    assert_op_declaration_materialization_rejected()
