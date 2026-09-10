"""Executable contracts for the source deployment Compose file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"


def _compose() -> dict[str, Any]:
    assert COMPOSE_PATH.is_file(), "docker-compose.yml is missing"
    # rule19: executes the shipped Compose artifact through a YAML parser
    text = COMPOSE_PATH.read_text()
    data = yaml.safe_load(text)
    assert isinstance(data, dict) and "services" in data, (
        "docker-compose.yml did not parse into a services mapping"
    )
    return data


def _services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = compose["services"]
    assert isinstance(services, dict)
    return services


# ---------------------------------------------------------------------------
# source compose: owned local image by default, explicit override in deploys
# ---------------------------------------------------------------------------


def test_source_compose_uses_a_version_independent_local_image_fallback() -> None:
    services = _services(_compose())
    for service_name in ("run-queue-provision", "run-queue-migrate", "app", "worker"):
        image = services[service_name]["image"]
        assert image == "${FRISKET_IMAGE:-frisket-team:local}"


# ---------------------------------------------------------------------------
# (c) the worker command: never the managed-worker-only hosted-worker
# ---------------------------------------------------------------------------


def test_worker_service_runs_the_self_hostable_worker_command() -> None:
    """frisket hosted-worker is the managed-worker-only mode: it refuses to
    register handlers without an external worker edition and crash-loops
    forever on this artifact. `frisket worker` is the self-hostable
    entry point. This was the most severe rehearsal finding — wiring the
    wrong one means no self-hosted job can ever run, silently, forever."""
    worker = _services(_compose())["worker"]
    command = worker.get("command")
    assert command is not None, "worker service must set an explicit command"
    if isinstance(command, str):
        tokens = command.split()
    else:
        tokens = list(command)
    assert "hosted-worker" not in tokens, (
        "worker service must never run `frisket hosted-worker` "
        "(managed-worker-only, crash-loops without an external edition)"
    )
    assert tokens == ["frisket", "worker"], (
        f"worker service command must be exactly ['frisket', 'worker'], got {tokens!r}"
    )


def test_app_service_runs_the_asgi_app_not_the_standalone_server_command() -> None:
    """The image's default CMD is `frisket server`, which calls
    `_standalone_env()` and unconditionally overwrites
    FRISKET_TEAM_DATABASE_URL / FRISKET_DATABASE_URL /
    FRISKET_RUN_QUEUE_DATABASE_URL with sqlite. Left unset, the `app` service
    would silently run on SQLite while `worker` runs on the Postgres control
    plane. The `app` service must set an explicit command that runs the ASGI
    app directly instead."""
    app = _services(_compose())["app"]
    command = app.get("command")
    assert command is not None, "app service must set an explicit command"
    if isinstance(command, str):
        tokens = command.split()
    else:
        tokens = list(command)
    assert tokens == [
        "uvicorn",
        "frisket.team.asgi:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--no-proxy-headers",
    ], (
        "app service command must be exactly "
        "['uvicorn', 'frisket.team.asgi:app', '--host', '0.0.0.0', "
        f"'--port', '8000', '--no-proxy-headers'], got {tokens!r}"
    )


# ---------------------------------------------------------------------------
# (d) the bundled Postgres image must be multi-arch (a tag, not a
#     single-platform pinned digest)
# ---------------------------------------------------------------------------


def test_bundled_postgres_image_is_a_tag_not_a_pinned_digest() -> None:
    """A `postgres@sha256:...` reference pins one manifest, which may be a
    single-platform (e.g. arm64-only) image rather than a multi-arch index —
    exactly what broke every ordinary amd64 cloud VPS in the rehearsal. A
    plain tag (postgres:17, postgres:17.x) always resolves through the
    official image's multi-arch index."""
    db_image = _services(_compose())["db"]["image"]
    assert isinstance(db_image, str)
    assert "@sha256:" not in db_image, (
        f"db.image {db_image!r} pins a single-manifest digest — verify "
        "multi-arch with `docker manifest inspect` before ever pinning a "
        "digest here, and prefer a plain tag by default"
    )
    assert re.match(r"^postgres:\d+", db_image), (
        f"db.image {db_image!r} should be the official postgres image on a "
        "plain major-version tag"
    )


# ---------------------------------------------------------------------------
# sanity: the fixed run-queue database/role names compose defaults to must
# match the values frisket.jobs.queue_provision hardcodes, or the default
# path breaks on first boot.
# ---------------------------------------------------------------------------


def test_run_queue_defaults_match_the_hardcoded_provisioning_contract() -> None:
    from frisket.engine.jobs.queue_provision import (
        RUN_QUEUE_DATABASE_NAME,
        RUN_QUEUE_RUNTIME_ROLE,
    )

    # rule19: two-sources: compose env names diffed against frisket.jobs.queue_provision constants
    compose_text = COMPOSE_PATH.read_text()
    assert f"/{RUN_QUEUE_DATABASE_NAME}" in compose_text, (
        f"compose's default FRISKET_RUN_QUEUE_DATABASE_URL must name the "
        f"database frisket.jobs.queue_provision hardcodes ({RUN_QUEUE_DATABASE_NAME!r})"
    )
    assert f"{RUN_QUEUE_RUNTIME_ROLE}:" in compose_text, (
        f"compose's default FRISKET_RUN_QUEUE_DATABASE_URL must use the "
        f"role frisket.jobs.queue_provision hardcodes ({RUN_QUEUE_RUNTIME_ROLE!r})"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
