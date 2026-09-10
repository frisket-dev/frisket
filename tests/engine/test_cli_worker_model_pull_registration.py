"""``frisket worker``: the plain local
worker command path is the other real local-tier registration site besides
``Workspace`` -- ``cli.py``'s ``_run_worker(hosted=False)`` branch is the
process ``frisket serve`` auto-spawns and the one a bare
``frisket worker <workspace>`` invocation runs. It must register
``model.pull``; the ``hosted=True`` branch (``register_hosted_handlers``,
an external managed edition) must never see this call at all.
"""

from __future__ import annotations

import frisket.engine.jobs.model_pull as model_pull_module
from frisket.cli import _run_worker


def test_local_worker_command_registers_model_pull(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []
    real = model_pull_module.register_model_pull_handler

    def spy(registry, *, workspace_root, queue, **kwargs):
        calls.append({"workspace_root": workspace_root, "queue": queue})
        return real(registry, workspace_root=workspace_root, queue=queue, **kwargs)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", spy)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")

    workspace = tmp_path / "ws"
    rc = _run_worker(["--drain", str(workspace)], hosted=False)

    assert rc == 0
    assert len(calls) == 1


def test_hosted_worker_command_never_calls_model_pull_registration(
    tmp_path, monkeypatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        model_pull_module,
        "register_model_pull_handler",
        lambda *a, **k: calls.append({"args": a, "kwargs": k}),
    )

    # The hosted branch requires FRISKET_RUN_QUEUE_DATABASE_URL / an external
    # managed edition to get past registration at all; without either it fails
    # fast (RuntimeError: edition not installed) before reaching the
    # worker loop -- which is exactly the assertion: model.pull registration
    # is never attempted on this path, regardless of how far it gets.
    try:
        _run_worker(["--database-url", "postgresql://nope/db"], hosted=True)
    except Exception:
        pass

    assert calls == []
