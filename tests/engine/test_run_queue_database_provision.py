"""RED-first real-Postgres checks for the dedicated queue DB provisioner."""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import re
import shlex
from pathlib import Path

import pytest
import yaml

from tests.runtime_foundation_test_helpers import (
    ROOT,
    RUN_QUEUE_DATABASE,
    RUN_QUEUE_RUNTIME_PASSWORD,
    execute,
    postgres_server,
    queue_cli,
    queue_cli_process,
    require_queue_cli,
    require_ok,
    rows,
    run_waiting_on_advisory_lock,
    scalar,
)


ADMIN_ENV = "FRISKET_DATABASE_ADMIN_URL"
QUEUE_ENV = "FRISKET_RUN_QUEUE_DATABASE_URL"
SCHEMA_MODE_ENV = "FRISKET_RUN_QUEUE_SCHEMA_MODE"
pytestmark = pytest.mark.gap


def _provision_env(
    postgres,
    *,
    runtime_url: str | None = None,
) -> dict[str, str]:
    return {
        ADMIN_ENV: postgres.admin_url,
        QUEUE_ENV: runtime_url or postgres.runtime_url,
    }


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _environment_keys(service: dict) -> set[str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return set(raw)
    return {str(item).split("=", 1)[0] for item in raw}


def _environment_value(service: dict, key: str) -> object | None:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return raw.get(key)
    prefix = f"{key}="
    return next(
        (str(item)[len(prefix) :] for item in raw if str(item).startswith(prefix)),
        None,
    )


def _compose_default(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    match = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", text)
    return match.group(1) if match else text


def _env_files(service: dict, *, compose_path: Path) -> set[Path]:
    raw = service.get("env_file", [])
    entries = [raw] if isinstance(raw, (str, dict)) else raw
    paths: set[Path] = set()
    for entry in entries:
        value = entry.get("path") if isinstance(entry, dict) else entry
        if value:
            assert "${" not in str(value), (
                "env_file paths at this secret boundary must resolve statically so "
                "admin-source separation is auditable"
            )
            paths.add((compose_path.parent / str(value)).resolve())
    return paths


def _secret_tokens(service: dict) -> set[str]:
    raw = service.get("secrets", [])
    entries = raw if isinstance(raw, list) else [raw]
    targets: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            targets.add(str(entry.get("target") or ""))
            targets.add(str(entry.get("source") or ""))
        else:
            targets.add(str(entry))
    return targets


def _condition(service: dict, dependency: str) -> str | None:
    depends = service.get("depends_on", {})
    if isinstance(depends, list):
        return None if dependency not in depends else "started"
    value = depends.get(dependency)
    return value.get("condition") if isinstance(value, dict) else "started"


def _command_tokens(service: dict) -> list[str]:
    entrypoint = service.get("entrypoint", [])
    command = service.get("command", [])
    prefix = shlex.split(entrypoint) if isinstance(entrypoint, str) else entrypoint
    suffix = shlex.split(command) if isinstance(command, str) else command
    return [str(token) for token in (*prefix, *suffix)]


def _provision_lock_id() -> int:
    module_name = "frisket.engine.jobs.queue_provision"
    assert importlib.util.find_spec(module_name) is not None, (
        "missing product seam: frisket.jobs.queue_provision must own the "
        "identifier-safe advisory-locked database provisioner"
    )
    module = importlib.import_module(module_name)
    lock_id = getattr(module, "QUEUE_DATABASE_PROVISION_LOCK_ID", None)
    assert isinstance(lock_id, int), (
        f"{module_name} is missing integer QUEUE_DATABASE_PROVISION_LOCK_ID"
    )
    return lock_id


@pytest.mark.gap_env
def test_existing_postgres_volume_provisions_fixed_run_queue_database_idempotently() -> (
    None
):
    require_queue_cli()
    with postgres_server() as postgres:
        execute(
            postgres.control_url,
            "CREATE TABLE existing_control_marker (id integer primary key, value text)",
        )
        execute(
            postgres.control_url,
            "INSERT INTO existing_control_marker (id, value) VALUES (1, 'preserve-me')",
        )
        assert (
            scalar(
                postgres.admin_url,
                "SELECT count(*) FROM pg_database WHERE datname='frisket_run_queue'",
            )
            == 0
        )
        env = _provision_env(postgres)
        for missing, label in (
            (ADMIN_ENV, "admin"),
            (QUEUE_ENV, "runtime"),
        ):
            incomplete = dict(env)
            incomplete.pop(missing)
            refused = queue_cli("provision", env=incomplete)
            output = refused.stdout + refused.stderr
            assert refused.returncode != 0, (
                f"queue provision accepted a missing {label} capability"
            )
            assert postgres.admin_url not in output
            assert RUN_QUEUE_RUNTIME_PASSWORD not in output

        require_ok(queue_cli("provision", env=env), what="first queue provision")
        require_ok(queue_cli("provision", env=env), what="idempotent queue provision")

        assert (
            scalar(
                postgres.admin_url,
                "SELECT count(*) FROM pg_database WHERE datname='frisket_run_queue'",
            )
            == 1
        )
        assert (
            scalar(
                postgres.control_url,
                "SELECT value FROM existing_control_marker WHERE id=1",
            )
            == "preserve-me"
        )
        assert (
            scalar(postgres.queue_url, "SELECT current_database()")
            == RUN_QUEUE_DATABASE
        )


@pytest.mark.gap_env
def test_provision_is_locked_identifier_safe_and_secret_redacted() -> None:
    require_queue_cli()
    lock_id = _provision_lock_id()

    secret = "stage0a-provision-secret"
    with postgres_server(password=secret) as postgres:
        env = _provision_env(postgres)
        locked = run_waiting_on_advisory_lock(
            postgres.admin_url,
            lock_id=lock_id,
            start=lambda: queue_cli_process("provision", env=env),
        )
        require_ok(locked, what="provision after exact advisory-lock release")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            procs = list(
                pool.map(lambda _index: queue_cli("provision", env=env), range(2))
            )
        for proc in procs:
            require_ok(proc, what="concurrent locked queue provision")
            output = proc.stdout + proc.stderr
            assert secret not in output
            for locator in (
                postgres.admin_url,
                postgres.runtime_url,
            ):
                assert locator not in output
            assert RUN_QUEUE_RUNTIME_PASSWORD not in output
        assert (
            scalar(
                postgres.admin_url,
                "SELECT count(*) FROM pg_database WHERE datname='frisket_run_queue'",
            )
            == 1
        )

        unsafe_database = "frisket-run-queue;drop database frisket"
        unsafe_runtime_url = postgres.runtime_url_for(unsafe_database)
        unsafe = queue_cli(
            "provision",
            env=_provision_env(postgres, runtime_url=unsafe_runtime_url),
        )
        assert unsafe.returncode != 0
        unsafe_output = unsafe.stdout + unsafe.stderr
        assert secret not in unsafe_output
        assert RUN_QUEUE_RUNTIME_PASSWORD not in unsafe_output
        assert (
            postgres.admin_url not in unsafe_output
            and unsafe_runtime_url not in unsafe_output
        )
        names = {
            str(row["datname"])
            for row in rows(
                postgres.admin_url,
                "SELECT datname FROM pg_database ORDER BY datname",
            )
        }
        assert "frisket" in names and "frisket_run_queue" in names
        assert unsafe_database not in names

        # PostgreSQL itself rejects CREATE DATABASE inside a transaction. The
        # successfully connected new DB above is the transaction-boundary proof.


def test_compose_orders_provision_before_schema_migration() -> None:
    path = ROOT / "docker-compose.yml"
    services = _load_compose(path)["services"]
    assert "run-queue-provision" in services, f"{path} lacks one-shot provision"
    assert "run-queue-migrate" in services, f"{path} lacks one-shot migration"
    assert _condition(services["run-queue-migrate"], "run-queue-provision") == (
        "service_completed_successfully"
    )
    provision = services["run-queue-provision"]
    migrate = services["run-queue-migrate"]
    assert _command_tokens(provision) == ["frisket", "queue", "provision"]
    assert _command_tokens(migrate) == ["frisket", "queue", "migrate"]
    assert provision.get("image") == services["app"].get("image")
    assert migrate.get("image") == services["app"].get("image")
    admin_value = _environment_value(provision, ADMIN_ENV)
    assert ADMIN_ENV in _environment_keys(provision)
    assert isinstance(admin_value, str) and admin_value.startswith(
        "${FRISKET_DATABASE_ADMIN_URL"
    ), (
        f"{path} must interpolate {ADMIN_ENV} into only the owner-principal "
        "services (provision, migrate); a committed literal credential/DSN "
        "is forbidden"
    )
    assert "postgresql" not in admin_value and "://" not in admin_value
    admin_sources = {
        (path.parent / ".env").resolve(),
        *_env_files(provision, compose_path=path),
        *_env_files(migrate, compose_path=path),
    }

    for non_admin_service in (services["app"], services["worker"]):
        assert ADMIN_ENV not in _environment_keys(non_admin_service)
        assert admin_sources.isdisjoint(
            _env_files(non_admin_service, compose_path=path)
        )
        assert ADMIN_ENV not in _secret_tokens(non_admin_service)
        assert ADMIN_ENV not in str(non_admin_service.get("environment", {}))
        argv = " ".join(str(part) for part in non_admin_service.get("command", []))
        assert ADMIN_ENV not in argv and "postgresql" not in argv

    for service, expected_capabilities in (
        (provision, {ADMIN_ENV, QUEUE_ENV}),
        (migrate, {ADMIN_ENV, QUEUE_ENV}),
        (services["app"], {QUEUE_ENV}),
        (services["worker"], {QUEUE_ENV}),
    ):
        keys = _environment_keys(service)
        for capability in (ADMIN_ENV, QUEUE_ENV):
            assert (capability in keys) is (capability in expected_capabilities)
        for capability in expected_capabilities:
            value = _environment_value(service, capability)
            assert isinstance(value, str) and value.startswith(f"${{{capability}"), (
                f"{path} must pass {capability} by its named environment locator"
            )
            assert "postgresql" not in value and "://" not in value

    for runtime in ("app", "worker"):
        assert _condition(services[runtime], "run-queue-migrate") == (
            "service_completed_successfully"
        )
        assert QUEUE_ENV in _environment_keys(services[runtime])
        assert (
            _compose_default(_environment_value(services[runtime], SCHEMA_MODE_ENV))
            == "strict"
        )

    for one_shot in (provision, migrate):
        argv = " ".join(str(part) for part in one_shot.get("command", []))
        assert "postgresql" not in argv and "://" not in argv
