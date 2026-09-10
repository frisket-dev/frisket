"""Durable proof of the complete app→gateway→worker→committed-transcript path
for the ``moss`` product engine.

Marked integration tests drive the REAL durable machinery end to end,
entirely in process and without GPU or network:

- the app enqueues a ``media.transcribe`` action run with engine ``moss``
  through the real HTTP action route (queued_project_run placement);
- the real job worker (``register_project_run_handler`` + ``Worker``) claims
  the job from the workspace queue and opens the claimed project;
- ``TranscriptionV1Adapter`` crosses Boundary A into the REAL
  frisket_models sidecar app (``create_app``, authenticated ``/v1/transcribe``)
  via an ASGI transport bound to ``FRISKET_MODELS_URL``;
- the sidecar's ``TranscriptionWorkerGateway`` — configured through the
  DOCUMENTED deployment path (``worker_registry_from_env`` over the
  ``FRISKET_TRANSCRIPTION_MOSS_WORKER_*`` env family) — crosses Boundary B
  into the REAL versioned worker ASGI app (``create_worker_app``) wrapping a
  contract-stub MOSS adapter: the ONLY stubbed seam is the GPU inference
  inside the adapter; every contract validation on both boundaries is real;
- the MapRunner/coordinator COMMITS the validated transcript to the project
  store, and the run's model-call facts carry the sidecar provenance
  (engine ``moss``, pinned model id + revision, device/dtype/timings).

The MOSS output contract is the REAL one (sidecar/workers/moss/README.md):
speaker-labeled segments, NO word timestamps ("``words`` stays absent rather
than fabricated"), and ``result.language`` null (MOSS auto-detects but does
not report). The happy path proves absence is preserved as absence all the
way into the committed store. Two failure-path tests pin the durable terminal
behavior for a dead worker and for a declaration-violating adapter result.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import io
import json
import math
import struct
import sys
import wave
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from frisket.server.app import create_app
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage

_SIDECAR_SRC = Path(__file__).resolve().parents[2] / "sidecar" / "src"
if str(_SIDECAR_SRC) not in sys.path:  # the root conftest normally adds it
    sys.path.insert(0, str(_SIDECAR_SRC))

from frisket_models.app import create_app as create_sidecar_app  # noqa: E402
from frisket_models.engines import Registry as SidecarRegistry  # noqa: E402
from frisket_models.transcription.config import (  # noqa: E402
    MOSS_DEFINITION,
    worker_registry_from_env,
)
from frisket_models.transcription.contract import (  # noqa: E402
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
)
from frisket_models.transcription.gateway import (  # noqa: E402
    TranscriptionWorkerGateway,
    WorkerEndpoint,
)
from frisket_models.transcription.moss import (  # noqa: E402
    MOSS_DESCRIPTOR,
    MOSS_ENGINE,
    MOSS_MODEL_ID,
    MOSS_MODEL_REVISION,
)
from frisket_models.transcription.worker import create_worker_app  # noqa: E402

pytestmark = pytest.mark.integration

PUBLIC_TOKEN = "public-sidecar-secret"
WORKER_TOKEN = "internal-worker-secret"
RUNTIME_IMAGE_ID = "oci:sha256:" + ("a" * 64)


def _wav_bytes(seconds: float = 0.3, freq: float = 220.0) -> bytes:
    sr = 16000
    n = int(sr * seconds)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return buf.getvalue()


# --- sidecar/worker composition helpers -------------------------------------


def _moss_worker_registry():
    """Build the moss endpoint through the DOCUMENTED config path.

    ``worker_registry_from_env`` + the ``FRISKET_TRANSCRIPTION_MOSS_WORKER_*``
    family is how a real deployment routes moss; the descriptor stays
    code-owned (``MOSS_DEFINITION``) with ``runtime_image_id=None`` — the
    worker's live ``/capabilities`` reports its runtime image id and the gateway's
    ``_descriptor_matches`` adopts exactly that one unknown field, nothing
    else. The gateway still refuses to run inference against a worker whose
    live capabilities carry NO digest (provenance required), which is why the
    worker app below is created with a ``runtime_image_id``.
    """

    return worker_registry_from_env(
        definitions=(MOSS_DEFINITION,),
        environ={
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": "http://worker.internal",
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_TOKEN": WORKER_TOKEN,
        },
    )


def _moss_worker_asgi_factory(tmp_path: Path, adapter_cls):
    """A real create_worker_app around ``adapter_cls``, bridged over ASGI."""

    worker_spool = tmp_path / "worker-spool"
    worker_spool.mkdir()
    worker_app = create_worker_app(
        AdapterRegistration(
            descriptor=MOSS_DESCRIPTOR,
            factory=adapter_cls,
            probe=lambda: EngineProbe(available=True, loaded=False, error=None),
        ),
        token=WORKER_TOKEN,
        concurrency=1,
        spool_dir=worker_spool,
        runtime_image_id=RUNTIME_IMAGE_ID,
    )

    def factory(_endpoint: WorkerEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker_app),
            base_url="http://worker.internal",
        )

    return factory, worker_spool


def _moss_sidecar_app(tmp_path: Path, worker_client_factory):
    gateway_spool = tmp_path / "gateway-spool"
    gateway_spool.mkdir()
    app = create_sidecar_app(
        token=PUBLIC_TOKEN,
        registry=SidecarRegistry([]),
        concurrency=1,
        transcription_gateway=TranscriptionWorkerGateway(
            _moss_worker_registry(),
            client_factory=worker_client_factory,
        ),
        transcription_spool_dir=gateway_spool,
    )
    return app, gateway_spool


class _SidecarAsgiRouter(ModelRouter):
    """A real ModelRouter whose http client resolves to the in-process sidecar.

    ``MapRunner`` hands ``router.client`` to every recipe as ``OpContext.http``;
    binding it to an ``ASGITransport`` makes ``sidecar_post``'s POST against
    ``FRISKET_MODELS_URL`` land on the real frisket_models FastAPI app without
    a socket. The base ModelRouter's loop-bound cached client is replaced with
    a per-access client because the durable handler runs each job in a fresh
    ``asyncio.run`` loop.
    """

    def __init__(self, sidecar_asgi_app) -> None:
        super().__init__(cache=None, cache_mode="off", use_env_keys=False)
        self._sidecar_asgi_app = sidecar_asgi_app

    @property
    def client(self) -> httpx.AsyncClient:  # type: ignore[override]
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._sidecar_asgi_app),
            base_url="http://models.test",
        )


# --- app-side helpers -------------------------------------------------------


def _seeded_app_with_audio_row(tmp_path: Path):
    client = TestClient(
        create_app(
            tmp_path / "ws",
            executor_deps_factory=lambda project_id, _request: ExecutorDeps(
                consent_coverage=ConsentCoverage(
                    instance_principal(client.app.state.workspace.get(project_id)),
                    Decimal("0"),
                )
            ),
        )
    )
    project_id = client.post(
        "/api/projects", json={"name": "MOSS durable proof"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    audio = _wav_bytes()
    blob = project.add_blob(
        audio,
        filename="ep1.wav",
        mime="audio/wav",
        source_url="https://cdn.example/ep1.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.4, "kind": "audio"}
        ),
    )
    [row_id] = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(
                    blob,
                    mime="audio/wav",
                    filename="ep1.wav",
                ),
            }
        ],
        cols,
    )
    return client, project_id, project, sheet_id, row_id, audio


def _enqueue_moss_transcribe(client, project_id: str, sheet_id: int, *, key: str):
    action = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            # moss is auto_only + intrinsic diarization: language and every
            # diarization knob are engine facts, not request options, and the
            # roster validation rejects them if supplied.
            "engine": "moss",
        },
        "idempotency_key": key,
    }
    # Known-free MOSS fits even zero preapproval, including operator_lan
    # egress. The unconfirmed launch queues with durable exact consent.
    queued = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action,
    )
    assert queued.status_code == 200, queued.text
    launched = queued.json()
    assert launched["status"] == "queued"
    assert launched["receipt_id"]
    return launched["receipt_id"]


def _drain_one_job(client, sidecar_app, *, worker_id: str) -> bool:
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=_SidecarAsgiRouter(sidecar_app),
    )
    worker = Worker(client.app.state.workspace.queue, registry, worker_id=worker_id)
    return worker.run_once()


def _columns(project, sheet_id: int) -> dict:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def _latest_run(project):
    return project.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def _receipt(project, receipt_id: str) -> dict:
    row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


# --- the durable happy path -------------------------------------------------


def test_moss_durable_run_commits_transcript_through_real_gateway_and_worker(
    tmp_path, monkeypatch
) -> None:
    adapter_calls: list[dict] = []

    class _MossContractStubAdapter:
        """Registered MOSS provenance and the REAL MOSS output contract.

        Per sidecar/workers/moss/README.md: speaker-labeled segments, NO word
        timestamps ("words stays absent rather than fabricated"), and no
        reported language (MOSS auto-detects but does not report it). Only
        the GPU inference is stubbed.
        """

        def transcribe(
            self, audio_path: Path, options: TranscribeOptions
        ) -> TranscribeResult:
            if not audio_path.is_file():
                raise RuntimeError("worker did not spool the uploaded audio")
            adapter_calls.append(
                {"bytes": audio_path.stat().st_size, "options": options}
            )
            segments = [
                TranscribeSegment(
                    start=0.0,
                    end=1.2,
                    text="hello there",
                    speaker="SPEAKER_00",
                    speaker_confidence="approximate",
                ),
                TranscribeSegment(
                    start=1.3,
                    end=2.4,
                    text="general kenobi",
                    speaker="SPEAKER_01",
                ),
            ]
            return TranscribeResult(
                engine=MOSS_ENGINE,
                text="hello there general kenobi",
                segments=segments,
                language=None,
                duration=2.4,
                model_ids=[MOSS_MODEL_ID],
                revision=MOSS_MODEL_REVISION,
                device="cuda:0",
                dtype="bfloat16",
                timings={"inference_seconds": 0.05},
                warnings=[],
                accepted_options=options.supplied_options(),
            )

    worker_client, worker_spool = _moss_worker_asgi_factory(
        tmp_path, _MossContractStubAdapter
    )
    sidecar_app, gateway_spool = _moss_sidecar_app(tmp_path, worker_client)
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", PUBLIC_TOKEN)

    # --- the advertisement path the catalog relies on: GET /capabilities ----
    sidecar_http = TestClient(sidecar_app)
    caps = sidecar_http.get(
        "/capabilities", headers={"Authorization": f"Bearer {PUBLIC_TOKEN}"}
    )
    assert caps.status_code == 200, caps.text
    [moss_entry] = [e for e in caps.json()["engines"] if e["name"] == MOSS_ENGINE]
    assert moss_entry["available"] is True
    assert "frisket.transcription.v1" in moss_entry["contract_versions"]
    assert moss_entry["models"] == [MOSS_MODEL_ID]
    assert moss_entry["revision"] == MOSS_MODEL_REVISION
    # Scale-to-zero cataloging does not wake the worker. The strict inference
    # preflight below verifies the worker's actual runtime image id.
    assert moss_entry["runtime_image_id"] is None

    # --- app side: seed one audio row, enqueue over the real HTTP route -----
    client, project_id, project, sheet_id, row_id, audio = _seeded_app_with_audio_row(
        tmp_path
    )
    receipt_id = _enqueue_moss_transcribe(
        client, project_id, sheet_id, key="media_transcribe@sha256:moss-durable-v1"
    )

    # --- the real durable worker claims the job and runs the recipe ---------
    assert _drain_one_job(client, sidecar_app, worker_id="moss-durable-proof") is True

    # exactly one Boundary-B adapter dispatch, fed the spooled audio bytes,
    # with the app's canonical empty option set (nothing to declare for moss)
    assert len(adapter_calls) == 1
    assert adapter_calls[0]["bytes"] == len(audio)
    assert adapter_calls[0]["options"].supplied_options() == {}

    # --- committed transcript in the project store --------------------------
    columns = _columns(project, sheet_id)
    assert columns["transcript"]["type"] == "timestamped_transcript"
    assert columns["transcript_segments"]["type"] == "json"
    # moss does not report a detected language (detects_language=False):
    # emitting the column would fabricate provenance.
    assert "detected_language" not in columns

    transcript = project.get_values(
        sheet_id, int(columns["transcript"]["id"]), row_ids=[row_id]
    )[row_id]
    assert transcript == "hello there general kenobi"

    segments = project.get_values(
        sheet_id, int(columns["transcript_segments"]["id"]), row_ids=[row_id]
    )[row_id]
    assert segments == [
        {
            "start": 0.0,
            "end": 1.2,
            "text": "hello there",
            "speaker": "SPEAKER_00",
            "speaker_confidence": "approximate",
            "segment_index": 0,
        },
        {
            "start": 1.3,
            "end": 2.4,
            "text": "general kenobi",
            "speaker": "SPEAKER_01",
            "segment_index": 1,
        },
    ]
    # absence preserved as absence: MOSS emits no word timestamps and none may
    # be fabricated anywhere between the adapter and the committed store.
    assert all("words" not in segment for segment in segments)

    # --- run + receipt + model-call facts carry the sidecar provenance ------
    run_row = _latest_run(project)
    assert run_row is not None
    run_id = int(run_row["id"])
    assert run_row["status"] == "completed"
    assert int(columns["transcript"]["current_run_id"]) == run_id

    [model_call] = RunResultStore(project).model_calls(run_id)
    assert model_call["capability"] == "transcribe"
    assert model_call["engine"] == "moss"
    assert model_call["provider"] == "frisket-sidecar"
    assert model_call["provider_kind"] == "local_http"
    assert json.loads(model_call["model_ids"]) == [MOSS_MODEL_ID]
    # self-hosted compute is genuinely free (0.0), never "unknown"
    assert model_call["provider_cost_usd"] == 0.0
    assert model_call["cost_source"] == "free_local"
    units = json.loads(model_call["units"])
    assert units["model_revision"] == MOSS_MODEL_REVISION
    assert units["device"] == "cuda:0"
    assert units["dtype"] == "bfloat16"
    assert units["timing_inference_seconds"] == 0.05
    assert units["audio_seconds"] == 2.4
    assert units["input_bytes"] == len(audio)

    assert _receipt(project, receipt_id)["status"] == "completed"

    # both spool tiers cleaned up after the run
    assert list(gateway_spool.iterdir()) == []
    assert list(worker_spool.iterdir()) == []


# --- durable failure path: dead worker --------------------------------------


class _UnreachableTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("worker endpoint is unreachable", request=request)


def test_moss_durable_run_dead_worker_fails_honestly_and_commits_nothing(
    tmp_path, monkeypatch
) -> None:
    """Registry routes moss, but the worker endpoint is dead.

    The gateway's provenance preflight hits the unreachable worker and raises
    its retryable 502 ``worker_unreachable``. Pinned terminal behavior of the
    durable machinery: the JOB completes on its first attempt (a row-level
    provider failure is recorded data, not a worker crash, so the queue never
    retries); the RUN row is the mechanical record — status ``completed``
    with ``failed_rows == total_rows`` and per-column result rows carrying
    the structured gateway code; the RECEIPT is the verdict — ``failed`` with
    the action's map error code ``transcribe_run_failed``; the output
    columns exist only as hidden placeholders with ZERO committed values and
    no model-call facts; and the output-column claims go terminal
    (``failed``), never left stuck ``active``.
    """

    def dead_worker_client(_endpoint: WorkerEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_UnreachableTransport(), base_url="http://worker.internal"
        )

    sidecar_app, gateway_spool = _moss_sidecar_app(tmp_path, dead_worker_client)
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", PUBLIC_TOKEN)

    client, project_id, project, sheet_id, row_id, _audio = _seeded_app_with_audio_row(
        tmp_path
    )
    receipt_id = _enqueue_moss_transcribe(
        client, project_id, sheet_id, key="media_transcribe@sha256:moss-dead-worker-v1"
    )

    assert _drain_one_job(client, sidecar_app, worker_id="moss-dead-worker") is True

    # the run row is the mechanical record: it ran to the end, every row failed
    run_row = _latest_run(project)
    assert run_row is not None
    assert run_row["status"] == "completed"
    assert run_row["total_rows"] == 1
    assert run_row["failed_rows"] == 1
    run_id = int(run_row["id"])

    # per-output-column result rows carry the structured gateway code, no value
    result_rows = project.db.execute(
        "SELECT * FROM results WHERE run_id=? ORDER BY column_id", (run_id,)
    ).fetchall()
    assert len(result_rows) == 2  # transcript + transcript_segments
    for result_row in result_rows:
        assert result_row["row_id"] == row_id
        assert result_row["value"] is None
        assert result_row["error_code"] == "model_error"
        assert "worker_unreachable" in str(result_row["error"])

    # the receipt is the verdict: failed with the action's map error code
    receipt = _receipt(project, receipt_id)
    assert receipt["status"] == "failed"
    [error] = json.loads(receipt["body"])["errors"]
    assert error["code"] == "external_rows_failed"
    assert error["message"] == "media.transcribe failed for every target row"

    # nothing committed: the output columns exist only as hidden placeholders
    # with zero cell values, and no model-call facts were recorded
    columns = _columns(project, sheet_id)
    output_ids = tuple(
        int(columns[name]["id"]) for name in ("transcript", "transcript_segments")
    )
    for name in ("transcript", "transcript_segments"):
        assert columns[name]["hidden"] == 1
    committed = project.db.execute(
        "SELECT COUNT(*) FROM cells WHERE column_id IN (?, ?)", output_ids
    ).fetchone()[0]
    assert committed == 0
    assert RunResultStore(project).model_calls(run_id) == []

    # the output-column claims went terminal, never left stuck active
    claims = project.db.execute(
        "SELECT output_name, status FROM output_column_claims ORDER BY output_name"
    ).fetchall()
    # The single preapproved launch owns a pair of claims; both go terminal.
    assert [(c["output_name"], c["status"]) for c in claims] == [
        ("transcript", "failed"),
        ("transcript_segments", "failed"),
    ]

    # the job itself terminalized on its first attempt — the failed receipt
    # is the durable record; row failures are data, not queue crashes
    [job] = client.app.state.workspace.queue.list_project_jobs(project_id)
    assert job.status == "done"
    assert job.error is None

    assert list(gateway_spool.iterdir()) == []


# --- durable failure path: declaration-violating adapter result -------------


def test_moss_durable_run_refuses_declaration_violating_result(
    tmp_path, monkeypatch
) -> None:
    """The adapter violates the moss declaration; the violation is REFUSED.

    The stub returns segments WITHOUT speaker labels (moss diarization is
    intrinsic — every segment must be labeled) plus a fabricated reported
    language. The worker's own ``descriptor.validate_result`` refuses the
    result (500 invalid_adapter_result) before it ever leaves the worker; the
    gateway forwards only the bounded public code (``worker_failure`` — the
    worker's internal detail never crosses the boundary); app-side the row
    fails, the receipt terminalizes ``failed``, and NOTHING of the violating
    result — not the text, not the unlabeled segments, not the fabricated
    language — is committed.
    """

    adapter_calls: list[int] = []

    class _ViolatingAdapter:
        def transcribe(
            self, audio_path: Path, options: TranscribeOptions
        ) -> TranscribeResult:
            adapter_calls.append(audio_path.stat().st_size)
            return TranscribeResult(
                engine=MOSS_ENGINE,
                text="unlabeled speech",
                # intrinsic-diarization violation: no speaker labels
                segments=[
                    TranscribeSegment(start=0.0, end=2.4, text="unlabeled speech")
                ],
                # fabricated detected language for an engine that never
                # reports one
                language="en",
                duration=2.4,
                model_ids=[MOSS_MODEL_ID],
                revision=MOSS_MODEL_REVISION,
                device="cuda:0",
                dtype="bfloat16",
                timings={"inference_seconds": 0.05},
                warnings=[],
                accepted_options=options.supplied_options(),
            )

    worker_client, worker_spool = _moss_worker_asgi_factory(tmp_path, _ViolatingAdapter)
    sidecar_app, gateway_spool = _moss_sidecar_app(tmp_path, worker_client)
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", PUBLIC_TOKEN)

    client, project_id, project, sheet_id, _row_id, _audio = _seeded_app_with_audio_row(
        tmp_path
    )
    receipt_id = _enqueue_moss_transcribe(
        client, project_id, sheet_id, key="media_transcribe@sha256:moss-violation-v1"
    )

    assert _drain_one_job(client, sidecar_app, worker_id="moss-violation") is True

    # the violating result WAS produced — and then refused, never stored
    assert len(adapter_calls) == 1

    # same terminal shape as every all-rows-failed run: the run row records
    # the mechanics, the receipt records the failed verdict
    run_row = _latest_run(project)
    assert run_row is not None
    assert run_row["status"] == "completed"
    assert run_row["failed_rows"] == 1
    run_id = int(run_row["id"])

    result_rows = project.db.execute(
        "SELECT * FROM results WHERE run_id=? ORDER BY column_id", (run_id,)
    ).fetchall()
    assert len(result_rows) == 2
    for result_row in result_rows:
        assert result_row["value"] is None
        assert result_row["error_code"] == "model_error"
        # only the bounded public code crosses the boundary — never the
        # worker's internal invalid_adapter_result detail or the violating
        # payload itself
        assert "worker_failure" in str(result_row["error"])
        assert "unlabeled speech" not in str(result_row["error"])

    receipt = _receipt(project, receipt_id)
    assert receipt["status"] == "failed"
    [error] = json.loads(receipt["body"])["errors"]
    assert error["code"] == "external_rows_failed"

    # nothing of the violating result was committed anywhere
    columns = _columns(project, sheet_id)
    assert "detected_language" not in columns
    output_ids = tuple(
        int(columns[name]["id"]) for name in ("transcript", "transcript_segments")
    )
    committed = project.db.execute(
        "SELECT COUNT(*) FROM cells WHERE column_id IN (?, ?)", output_ids
    ).fetchone()[0]
    assert committed == 0
    assert RunResultStore(project).model_calls(run_id) == []

    claims = project.db.execute(
        "SELECT output_name, status FROM output_column_claims ORDER BY output_name"
    ).fetchall()
    # The single preapproved launch reserves these outputs, then fails them.
    assert [(c["output_name"], c["status"]) for c in claims] == [
        ("transcript", "failed"),
        ("transcript_segments", "failed"),
    ]

    [job] = client.app.state.workspace.queue.list_project_jobs(project_id)
    assert job.status == "done"
    assert job.error is None

    assert list(gateway_spool.iterdir()) == []
    assert list(worker_spool.iterdir()) == []
