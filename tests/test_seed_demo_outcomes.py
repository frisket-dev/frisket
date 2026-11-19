"""seed_demo.py builds each demo project independently and reports each one."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from scripts.e2e import seed_demo

ALL_PROJECTS = ["Local stories", "Council audio", "Tariff impacts", "People mentioned"]


class FakeServer:
    """A frisket API double: projects, imports and runs that finish at once.

    A run of ``failing_action`` in ``failing_project`` ends with one failed
    row, the way the server records a row failure; creating a project named in
    ``unbuildable`` answers HTTP 500.
    """

    def __init__(
        self,
        *,
        failing: tuple[str, str] | None = None,
        unbuildable: frozenset[str] = frozenset(),
    ) -> None:
        self.failing = failing
        self.unbuildable = unbuildable
        self.projects: dict[str, str] = {}
        self.runs: dict[int, tuple[str, str]] = {}
        self.actions: list[tuple[str, str]] = []

    def _name(self, pid: str) -> str:
        return next(name for name, known in self.projects.items() if known == pid)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/projects" and request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": pid, "name": name} for name, pid in self.projects.items()],
            )
        if path == "/api/projects":
            name = json.loads(request.content)["name"]
            if name in self.unbuildable:
                return httpx.Response(500, json={"detail": "disk full"})
            self.projects[name] = f"p{len(self.projects) + 1}"
            return httpx.Response(200, json={"id": self.projects[name]})
        pid = path.split("/")[3]
        if path.endswith(("/import/csv", "/import/files")):
            return httpx.Response(200, json={"sheet_id": 1})
        if path.endswith("/model-routes"):
            return httpx.Response(200, json={})
        if path.endswith("/sheets"):
            return httpx.Response(
                200, json=[{"id": 1, "name": "stories"}, {"id": 1, "name": "countries"}]
            )
        if path.endswith("/data"):
            return httpx.Response(200, json={"columns": [{"id": 2, "name": "people"}]})
        if path.endswith("/actions/v1/run"):
            action_id = json.loads(request.content)["action_id"]
            self.actions.append((self._name(pid), action_id))
            run_id = len(self.runs) + 1
            self.runs[run_id] = (self._name(pid), action_id)
            return httpx.Response(200, json={"run_id": run_id})
        assert path.endswith("/status"), path
        run_id = int(path.split("/")[-2])
        failed = 1 if self.runs[run_id] == self.failing else 0
        return httpx.Response(
            200,
            json={
                "run": {
                    "public_status": {
                        "status": "completed",
                        "completed": 8 - failed,
                        "failed": failed,
                        "total": 8,
                        "cost": 0.0,
                        "live": False,
                    }
                }
            },
        )


def _seed(
    monkeypatch, tmp_path: Path, server: FakeServer, *flags: str
) -> tuple[int, dict]:
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(server.handle), **kwargs)

    monkeypatch.setattr(seed_demo.httpx, "Client", client)
    monkeypatch.setattr(seed_demo, "prime_replies", lambda *_args: None)
    status_file = tmp_path / "seed-status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_demo.py",
            "http://frisket.test",
            "--status-file",
            str(status_file),
            *flags,
        ],
    )
    try:
        seed_demo.main()
        code = 0
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    return code, json.loads(status_file.read_text())


def test_every_project_builds_and_is_recorded_complete(monkeypatch, tmp_path, capsys):
    server = FakeServer()

    code, status = _seed(monkeypatch, tmp_path, server)

    assert code == 0
    assert status == {
        name: {"state": "complete", "detail": None} for name in ALL_PROJECTS
    }
    assert set(server.projects) == set(ALL_PROJECTS)
    assert (
        "complete: Local stories, Council audio, Tariff impacts, People mentioned"
        in (capsys.readouterr().out)
    )


def test_a_failed_row_leaves_its_project_partial_and_the_rest_built(
    monkeypatch, tmp_path, capsys
):
    server = FakeServer(failing=("Local stories", "map.classify"))

    code, status = _seed(monkeypatch, tmp_path, server)

    assert code == 0
    assert status["Local stories"]["state"] == "partial"
    assert "failed_rows=1" in status["Local stories"]["detail"]
    for name in ["Council audio", "Tariff impacts", "People mentioned"]:
        assert status[name] == {"state": "complete", "detail": None}
    # The run that recorded the row failure is the server's; the seed starts
    # it once and does not retry or hide it.
    assert server.actions.count(("Local stories", "map.classify")) == 1
    out = capsys.readouterr().out
    assert "partial: Local stories" in out


def test_a_project_that_cannot_be_built_does_not_stop_the_others(
    monkeypatch, tmp_path, capsys
):
    server = FakeServer(unbuildable=frozenset({"Council audio"}))

    code, status = _seed(monkeypatch, tmp_path, server)

    assert code == 0
    assert status["Council audio"]["state"] == "missing"
    assert "500" in status["Council audio"]["detail"]
    for name in ["Local stories", "Tariff impacts", "People mentioned"]:
        assert status[name] == {"state": "complete", "detail": None}
    assert set(server.projects) == {
        "Local stories",
        "Tariff impacts",
        "People mentioned",
    }
    assert "missing: Council audio" in capsys.readouterr().out


@pytest.mark.parametrize(
    "server",
    [
        FakeServer(failing=("Tariff impacts", "map.summarize")),
        FakeServer(unbuildable=frozenset({"People mentioned"})),
    ],
)
def test_strict_exits_nonzero_unless_every_project_is_complete(
    monkeypatch, tmp_path, server
):
    code, status = _seed(monkeypatch, tmp_path, server, "--strict")

    assert code == 1
    assert sum(entry["state"] == "complete" for entry in status.values()) == 3


def test_strict_exits_zero_when_every_project_is_complete(monkeypatch, tmp_path):
    code, _status = _seed(monkeypatch, tmp_path, FakeServer(), "--strict")

    assert code == 0


def test_a_project_the_server_already_holds_is_recorded_present(monkeypatch, tmp_path):
    server = FakeServer()
    server.projects["Local stories"] = "existing"

    code, status = _seed(monkeypatch, tmp_path, server)

    assert code == 0
    assert status["Local stories"] == {"state": "present", "detail": None}
    assert ("Local stories", "map.classify") not in server.actions
