"""Integration helpers for the Stage 0A runtime-foundation acceptance checks.

These helpers deliberately do not skip.  The manifest checks which import them
are release/designated gates: once their fast semantic preconditions are green,
Docker and the pinned Postgres image are required test infrastructure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

import pytest
import sqlalchemy as sa

# realtime: these helpers poll a real Docker container's readiness and a
# real subprocess waiting on a real Postgres advisory lock — genuine
# real-child-process supervision that a manual clock cannot advance. All waits
# below are positive ("wait FOR readiness/the waiter to appear") with generous
# timeouts; consuming tests are already gap/gap_env-gated (excluded from stock
# CI).
pytestmark = pytest.mark.realtime

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE_ENV = "FRISKET_POSTGRES_TEST_IMAGE"
RUN_QUEUE_DATABASE = "frisket_run_queue"
RUN_QUEUE_RUNTIME_ROLE = "frisket_run_queue_runtime"
RUN_QUEUE_RUNTIME_PASSWORD = "stage0a-runtime-secret"


def subprocess_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    merged = os.environ.copy()
    for key in (
        "FRISKET_DATABASE_ADMIN_URL",
        "FRISKET_RUN_QUEUE_DATABASE_URL",
        "FRISKET_DATABASE_URL",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
    ):
        merged.pop(key, None)
    if env:
        merged.update({key: str(value) for key, value in env.items()})
    source_path = str(ROOT / "src")
    merged["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, merged.get("PYTHONPATH", "")) if part
    )
    return merged


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path = ROOT,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=subprocess_env(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def require_ok(
    proc: subprocess.CompletedProcess[str],
    *,
    what: str,
) -> subprocess.CompletedProcess[str]:
    assert proc.returncode == 0, (
        f"{what} failed with exit {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def require_queue_cli() -> None:
    """Fail on the product gap before a missing Docker daemon can obscure it."""
    from frisket.cli import _subcommands

    commands = _subcommands()
    assert "queue" in commands, (
        "the runtime foundation requires the real `frisket queue` command "
        "with `provision` and `migrate` subcommands"
    )
    help_proc = run([sys.executable, "-m", "frisket.cli", "queue", "--help"])
    require_ok(help_proc, what="frisket queue --help")
    help_text = (help_proc.stdout + help_proc.stderr).lower()
    assert "provision" in help_text and "migrate" in help_text


def queue_cli(
    command: str,
    *,
    env: Mapping[str, str],
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    require_queue_cli()
    return run(
        [sys.executable, "-m", "frisket.cli", "queue", command],
        env=env,
        timeout=timeout,
    )


def queue_cli_process(
    command: str,
    *,
    env: Mapping[str, str],
) -> subprocess.Popen[str]:
    require_queue_cli()
    return subprocess.Popen(
        [sys.executable, "-m", "frisket.cli", "queue", command],
        cwd=ROOT,
        env=subprocess_env(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_docker() -> str:
    binary = shutil.which("docker")
    assert binary, "Docker CLI is required by this non-skippable release gate"
    proc = run([binary, "version", "--format", "{{.Server.Version}}"], timeout=20)
    require_ok(proc, what="Docker daemon probe")
    version = proc.stdout.strip()
    assert version, "Docker daemon probe returned no server version"
    return binary


def require_local_image(image: str) -> str:
    docker = require_docker()
    proc = run([docker, "image", "inspect", image], timeout=20)
    assert proc.returncode == 0, (
        f"required image {image!r} is not present in the designated gate; "
        "pre-pull it rather than turning this proof into a skip\n"
        f"{proc.stderr}"
    )
    return docker


def postgres_image_reference() -> str:
    image = os.environ.get(POSTGRES_IMAGE_ENV, "").strip()
    assert image, (
        f"{POSTGRES_IMAGE_ENV} must name the preloaded immutable Postgres gate "
        "image (for example postgres:17-alpine@sha256:<64 hex>); this proof "
        "does not pull implicitly"
    )
    assert re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image), (
        f"{POSTGRES_IMAGE_ENV} must be a repository reference pinned by sha256 "
        f"digest, got {image!r}"
    )
    require_local_image(image)
    return image


@dataclass(frozen=True)
class PostgresServer:
    container: str
    host: str
    port: int
    user: str
    password: str

    def url(self, database: str) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{database}"
        )

    def role_url(self, *, role: str, password: str, database: str) -> str:
        return (
            f"postgresql+psycopg://{role}:{password}@{self.host}:{self.port}/{database}"
        )

    @property
    def admin_url(self) -> str:
        return self.url("postgres")

    @property
    def control_url(self) -> str:
        return self.url("frisket")

    @property
    def queue_url(self) -> str:
        return self.url(RUN_QUEUE_DATABASE)

    @property
    def runtime_url(self) -> str:
        return self.runtime_url_for(RUN_QUEUE_DATABASE)

    def runtime_url_for(self, database: str) -> str:
        return self.role_url(
            role=RUN_QUEUE_RUNTIME_ROLE,
            password=RUN_QUEUE_RUNTIME_PASSWORD,
            database=database,
        )


@contextmanager
def postgres_server(*, password: str = "stage0a-secret") -> Iterator[PostgresServer]:
    image = postgres_image_reference()
    docker = require_local_image(image)
    name = f"frisket-stage0a-pg-{uuid.uuid4().hex[:12]}"
    started = run(
        [
            docker,
            "run",
            "--pull=never",
            "--detach",
            "--name",
            name,
            "--env",
            "POSTGRES_USER=frisket",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=frisket",
            "--publish",
            "127.0.0.1::5432",
            image,
        ],
        timeout=30,
    )
    require_ok(started, what="disposable Postgres container start")
    try:
        port_proc = run([docker, "port", name, "5432/tcp"], timeout=10)
        require_ok(port_proc, what="disposable Postgres port inspection")
        match = re.search(r":(\d+)\s*$", port_proc.stdout)
        assert match, f"could not parse published Postgres port: {port_proc.stdout!r}"
        server = PostgresServer(
            container=name,
            host="127.0.0.1",
            port=int(match.group(1)),
            user="frisket",
            password=password,
        )
        deadline = time.monotonic() + 40
        last = "not yet accepting published TCP SQL connections"
        while time.monotonic() < deadline:
            try:
                # The official image briefly exposes its init-only Unix socket
                # before restarting the final server. Readiness for this gate is
                # a real SQL round trip over the published TCP port that product
                # processes use, not pg_isready against that temporary socket.
                if scalar(server.control_url, "SELECT 1") == 1:
                    last = ""
                    break
            except (sa.exc.SQLAlchemyError, OSError) as exc:
                # Exception text can contain the connection URL and password.
                last = type(exc).__name__
            if last:
                time.sleep(0.25)
            else:
                break
        else:
            logs = run([docker, "logs", name], timeout=10)
            pytest.fail(
                "disposable Postgres did not become ready: "
                f"{last}\ncontainer logs:\n{logs.stdout}\n{logs.stderr}"
            )
        yield server
    finally:
        run([docker, "rm", "--force", name], timeout=20)


def scalar(url: str, sql: str, params: Mapping[str, object] | None = None) -> object:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.text(sql), params or {}).scalar()
    finally:
        engine.dispose()


def run_waiting_on_advisory_lock(
    url: str,
    *,
    lock_id: int,
    start: Callable[[], subprocess.Popen[str]],
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    """Prove a real child blocks on the exact advisory lock before releasing it."""
    engine = sa.create_engine(url)
    process: subprocess.Popen[str] | None = None
    try:
        with engine.connect() as blocker:
            blocker.execute(
                sa.text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id}
            )
            process = start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                waiting = scalar(
                    url,
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype='advisory' AND granted=false",
                )
                if int(waiting or 0) > 0:
                    break
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(
                        "queue command exited before waiting on its required advisory "
                        f"lock (exit {process.returncode})\nstdout:\n{stdout}\n"
                        f"stderr:\n{stderr}"
                    )
                time.sleep(0.05)
            else:
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(
                    "queue command never appeared as a waiter on the exact real "
                    f"Postgres advisory lock\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
            blocker.execute(
                sa.text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
            )
        stdout, stderr = process.communicate(timeout=timeout)
    finally:
        engine.dispose()
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process is not None
    return subprocess.CompletedProcess(
        args=process.args,
        returncode=int(process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
    )


def rows(
    url: str,
    sql: str,
    params: Mapping[str, object] | None = None,
) -> list[Mapping[str, object]]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(sa.text(sql), params or {}).mappings()
            ]
    finally:
        engine.dispose()


def execute(url: str, sql: str, params: Mapping[str, object] | None = None) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(sql), params or {})
    finally:
        engine.dispose()


def queue_schema_fingerprint(url: str) -> tuple[tuple[object, ...], ...]:
    """Capture tables, columns, constraints, and indexes—not table data."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            tables = connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' ORDER BY table_name"
                )
            ).all()
            columns = connection.execute(
                sa.text(
                    "SELECT table_name, column_name, data_type, is_nullable, "
                    "coalesce(column_default, '') FROM information_schema.columns "
                    "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
                )
            ).all()
            constraints = connection.execute(
                sa.text(
                    "SELECT table_name, constraint_name, constraint_type "
                    "FROM information_schema.table_constraints "
                    "WHERE table_schema='public' "
                    "ORDER BY table_name, constraint_name"
                )
            ).all()
            indexes = connection.execute(
                sa.text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname='public' ORDER BY tablename, indexname"
                )
            ).all()
    finally:
        engine.dispose()
    tagged: list[tuple[object, ...]] = []
    tagged.extend(("table", *tuple(row)) for row in tables)
    tagged.extend(("column", *tuple(row)) for row in columns)
    tagged.extend(("constraint", *tuple(row)) for row in constraints)
    tagged.extend(("index", *tuple(row)) for row in indexes)
    return tuple(tagged)
