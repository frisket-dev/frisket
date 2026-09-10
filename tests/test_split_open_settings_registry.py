"""Red-first behavioral contract for the derived open settings registry."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.gap
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
REGISTRY = WEB / "src" / "settings" / "settingsRegistry.ts"
OPEN = WEB / "src" / "settings" / "openSettingsRegistry.ts"
POSTURE = WEB / "src" / "editions" / "posture.ts"


def _registry_snapshot(tmp_path: Path) -> dict:
    runner = tmp_path / "settings-registry-proof.ts"
    bundle = tmp_path / "settings-registry-proof.mjs"
    runner.write_text(
        "\n".join(
            (
                f"import {{ SETTINGS_SECTIONS }} from {json.dumps(str(REGISTRY))};",
                f"import {{ settingsSectionsFor }} from {json.dumps(str(OPEN))};",
                f"import {{ LOCAL_EDITION_DESCRIPTOR, TEAM_EDITION_DESCRIPTOR }} "
                f"from {json.dumps(str(POSTURE))};",
                # Capabilities come from the canonical open descriptors rather
                # than a test-owned posture reconstruction.
                "const openEditions = [LOCAL_EDITION_DESCRIPTOR, TEAM_EDITION_DESCRIPTOR];",
                "const idsFor = (edition: typeof LOCAL_EDITION_DESCRIPTOR) =>",
                "  settingsSectionsFor(edition).map((item) => item.id);",
                "const byPosture = () => Object.fromEntries(",
                "  openEditions.map((edition) => [edition.id, idsFor(edition)]),",
                ");",
                "const before = {",
                "  registry: SETTINGS_SECTIONS.map((item) => item.id),",
                "  postures: byPosture(),",
                "};",
                "(SETTINGS_SECTIONS as unknown as Array<Record<string, unknown>>).push({",
                "  id: 'project.temporary-open', scope: 'project', section: 'temporary-open',",
                "  routePattern: '/p/{projectId}/settings/project/temporary-open',",
                "  title: 'Temporary Open', navGroup: 'Project',",
                "  searchLabels: ['temporary'], visibility: 'project', permission: 'owner',",
                "  component: 'project.general', summary: 'Mutation proof.',",
                "});",
                "const after = {",
                "  registry: SETTINGS_SECTIONS.map((item) => item.id),",
                "  postures: byPosture(),",
                "};",
                "console.log(JSON.stringify({ before, after }));",
                "",
            )
        ),
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [
            str(WEB / "node_modules" / ".bin" / "esbuild"),
            str(runner),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--outfile={bundle}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    executed = subprocess.run(
        ["node", str(bundle)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    return json.loads(executed.stdout.strip().splitlines()[-1])


def _expected_open_ids(registry_ids: list[str], posture: str) -> set[str]:
    ids = set(registry_ids)
    if posture == "local":
        ids.discard("project.access")
    else:
        # The team-only definition may remain in the central registry or move
        # wholly into the explicit edition contribution; availability is the
        # invariant, not either storage choice.
        ids.add("project.access")
    # Notifications is a complete capability-gated definition: both open
    # editions opt in, while a downstream managed edition can omit it.
    ids.add("project.notifications")
    # Usage/spend is an explicit open contribution while its definition is not
    # one of SETTINGS_SECTIONS. Private funding/billing contributions are never
    # implicit members of this open result.
    ids.add("organization.spend")
    return ids


def test_returned_open_ids_derive_from_registry_and_follow_runtime_addition(
    tmp_path: Path,
) -> None:
    snapshot = _registry_snapshot(tmp_path)
    private_ids = {
        "organization.funding-accounts",
        "organization.billing",
        "project.billing",
    }
    postures = sorted(snapshot["before"]["postures"])
    assert postures, "the open tree must register at least one edition posture"
    for phase in ("before", "after"):
        state = snapshot[phase]
        registry_ids = state["registry"]
        for posture in postures:
            returned = state["postures"][posture]
            assert len(returned) == len(set(returned)), (
                f"{posture} settings must remain duplicate-free"
            )
            assert set(returned) == _expected_open_ids(registry_ids, posture), (
                f"{posture} availability must be computed from SETTINGS_SECTIONS "
                "plus the explicit open edition contribution"
            )
            assert private_ids.isdisjoint(returned)

    assert "project.temporary-open" not in snapshot["before"]["registry"]
    assert "project.temporary-open" in snapshot["after"]["registry"]
    for posture in postures:
        assert "project.temporary-open" in snapshot["after"]["postures"][posture], (
            "adding an ordinary open SETTINGS_SECTIONS definition must flow "
            "through without editing a second identifier list"
        )
