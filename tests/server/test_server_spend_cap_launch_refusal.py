"""The spend cap refuses at the HTTP seam, on the DEFAULT router.

Every unit test of the cap built its own `ModelRouter(cache_mode="off")`,
which is why all of them passed while the shipped app queued and billed an
over-cap run for a month: the real `create_app()` router is plain 'replay'
with a cache, and the launch check used to sit inside a guard that exempts
exactly that. A fence with no test on the real composition is a fence
nobody is standing behind.

So this file constructs the app the way the product does — no router
override — and asserts the whole path: 400, a typed error naming the cap
and the accrued amount, and NO run row created.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.engine.store.runs import RunResultStore
from frisket.server.app import create_app
from frisket.team.security.secrets import encrypt_secret, key_hint
from http_test_helpers import post_v1_action_with_exact_confirmation
from helpers import write_claimless_test_model_calls

KEY = "sk-test-not-a-real-key"


def _client(tmp_path: Path) -> TestClient:
    # No `router=` override: this is the local-tier default composition.
    return TestClient(create_app(tmp_path / "workspace"))


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Cap seam"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "rows",
            "params": {
                "columns": [{"name": "text", "type": "text"}],
                "rows": [{"text": "Maria Gonzalez was hired by Acme Corp"}],
                "source": {
                    "kind": "inline",
                    "label": "seed",
                    "fingerprint": "sha256:seed",
                },
            },
            "idempotency_key": "seed@sha256:1",
        },
    )
    assert response.status_code == 200, response.text
    project = client.app.state.workspace.get(pid)
    sheet_id = int(project.db.execute("SELECT id FROM sheets").fetchone()["id"])
    return pid, sheet_id


def _capped_key(client: TestClient, pid: str, *, cap_micro: int) -> None:
    project = client.app.state.workspace.get(pid)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret(KEY),
        hint=key_hint(KEY),
        spend_cap_micro=cap_micro,
    )


def _accrue(client: TestClient, pid: str, *, cost_usd: float | None) -> None:
    """Spend through the REAL accrual path, not by writing the counter."""
    project = client.app.state.workspace.get(pid)
    sheet_id = int(project.db.execute("SELECT id FROM sheets").fetchone()["id"])
    op_id = project.append_op("map", {"recipe": "accrual_seed"}, label="accrual")
    run_id = RunResultStore(project).start_run(op_id, sheet_id, "test.accrual_seed")
    write_claimless_test_model_calls(
        project,
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.complete",
                        "engine": "openai/gpt-5-mini",
                        "provider": "openai",
                        "provider_kind": "chat_api",
                        "credential_source": "project_key",
                        "provider_reported_cost_usd": cost_usd,
                        "provider_cost_usd": cost_usd,
                        "cost_source": (
                            "pricing_data" if cost_usd is not None else "unknown"
                        ),
                        "units": {"tokens_in": 257, "tokens_out": 35},
                    }
                ],
            }
        ],
    )
    project.db.commit()


def _launch(client: TestClient, pid: str, sheet_id: int):
    return post_v1_action_with_exact_confirmation(
        client,
        pid,
        # A typed paid request: the helper answers the 402 with the exact
        # top-level `confirmation` echo, which is the only consent there is.
        {
            "action_id": "map.extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["text"],
                "model": "openai/gpt-5-mini",
                "context": "extract",
                "fields": [
                    {"name": "person", "type": "text", "description": "person name"}
                ],
            },
            "output_names": {},
            "idempotency_key": "extract@sha256:1",
        },
    )


def _extract_runs(client: TestClient, pid: str) -> int:
    project = client.app.state.workspace.get(pid)
    return int(
        project.db.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE action_kind='map.extract'"
        ).fetchone()["c"]
    )


def test_over_cap_launch_is_refused_and_queues_nothing(tmp_path: Path) -> None:
    """Live 2026-07-26 before this fix: 200 "queued", a run row, real money.

    The one assertion that would have caught it is the run count.
    """
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    _capped_key(client, pid, cap_micro=1_000_000)  # $1
    _accrue(client, pid, cost_usd=2.0)  # $2 spent

    response = _launch(client, pid, sheet_id)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["run_id"] is None
    error = body["errors"][0]
    assert error["code"] == "provider_spend_cap_exceeded"
    assert error["details"]["cap_usd"] == 1.0
    assert error["details"]["spent_usd"] == 2.0
    assert error["details"]["setting"] == "spend_cap_usd"
    assert "$2.00" in error["message"] and "$1.00" in error["message"]
    assert _extract_runs(client, pid) == 0


def test_unenforceable_cap_launch_is_refused_and_queues_nothing(
    tmp_path: Path,
) -> None:
    """A capped key with a call of undeterminable price: "under the cap" is a
    guess, so the launch refuses rather than spending on a bound it cannot
    check."""
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    _capped_key(client, pid, cap_micro=25_000_000)  # $25, nowhere near spent
    _accrue(client, pid, cost_usd=None)

    response = _launch(client, pid, sheet_id)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    error = body["errors"][0]
    assert error["code"] == "provider_spend_cap_unenforceable"
    assert error["details"]["unmetered_calls"] == 1
    assert error["details"]["setting"] == "spend_cap_usd"
    assert _extract_runs(client, pid) == 0


def test_under_cap_launch_still_queues(tmp_path: Path) -> None:
    """The refusal is a bound, not a brake: a key under its cap launches."""
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    _capped_key(client, pid, cap_micro=25_000_000)
    _accrue(client, pid, cost_usd=2.0)

    response = _launch(client, pid, sheet_id)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert _extract_runs(client, pid) == 1


def test_editing_the_cap_through_the_store_keeps_the_refusal_armed(
    tmp_path: Path,
) -> None:
    """Live 2026-07-26: lowering the cap zeroed the accrual, so the tightened
    cap read $0.00 spent and refused nothing. End to end, the tightening now
    bites on the very next launch."""
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    _capped_key(client, pid, cap_micro=25_000_000)
    _accrue(client, pid, cost_usd=2.0)

    _capped_key(client, pid, cap_micro=1_000_000)  # tighten $25 -> $1

    response = _launch(client, pid, sheet_id)

    assert response.status_code == 400, response.text
    assert response.json()["errors"][0]["code"] == "provider_spend_cap_exceeded"
    assert _extract_runs(client, pid) == 0
