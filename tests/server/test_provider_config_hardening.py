"""Process-safety and lossless-refusal pins for provider settings.

subprocess-boundary: the race crosses OS process locks and cannot be proven
with threads or an in-process fake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from frisket.server import provider_config

pytestmark = pytest.mark.realtime


Mutation = Callable[[Path], object]


MUTATIONS: tuple[tuple[str, Mutation], ...] = (
    (
        "save_provider_key",
        lambda root: provider_config.save_local_provider_key(root, "openai", "sk-new"),
    ),
    (
        "delete_provider_key",
        lambda root: provider_config.delete_local_provider_key(root, "openai"),
    ),
    (
        "create_local_endpoint",
        lambda root: provider_config.create_local_endpoint(
            root,
            name="Loopback",
            url="http://127.0.0.1:11434",
            inference_token="inference",
            provisioning_token="provisioning",
            edge_auth=True,
        ),
    ),
    (
        "patch_local_endpoint",
        lambda root: provider_config.patch_local_endpoint(
            root, "local-a1b2c3d4e5f6", {"display_name": "Renamed"}
        ),
    ),
    (
        "delete_local_endpoint",
        lambda root: provider_config.delete_local_endpoint(root, "local-a1b2c3d4e5f6"),
    ),
)


INVALID_CONFIGS: tuple[tuple[str, bytes], ...] = (
    ("malformed_json", b'{"openai": "preserve-me"'),
    ("valid_non_object", b'["preserve", "me"]'),
    ("filtered_empty_value", b'{"openai": ""}'),
    (
        "filtered_invalid_value",
        b'{"openai": "preserve-me", "unexpected": false}',
    ),
)


@pytest.mark.parametrize(("shape", "original"), INVALID_CONFIGS)
@pytest.mark.parametrize(("mutation_name", "mutation"), MUTATIONS)
def test_every_mutation_refuses_invalid_config_without_changing_bytes(
    tmp_path: Path,
    shape: str,
    original: bytes,
    mutation_name: str,
    mutation: Mutation,
) -> None:
    del shape, mutation_name
    root = tmp_path / "workspace"
    path = provider_config.local_secrets_path(root)
    path.parent.mkdir(parents=True)
    path.write_bytes(original)

    with pytest.raises(
        provider_config.InvalidProviderConfigError,
        match="cannot safely update invalid provider settings",
    ):
        mutation(root)

    assert path.read_bytes() == original


def test_mutation_refuses_to_publish_an_invalid_postimage(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    def publish_invalid(config: dict[str, str]) -> tuple[None, bool]:
        config["openai"] = ""
        return None, True

    with pytest.raises(
        provider_config.InvalidProviderConfigError,
        match="every value a non-empty string",
    ):
        provider_config._mutate_config_file(root, publish_invalid)

    assert provider_config.local_secrets_path(root).exists() is False


_COLLISION_CHILD = """
import os
import sys
import time
from pathlib import Path

from frisket.server import provider_config

root = Path(sys.argv[1])
gate_dir = Path(sys.argv[2])
worker = sys.argv[3]
iterations = int(sys.argv[4])
(gate_dir / f"{os.getpid()}.ready").touch()
while not (gate_dir / "go").exists():
    time.sleep(0.005)

for index in range(iterations):
    key = f"collision_{worker}_{index}"

    def add(config, *, key=key):
        config[key] = f"value-{worker}-{index}"
        return None, True

    provider_config._mutate_config_file(root, add)

print(os.getpid(), flush=True)
"""


def _wait_until(predicate: Callable[[], bool], *, description: str) -> None:
    deadline = time.monotonic() + 10  # realtime: bounded real-process rendezvous
    while not predicate():
        if time.monotonic() >= deadline:  # realtime: see rendezvous above
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(0.01)  # realtime: polling two real child-process markers


def test_two_process_mutations_preserve_every_update(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()
    iterations = 30
    processes = [
        subprocess.Popen(  # subprocess-boundary: process locking is the subject
            [
                sys.executable,  # subprocess-boundary: run this environment's package
                "-c",
                _COLLISION_CHILD,
                str(root),
                str(gate_dir),
                worker,
                str(iterations),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        for worker in ("a", "b")
    ]
    try:
        pids = {process.pid for process in processes}
        assert None not in pids
        assert len(pids) == 2
        _wait_until(
            lambda: all((gate_dir / f"{pid}.ready").exists() for pid in pids),
            description="both process barriers",
        )
        (gate_dir / "go").touch()

        observed_pids: set[int] = set()
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            observed_pids.add(int(stdout.strip()))
        assert observed_pids == pids
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait(timeout=5)

    data = json.loads(provider_config.local_secrets_path(root).read_text())
    expected = {
        f"collision_{worker}_{index}": f"value-{worker}-{index}"
        for worker in ("a", "b")
        for index in range(iterations)
    }
    assert data == expected
