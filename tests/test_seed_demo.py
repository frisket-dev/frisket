from __future__ import annotations

import json

import httpx
import pytest

from frisket.actions.registry import NEW_ACTION_IDS
from frisket.actions.system import validate_root_action
from scripts.e2e import seed_demo


def _classify_spec() -> dict:
    return seed_demo.typed_action_request(
        "map.classify",
        sheet_id=3,
        params={
            "source": ["snippet"],
            "engine": "llm",
            "model": "gemini/gemini-2.5-flash",
            "fields": [{"name": "beat", "type": "category", "labels": ["city"]}],
        },
        output_names={"beat": "beat"},
    )


def _run_status(
    status: str,
    *,
    cost: float | None = 0.125,
    failed: int = 0,
    live: bool = False,
    **context,
) -> dict:
    return {
        "run": {
            "public_status": {
                "status": status,
                "completed": 8,
                "failed": failed,
                "total": 8,
                "cost": cost,
                "live": live,
                **context,
            }
        }
    }


def _status_client(statuses: list[dict]) -> tuple[httpx.Client, list[str]]:
    paths: list[str] = []
    responses = iter(statuses)

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=next(responses))

    return (
        httpx.Client(
            base_url="http://frisket.test", transport=httpx.MockTransport(handle)
        ),
        paths,
    )


def test_wait_run_reports_numeric_cost(capsys):
    client, paths = _status_client([_run_status("completed", cost=0.125)])

    with client:
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    assert capsys.readouterr().out == "  classify stories: completed (8/8, $0.1250)\n"
    assert paths == ["/api/projects/project-1/actions/runs/41/status"]


def test_wait_run_reports_unknown_cost(capsys):
    client, _paths = _status_client([_run_status("completed", cost=None)])

    with client:
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    assert (
        capsys.readouterr().out == "  classify stories: completed (8/8, cost unknown)\n"
    )


def test_wait_run_rejects_completed_run_with_failed_rows():
    client, _paths = _status_client(
        [_run_status("completed", cost=None, failed=8, error="all rows failed")]
    )

    with (
        client,
        pytest.raises(RuntimeError) as raised,
    ):
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    message = str(raised.value)
    assert "ended unsuccessfully: completed (8/8, cost unknown)" in message
    assert "failed_rows=8" in message
    assert "error=all rows failed" in message


@pytest.mark.parametrize(
    "status",
    ["partial", "failed", "cancelled", "stalled", "orphaned", "needs_user_action"],
)
def test_wait_run_rejects_every_unsuccessful_terminal_status(status, monkeypatch):
    client, _paths = _status_client(
        [
            _run_status(
                status,
                error="provider failed",
                stalled_reason="lease_expired",
                queue={"error": "worker exited"},
            )
        ]
    )
    monkeypatch.setattr(
        seed_demo.time,
        "sleep",
        lambda _seconds: pytest.fail("terminal status must not be polled again"),
    )

    with (
        client,
        pytest.raises(RuntimeError) as raised,
    ):
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    message = str(raised.value)
    assert f"run 41 ended unsuccessfully: {status} (8/8, $0.1250)" in message
    assert "error=provider failed" in message
    assert "stalled_reason=lease_expired" in message
    assert "queue_error=worker exited" in message


def test_wait_run_keeps_polling_queued_running_and_live_statuses(monkeypatch, capsys):
    client, paths = _status_client(
        [
            _run_status("queued"),
            _run_status("running", live=True),
            _run_status("completed", live=True),
            _run_status("completed", cost=None),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(seed_demo.time, "sleep", sleeps.append)

    with client:
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    assert sleeps == [1, 1, 1]
    assert len(paths) == 4
    assert "cost unknown" in capsys.readouterr().out


def test_wait_run_times_out_after_600_polls(monkeypatch):
    polls = 0
    sleeps: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        return httpx.Response(200, json=_run_status("queued"))

    monkeypatch.setattr(seed_demo.time, "sleep", sleeps.append)
    with (
        httpx.Client(
            base_url="http://frisket.test", transport=httpx.MockTransport(handle)
        ) as client,
        pytest.raises(TimeoutError, match="classify stories"),
    ):
        seed_demo.wait_run(client, "project-1", 41, "classify stories")

    assert polls == 600
    assert sleeps == [1] * 600


def test_run_echoes_exact_confirmation_hash_with_same_action_identity(monkeypatch):
    requests: list[dict] = []
    quote = "server-authored-promise-set-hash"

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                402,
                json={
                    "status": "needs_confirmation",
                    "errors": [{"details": {"promise_set_hash": quote}}],
                },
            )
        return httpx.Response(200, json={"run_id": 41})

    waited: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        seed_demo,
        "wait_run",
        lambda _client, pid, run_id, label: waited.append((pid, run_id, label)),
    )
    original = _classify_spec()
    assert validate_root_action(original).ok, validate_root_action(original).error
    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed_demo.run(client, "project-1", original, "classify stories")

    assert len(requests) == 2
    challenge, retry = requests
    assert challenge["idempotency_key"] == retry["idempotency_key"]
    assert challenge["action_id"] == retry["action_id"] == "map.classify"
    # Consent is the exact server-authored token echoed at the top level;
    # it never lands inside params and the challenge carries none.
    assert challenge == original
    assert "confirmation" not in challenge
    assert "confirmed" not in challenge["params"]
    assert retry == {**challenge, "confirmation": quote}
    assert waited == [("project-1", 41, "classify stories")]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "failed", "errors": []},
        {"status": "needs_confirmation", "errors": []},
        {"status": "needs_confirmation", "errors": [{"details": {}}]},
        {
            "status": "needs_confirmation",
            "errors": [{"details": {"promise_set_hash": ""}}],
        },
    ],
)
def test_run_refuses_malformed_confirmation_envelope(body):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(402, json=body)

    with (
        httpx.Client(
            base_url="http://frisket.test", transport=httpx.MockTransport(handle)
        ) as client,
        pytest.raises(RuntimeError, match="malformed HTTP 402 needs_confirmation"),
    ):
        seed_demo.run(client, "project-1", _classify_spec(), "classify stories")

    assert len(requests) == 1


def test_run_surfaces_unexpected_non_confirmation_http_failure():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    with (
        httpx.Client(
            base_url="http://frisket.test", transport=httpx.MockTransport(handle)
        ) as client,
        pytest.raises(httpx.HTTPStatusError, match="503 Service Unavailable"),
    ):
        seed_demo.run(client, "project-1", _classify_spec(), "classify stories")

    assert len(requests) == 1


def test_run_accepts_completed_action_without_a_run(monkeypatch, capsys):
    waited = False

    def fail_wait(*_args, **_kwargs):
        nonlocal waited
        waited = True

    monkeypatch.setattr(seed_demo, "wait_run", fail_wait)
    with httpx.Client(
        base_url="http://frisket.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"status": "completed", "run_id": None},
            )
        ),
    ) as client:
        run_id = seed_demo.run(
            client,
            "project-1",
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "sheet_name": "Rows",
                "idempotency_key": "seed-demo-derive",
                "params": {
                    "source": {"kind": "column", "sheet_id": 1, "column_id": 2},
                },
            },
            "derive rows",
        )

    assert run_id is None
    assert waited is False
    assert capsys.readouterr().out == "  derive rows: completed\n"


def test_seed_audio_imports_packaged_council_fixture_without_network(
    monkeypatch, capsys
):
    sample = seed_demo.COUNCIL_AUDIO_SAMPLE
    expected_audio = seed_demo.council_audio_bytes(sample.filename)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/projects":
            assert json.loads(request.content) == {
                "name": seed_demo.COUNCIL_AUDIO_PROJECT_NAME
            }
            return httpx.Response(200, json={"id": "council-project"})
        assert request.url.path == "/api/projects/council-project/import/files"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert sample.filename.encode() in request.content
        assert expected_audio in request.content
        return httpx.Response(200, json={"sheet_id": 17})

    runs: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        seed_demo.httpx,
        "get",
        lambda *_args, **_kwargs: pytest.fail("seed_audio must not fetch media"),
    )
    monkeypatch.setattr(
        seed_demo,
        "run",
        lambda _client, pid, spec, label: runs.append((pid, spec, label)),
    )

    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed_demo.seed_audio(client)

    assert [request.url.path for request in requests] == [
        "/api/projects",
        "/api/projects/council-project/import/files",
    ]
    assert runs == [
        (
            "council-project",
            seed_demo.typed_action_request(
                "media.transcribe",
                sheet_id=17,
                params={
                    "source": "media",
                    "engine": "faster_whisper",
                    "model_size": "tiny",
                },
                output_names={
                    "text": "transcript",
                    "segments": "transcript_segments",
                    "detected_language": "detected_language",
                },
            ),
            "transcribe",
        ),
        (
            "council-project",
            seed_demo.typed_action_request(
                "map.summarize",
                sheet_id=17,
                params={
                    "model": seed_demo.MODEL,
                    "source": ["transcript"],
                    "preset": "one_line",
                },
                output_names={"summary": "one_line_summary"},
            ),
            "summarize transcript",
        ),
    ]
    assert capsys.readouterr().out == "✓ Council audio → council-project\n"


def test_seed_faces_chains_canonical_extract_and_derive_actions(monkeypatch):
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/projects":
            return httpx.Response(200, json={"id": "faces-project"})
        if request.url.path == "/api/projects/faces-project/import/files":
            return httpx.Response(200, json={"sheet_id": 17})
        assert request.url.path == "/api/projects/faces-project/sheets/17/data"
        assert dict(request.url.params) == {"offset": "0", "limit": "0"}
        return httpx.Response(200, json={"columns": [{"id": 23, "name": "faces"}]})

    runs: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        seed_demo.httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(200, content=b"jpeg"),
    )
    monkeypatch.setattr(
        seed_demo,
        "run",
        lambda _client, pid, spec, label: runs.append((pid, spec, label)),
    )

    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed_demo.seed_faces(client)

    assert paths == [
        "/api/projects",
        "/api/projects/faces-project/import/files",
        "/api/projects/faces-project/sheets/17/data",
    ]
    assert [(spec["action_id"], label) for _pid, spec, label in runs] == [
        ("media.extract_faces", "extract faces"),
        ("derive.table_from_list", "derive Faces sheet"),
    ]
    assert runs[0][1]["scope"] == {"kind": "sheet_rows", "sheet_id": 17}
    assert runs[0][1]["params"] == {"source": "media"}
    assert runs[0][1]["output_names"] == {"faces": "faces"}
    assert runs[1][1]["scope"] == {"kind": "project"}
    assert runs[1][1]["sheet_name"] == "Faces"
    assert runs[1][1]["params"]["source"] == {
        "kind": "column",
        "sheet_id": 17,
        "column_id": 23,
    }


@pytest.mark.parametrize(
    ("seed", "expected_actions"),
    [
        (seed_demo.seed_stories, ["map.classify"]),
        (
            seed_demo.seed_tariff,
            ["research.web_search", "map.summarize", "map.classify"],
        ),
        (seed_demo.seed_audio, ["media.transcribe", "map.summarize"]),
        (seed_demo.seed_people_derive, ["import.rows", "derive.table_from_list"]),
        (
            seed_demo.seed_feature_tour,
            [
                "map.regex_extract",
                "map.python",
                "map.classify",
                "map.judge",
                "reduce.group_summary",
            ],
        ),
        (seed_demo.seed_faces, ["media.extract_faces", "derive.table_from_list"]),
        (seed_demo.seed_agent, ["research.answer"]),
    ],
)
def test_demo_emitted_actions_pass_real_root_validation(
    seed, expected_actions, monkeypatch
):
    """Exercise builders and their actual wire boundary without provider effects."""
    emitted = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/projects":
            return httpx.Response(200, json={"id": "demo"})
        if path.endswith(("/import/csv", "/import/files")):
            return httpx.Response(200, json={"sheet_id": 17})
        if path.endswith("/sheets"):
            return httpx.Response(200, json=[{"id": 17, "name": "stories"}])
        if path.endswith("/data"):
            return httpx.Response(
                200,
                json={
                    "columns": [
                        {"id": 23, "name": "people"},
                        {"id": 24, "name": "faces"},
                    ]
                },
            )
        assert path == "/api/projects/demo/actions/v1/run"
        payload = json.loads(request.content)
        action_id = payload.get("action_id", payload.get("kind"))
        validated = validate_root_action(payload)
        assert validated.ok, (action_id, validated.error)
        assert ("action_id" in payload) == (action_id in NEW_ACTION_IDS)
        emitted.append(action_id)
        return httpx.Response(200, json={"status": "completed", "run_id": None})

    monkeypatch.setattr(
        seed_demo.httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(200, content=b"portrait-fixture"),
    )
    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed(client)
    assert emitted == expected_actions


def test_typed_run_echoes_confirmation_without_changing_request():
    original = seed_demo.typed_action_request(
        "map.summarize",
        sheet_id=17,
        params={"source": ["transcript"], "model": seed_demo.MODEL},
        output_names={"summary": "summary"},
    )
    requests = []
    quote = "server-authored-promise-set-hash"

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        validated = validate_root_action(payload)
        assert validated.ok, validated.error
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                402,
                json={
                    "status": "needs_confirmation",
                    "errors": [{"details": {"promise_set_hash": quote}}],
                },
            )
        return httpx.Response(200, json={"status": "completed"})

    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed_demo.run(client, "demo", original, "summarize")
    assert requests == [original, {**original, "confirmation": quote}]
    assert "confirmation" not in original


def test_typed_output_placement_participates_in_demo_idempotency_key():
    def request(name):
        return seed_demo.typed_action_request(
            "map.summarize",
            sheet_id=17,
            params={"source": ["transcript"], "model": seed_demo.MODEL},
            output_names={"summary": name},
        )

    assert request("summary") == request("summary")
    assert request("summary")["idempotency_key"] != request("other")["idempotency_key"]
