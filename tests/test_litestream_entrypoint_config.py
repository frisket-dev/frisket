"""The generated litestream config is published atomically, never emptied.

`scripts/release/litestream-entrypoint.sh` regenerates a litestream config on
every rescan (default 60s) because litestream expands no globs — one explicit
`dbs:` entry per discovered project.db or nothing is replicated. Written in
place, that regeneration passes through a state that is VALID YAML NAMING ZERO
DATABASES, which nothing errors on: litestream and the restore runbook both
read it as "there is nothing to replicate/restore" and say so without failing.

Two ways to reach that state, both covered here:

* the append window — a reader that samples the config while the entrypoint is
  writing it;
* an empty project set — a data dir that is momentarily empty (volume not
  mounted yet, a rescan racing a bundle rename) truncating a good config and
  then bailing, leaving the empty config on disk until the NEXT successful
  generate, a whole rescan interval later.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release" / "litestream-entrypoint.sh"


def _fake_litestream(bin_dir: Path) -> None:
    """A litestream that never exits, so the supervise loop keeps rescanning."""
    binary = bin_dir / "litestream"
    binary.write_text("#!/bin/sh\nwhile true; do sleep 3600; done\n")
    binary.chmod(0o755)


def _data_dir(tmp_path: Path, count: int) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        bundle = data / f"proj-{index:03d}.frisket"
        bundle.mkdir()
        (bundle / "project.db").write_bytes(b"")
    return data


def _spawn(
    tmp_path: Path,
    data: Path,
    generated: Path,
    *,
    rescan: str,
    capture_log: bool = True,
) -> tuple[subprocess.Popen[str], Path]:
    """Run the entrypoint in its own session, logging to a FILE never a pipe.

    A rescanning entrypoint logs on every pass; against an unread pipe it
    blocks in the middle of `log`, where its own TERM trap cannot run, and the
    test deadlocks instead of testing anything. A zero-interval rescan logs
    faster than any reader cares to read, so that caller discards the log and
    synchronizes on the config instead.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_litestream(bin_dir)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FRISKET_DATA_DIR": str(data),
        "LITESTREAM_GENERATED_CONFIG": str(generated),
        "LITESTREAM_RESCAN_INTERVAL": rescan,
        "LITESTREAM_REPLICA_DIR": str(tmp_path / "replica"),
    }
    log = tmp_path / "entrypoint.log"
    proc = subprocess.Popen(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=log.open("w") if capture_log else subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, log


def _reap(proc: subprocess.Popen[str]) -> None:
    """Kill the whole supervise tree, without waiting on the TERM trap.

    `sh` runs a trap only after the foreground command returns, so a SIGTERM
    delivered during `sleep RESCAN_INTERVAL` is handled a whole interval later.
    Everything asserted has already been read off disk by this point, so the
    session gets SIGKILL and the fake litestream child goes with it.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait(timeout=30)


def _await(proc: subprocess.Popen[str], ready, describe) -> None:
    """Spin until ``ready()``. The iteration cap is a liveness bound, not a
    window anything is asserted inside; the entrypoint reaches every state
    below in milliseconds."""
    for _ in range(1_000_000):
        if ready():
            return
        if proc.poll() is not None:
            break
    raise AssertionError(f"{describe()} (exit={proc.poll()})")


def _yaml_database_count(text: str) -> int:
    """What a YAML reader — litestream, the restore runbook — sees."""
    config = yaml.safe_load(text)
    assert isinstance(config, dict), text
    return len(config.get("dbs") or [])


def _database_count(text: str) -> int:
    """The same count, cheap enough for a tight sampling loop.

    A full parse costs ~20ms here, which would make the sampler far slower
    than the writer it is trying to catch. Agreement with the YAML reader is
    asserted at the top of the sampling test.
    """
    return text.count("\n  - path:")


def test_an_empty_project_set_leaves_the_existing_config_alone(
    tmp_path: Path,
) -> None:
    """A momentarily-empty data dir must not replace a good config.

    This is the worse half of the bug: the zero-project branch returns before
    writing any entries, so an in-place truncate leaves a header-only config on
    disk PERMANENTLY — until some later rescan happens to find projects again,
    an interval or more later.
    """
    data = _data_dir(tmp_path, 0)
    generated = tmp_path / "litestream.gen.yml"
    generated.write_text(
        "dbs:\n"
        "  - path: ${FRISKET_DATA_DIR}/proj-000.frisket/project.db\n"
        "    replicas:\n"
        "      - type: file\n"
        "        path: /replica/proj-000.frisket\n"
    )

    proc, log = _spawn(tmp_path, data, generated, rescan="30")
    try:
        _await(
            proc,
            lambda: "no project.db found" in log.read_text(),
            lambda: f"entrypoint never reported an empty data dir:\n{log.read_text()}",
        )
    finally:
        _reap(proc)

    assert _yaml_database_count(generated.read_text()) == 1, generated.read_text()


def test_a_reader_never_samples_a_config_naming_zero_databases(
    tmp_path: Path,
) -> None:
    """The append window, sampled the way an operator's restore would hit it.

    The rescan interval is 0, so the entrypoint regenerates continuously while
    this test reads the config as fast as it can. Every sample must name the
    whole project set: a sample naming fewer is a config an operator would
    restore from and get less back than they have, with no error anywhere.
    """
    projects = 60
    data = _data_dir(tmp_path, projects)
    generated = tmp_path / "litestream.gen.yml"

    proc, _log = _spawn(tmp_path, data, generated, rescan="0", capture_log=False)
    counts: list[int] = []
    try:
        _await(
            proc,
            lambda: (
                generated.exists()
                and _database_count(generated.read_text()) == projects
            ),
            lambda: f"entrypoint never published a {projects}-db config",
        )
        # The cheap count and a real YAML reader agree on a settled config.
        settled = generated.read_text()
        assert _yaml_database_count(settled) == _database_count(settled) == projects
        for _ in range(4000):
            try:
                counts.append(_database_count(generated.read_text()))
            except FileNotFoundError:
                counts.append(-1)
    finally:
        _reap(proc)

    assert counts, "nothing was sampled"
    assert set(counts) == {projects}, {
        count: counts.count(count) for count in sorted(set(counts))
    }
