from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "tests" / "goldens" / "ftm_bundled_plugin"


# --------------------------------------------------------------------------- #
# 1. The bundled plugin ships dormant with the write/read capabilities declared.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 2. Core FTM kinds are gone; the plugin is the only FTM execution surface.
# --------------------------------------------------------------------------- #


def test_core_followthemoney_action_kinds_are_absent_from_the_core_registry() -> None:
    from frisket.actions.registry import ACTION_REGISTRY

    kinds = ACTION_REGISTRY.action_ids
    assert "import.followthemoney" not in kinds, (
        "core import.followthemoney must be deleted"
    )
    assert "export.followthemoney" not in kinds, (
        "core export.followthemoney must be deleted"
    )


# --------------------------------------------------------------------------- #
# 3. Byte-equivalence: import round-trip + export reproduce the pre-deletion
#    core bytes (golden captured BEFORE deletion; the plugin matches it).
# --------------------------------------------------------------------------- #


def test_ftm_import_export_byte_golden_is_captured_and_reproduced() -> None:
    assert GOLDEN_DIR.is_dir(), (
        "tests/goldens/ftm_bundled_plugin/ is missing — the implementer must capture "
        "the current core FTM import fixture + export bytes as a golden BEFORE deleting core"
    )
    # The captured golden must include the source fixture and the expected export bytes.
    source_fixtures = list(GOLDEN_DIR.glob("*.ftm.json")) + list(
        GOLDEN_DIR.glob("*.ijson")
    )
    export_goldens = list(GOLDEN_DIR.glob("export*.json")) + list(
        GOLDEN_DIR.glob("*.export.ftm")
    )
    assert source_fixtures, (
        "golden import fixture (real FollowTheMoney file) is missing"
    )
    assert export_goldens, "golden export bytes (pre-deletion core output) are missing"

    # The plugin round-trip helper reproduces the golden export bytes byte-for-byte
    # -- requires the real followthemoney SDK (the `entities` extra).
    pytest.importorskip(
        "followthemoney",
        reason="requires the entities extra: pip install 'frisket-data[entities]'",
    )
    assert (
        importlib.util.find_spec("frisket.features.followthemoney.migration_harness")
        is not None
    ), (
        "frisket.features.followthemoney.migration_harness (the byte-equivalence round-trip helper) is unimplemented"
    )
    import frisket.features.followthemoney.migration_harness as harness

    assert hasattr(harness, "roundtrip_export_bytes"), (
        "harness.roundtrip_export_bytes(source_path) must import via WritePlan then export"
    )
    for source in source_fixtures:
        produced = harness.roundtrip_export_bytes(source)
        expected = export_goldens[0].read_bytes()
        assert produced == expected, (
            f"plugin export bytes diverge from the core golden for {source.name}"
        )
