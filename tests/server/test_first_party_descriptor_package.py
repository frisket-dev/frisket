"""first-party-descriptors-runtime-index-v1: red-first checks for the
checked-in first-party workbench descriptor package.

Three checks define the public contract:
  1. the artifact passes load_workbench_descriptor_package_file unmodified
     (the SAME loader plugin packages go through — no more-permissive
     first-party shape).
  2. the runtime index response's firstParty section is byte-equal (as
     parsed JSON) to the artifact.
  3. every contribution id in the artifact has a frontend registry binding
     in web/src/workbench/firstPartyComponents.ts (the binding seam D1
     introduces so first-party componentKey/handlerKey values, deliberately
     absent from the artifact, still resolve).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.authoring.workbench.contracts import (
    FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH,
    load_first_party_workbench_descriptor_package,
    load_workbench_descriptor_package_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRST_PARTY_COMPONENTS_TS = (
    REPO_ROOT / "web" / "src" / "workbench" / "firstPartyComponents.ts"
)
FRONTEND_DESCRIPTOR_PACKAGE_PATH = (
    REPO_ROOT / "web" / "src" / "assets" / "first_party_workbench_descriptors.json"
)


def _artifact() -> dict:
    return json.loads(
        FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH.read_text(encoding="utf-8")
    )


def test_artifact_passes_the_same_loader_plugin_packages_use_unmodified() -> None:
    artifact = _artifact()
    assert artifact["schemaVersion"] == "frisket.workbench_descriptor_package.v1"
    assert len(artifact["descriptors"]) > 0

    # First-party and plugin-shipped descriptor packages pass through the same
    # loader, so first-party descriptors cannot use shapes plugins cannot.
    loaded = load_workbench_descriptor_package_file(
        FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH
    )
    assert loaded.schema_version == artifact["schemaVersion"]
    assert loaded.descriptor_manifests == artifact["descriptors"]

    # No runtime-only fields (componentKey/handlerKey/...) leaked into the
    # checked-in artifact — binding lives in the frontend registry instead.
    runtime_only_fields = {
        "componentKey",
        "handlerKey",
        "lazyImport",
        "component",
        "callback",
    }
    for descriptor in artifact["descriptors"]:
        assert runtime_only_fields.isdisjoint(descriptor.keys()), descriptor["id"]


def test_frontend_host_ships_the_exact_first_party_descriptor_package() -> None:
    # rule19: two-sources: packaged frontend descriptors vs canonical backend artifact
    assert json.loads(FRONTEND_DESCRIPTOR_PACKAGE_PATH.read_text(encoding="utf-8")) == (
        json.loads(
            FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH.read_text(encoding="utf-8")
        )
    )


def test_load_first_party_workbench_descriptor_package_matches_the_generic_loader() -> (
    None
):
    generic = load_workbench_descriptor_package_file(
        FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH
    )
    dedicated = load_first_party_workbench_descriptor_package()
    assert dedicated.schema_version == generic.schema_version
    assert dedicated.descriptor_manifests == generic.descriptor_manifests


def test_runtime_index_first_party_section_equals_the_artifact(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "first-party descriptor runtime index"}
    ).json()["id"]

    response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert response.status_code == 200, response.text
    body = response.json()

    artifact = _artifact()
    # Honest firstParty section — NOT fake plugin entries. First-party
    # contributions carry no installState/receipts/moduleUrl; the section is
    # exactly {schemaVersion, descriptors} projected from the checked-in
    # artifact (descriptor-metadata-honesty precedent).
    assert set(body["firstParty"].keys()) == {"schemaVersion", "descriptors"}
    assert body["firstParty"]["schemaVersion"] == artifact["schemaVersion"]
    assert body["firstParty"]["descriptors"] == artifact["descriptors"]

    # The plugins section is untouched by the first-party addition — no
    # invented lifecycle rows for first-party contributions.
    assert body["loadedPluginCount"] == 0
    assert body["plugins"] == []


def test_every_artifact_contribution_id_has_a_frontend_registry_binding() -> None:
    artifact = _artifact()
    # rule19: two-sources: diffs the shipped descriptor artifact against the web component registry
    registry_source = FIRST_PARTY_COMPONENTS_TS.read_text(encoding="utf-8")

    missing = [
        descriptor["id"]
        for descriptor in artifact["descriptors"]
        if not re.search(
            rf"['\"]{re.escape(descriptor['id'])}['\"]\s*:\s*\{{", registry_source
        )
    ]
    assert missing == [], (
        f"web/src/workbench/firstPartyComponents.ts is missing a binding for: {missing}"
    )
