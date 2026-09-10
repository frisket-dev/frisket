"""Release-workflow contract for the generated frontend SDK boundary."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-public-artifacts.yml"


def test_release_regenerates_and_checks_sdk_before_building_web() -> None:
    # rule19: the release workflow is the SDK-producer contract under test
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]

    setup_index, setup = next(
        (index, step)
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    web_install_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "npm ci" and step.get("working-directory") == "web"
    )
    sdk_install_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "npm ci" and step.get("working-directory") == "sdk"
    )
    verify_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify vendored plugin SDK freshness"
    )
    web_build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build all frontend editions"
    )

    assert setup["with"]["cache-dependency-path"].splitlines() == [
        "web/package-lock.json",
        "sdk/package-lock.json",
    ]
    assert setup_index < web_install_index < sdk_install_index < verify_index
    assert verify_index < web_build_index

    script = steps[verify_index]["run"]
    assert "trap restore_sdk_vendor EXIT" in script
    assert script.index("npm --prefix sdk run build") < script.index(
        'cp "$sdk_vendor_snapshot" "$sdk_vendor"',
        script.index("npm --prefix sdk run build"),
    )
    assert 'cmp sdk/dist/index.mjs "$sdk_vendor"' in script
    assert 'cmp "$sdk_vendor" "$sdk_geo_vendor"' in script
