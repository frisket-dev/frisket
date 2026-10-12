"""Invalid worker configuration is refused at composition time."""

from pathlib import Path

import pytest

from frisket.engine.jobs import SqliteJobQueue, default_registry
from frisket.engine.jobs.ports import WorkerPorts
from frisket.engine.jobs.worker import register_production_handlers


@pytest.mark.parametrize(
    "parameter",
    [
        "worker_ports",
        "execution_composition_factory",
        "execution_composition_overlay",
        "execution_router_factory",
    ],
)
def test_unknown_port_implementation_is_refused(tmp_path: Path, parameter: str) -> None:
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    try:
        with pytest.raises(TypeError, match=parameter):
            register_production_handlers(
                registry=default_registry(),
                workspace_root=tmp_path,
                queue=queue,
                **{parameter: object()},
            )
    finally:
        queue.close()


@pytest.mark.parametrize("role", ["admission", "settlement"])
def test_malformed_worker_port_is_refused(role: str) -> None:
    with pytest.raises(TypeError, match=role):
        WorkerPorts(**{f"{role}_port": object()})
