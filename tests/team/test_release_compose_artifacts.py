"""Static contract for the small, release-only Compose artifacts."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "deploy" / "release"


def _read(name: str) -> dict:
    return yaml.safe_load((RELEASE / name).read_text())


def _images(compose: dict) -> list[str]:
    return [service["image"] for service in compose["services"].values()]


def test_release_packages_are_two_small_no_build_service_graphs() -> None:
    standalone = _read("compose.standalone.yaml")
    multi = _read("compose.multi-service.yaml")
    assert set(standalone["services"]) == {"app"}
    assert set(multi["services"]) == {
        "app",
        "worker",
        "db",
        "run-queue-provision",
        "run-queue-migrate",
    }
    for compose in (standalone, multi):
        assert (
            "build:"
            not in (
                RELEASE
                / (
                    "compose.standalone.yaml"
                    if compose is standalone
                    else "compose.multi-service.yaml"
                )
            ).read_text()
        )
        assert all("@sha256" not in image for image in _images(compose))
        assert all("${" in image and ":?" in image for image in _images(compose))
        assert "profiles" not in str(compose)


def test_release_network_exposure_is_loopback_or_caddy_only() -> None:
    standalone = _read("compose.standalone.yaml")
    multi = _read("compose.multi-service.yaml")
    edge = _read("compose.edge.yaml")
    assert standalone["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert multi["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert "ports" not in multi["services"]["db"]
    assert edge["services"]["caddy"]["ports"] == ["80:80", "443:443"]
    assert "reverse_proxy app:8000" in (RELEASE / "Caddyfile").read_text()


def test_local_asr_hub_cache_uses_the_shared_persistent_data_mount() -> None:
    standalone = _read("compose.standalone.yaml")
    multi = _read("compose.multi-service.yaml")
    assert "./data:/data" in standalone["services"]["app"]["volumes"]
    for service_name in ("app", "worker"):
        assert "./data:/data" in multi["services"][service_name]["volumes"]


def test_multi_service_uses_an_existing_app_command_and_flat_worker_root() -> None:
    multi = _read("compose.multi-service.yaml")
    assert multi["services"]["app"]["command"][:2] == [
        "uvicorn",
        "frisket.team.asgi:app",
    ]
    assert "--no-proxy-headers" in multi["services"]["app"]["command"]
    assert (
        multi["services"]["worker"]["environment"]["FRISKET_PROJECTS_ROOT"] == "/data"
    )
    for service in ("app", "worker"):
        environment = multi["services"][service]["environment"]
        assert environment["FRISKET_RUN_QUEUE_SCHEMA_MODE"] == "strict"
        assert environment["FRISKET_SECRETS_KEY_FILE"] == "/data/secrets/master.key"
        assert "FRISKET_DATABASE_ADMIN_URL" not in environment
    assert (
        "FRISKET_WORKER_VERSION_REFUSE"
        not in multi["services"]["worker"]["environment"]
    )
    for service in ("worker", "run-queue-provision", "run-queue-migrate"):
        assert multi["services"][service]["healthcheck"] == {"disable": True}


def test_release_forwards_optional_models_sidecar_to_recipe_processes() -> None:
    standalone = _read("compose.standalone.yaml")
    multi = _read("compose.multi-service.yaml")
    expected = {
        "FRISKET_MODELS_URL": "${FRISKET_MODELS_URL:-}",
        "FRISKET_MODELS_TOKEN": "${FRISKET_MODELS_TOKEN:-}",
    }

    assert {
        key: standalone["services"]["app"]["environment"][key] for key in expected
    } == expected
    for service_name in ("app", "worker"):
        assert {
            key: multi["services"][service_name]["environment"][key] for key in expected
        } == expected


def test_release_forwards_explicit_proxy_trust_to_each_app() -> None:
    standalone = _read("compose.standalone.yaml")
    multi = _read("compose.multi-service.yaml")
    expected = "${FRISKET_TRUSTED_PROXY_CIDRS:-}"

    assert (
        standalone["services"]["app"]["environment"]["FRISKET_TRUSTED_PROXY_CIDRS"]
        == expected
    )
    assert (
        multi["services"]["app"]["environment"]["FRISKET_TRUSTED_PROXY_CIDRS"]
        == expected
    )


def test_caddy_digest_pin_matches_between_release_workflow_and_heavy_compose() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-public-artifacts.yml"
    compose_path = ROOT / "docker-compose.heavy.yml"
    workflow_match = re.search(
        r"CADDY_IMAGE: docker\.io/library/caddy@(sha256:[0-9a-f]{64})",
        # rule19: two-sources: release-workflow Caddy digest diffed against docker-compose.heavy.yml
        workflow_path.read_text(),
    )
    compose_match = re.search(
        r"image: caddy@(sha256:[0-9a-f]{64})",
        # rule19: two-sources: docker-compose.heavy.yml Caddy digest diffed against release workflow
        compose_path.read_text(),
    )
    assert workflow_match, f"no Caddy digest pin found in {workflow_path}"
    assert compose_match, f"no Caddy digest pin found in {compose_path}"
    assert workflow_match.group(1) == compose_match.group(1), (
        f"Caddy image digest differs between {workflow_path} and {compose_path}"
    )


def test_python_base_image_matches_between_dockerfile_and_sidecar_dockerfile() -> None:
    dockerfile_path = ROOT / "Dockerfile"
    sidecar_path = ROOT / "sidecar" / "Dockerfile"
    dockerfile_match = re.search(
        r"(?m)^FROM (python:\S+)\s*$",
        # rule19: two-sources: Dockerfile python base diffed against sidecar/Dockerfile
        dockerfile_path.read_text(),
    )
    # rule19: two-sources: sidecar/Dockerfile python base diffed against Dockerfile
    sidecar_match = re.search(r"(?m)^FROM (python:\S+)\s*$", sidecar_path.read_text())
    assert dockerfile_match, f"no python base image found in {dockerfile_path}"
    assert sidecar_match, f"no python base image found in {sidecar_path}"
    assert dockerfile_match.group(1) == sidecar_match.group(1), (
        f"Python base image differs between {dockerfile_path} and {sidecar_path}"
    )


def test_dockerfile_warmup_model_ids_match_the_frisket_constants() -> None:
    # rule19: two-sources: Dockerfile warm-up model ids diffed against semantic.py/search.py constants
    dockerfile = (ROOT / "Dockerfile").read_text()
    # rule19: two-sources: semantic.py LOCAL_MODEL diffed against Dockerfile warm-up
    semantic_source = (ROOT / "src" / "frisket" / "semantic.py").read_text()
    # rule19: two-sources: search.py RERANK_MODEL diffed against Dockerfile warm-up
    search_source = (ROOT / "src" / "frisket" / "search.py").read_text()

    local_model_match = re.search(r'(?m)^LOCAL_MODEL = "([^"]+)"$', semantic_source)
    rerank_model_match = re.search(r'(?m)^RERANK_MODEL = "([^"]+)"$', search_source)
    assert local_model_match, "no LOCAL_MODEL constant found in semantic.py"
    assert rerank_model_match, "no RERANK_MODEL constant found in search.py"

    assert f"TextEmbedding('{local_model_match.group(1)}')" in dockerfile, (
        "Dockerfile warm-up embeddings model id differs from frisket.semantic.LOCAL_MODEL"
    )
    assert f"TextCrossEncoder('{rerank_model_match.group(1)}')" in dockerfile, (
        "Dockerfile warm-up rerank model id differs from frisket.search.RERANK_MODEL"
    )
