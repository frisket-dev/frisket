from __future__ import annotations

from pathlib import Path

from plugin_clean_cutover_assertions import (
    assert_internal_shadow_manifest_field_rejected,
    assert_public_action_collision_rejected,
)


def test_internal_shadow_manifest_field_is_no_longer_part_of_plugin_contract() -> None:
    assert_internal_shadow_manifest_field_rejected()


def test_public_action_kind_shadowing_fails_closed_for_plugins(tmp_path: Path) -> None:
    assert_public_action_collision_rejected(tmp_path)
