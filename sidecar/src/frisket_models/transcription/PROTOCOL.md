# Frisket transcription contract

`frisket.transcription.v1` is the versioned wire seam for GPU transcription.
It separates the authenticated Frisket sidecar gateway, an isolated
one-engine worker, and the model adapter inside that worker.

The Python source of truth is
[`contract.py`](./contract.py) in this package. The app-side mirror copy
lives at `src/frisket/contracts/transcription_sidecar.py` (the app cannot
import `frisket_models`, so it is a deliberately self-contained duplicate).
`sidecar/tests/fixtures/transcription.golden.json` is the language-neutral
conformance fixture consumed by both the sidecar package and the main
Frisket package's test suite. `sidecar/tests/test_contract_mirror_parity.py`
directly compares the two live copies' declared schemas — field sets,
requiredness, defaults, constraints, and model config — plus pins the exact
wire literals `frisket.transcription.v1` and `/v1/transcribe`; this proves
the two live copies agree today. A unilateral drift in one copy fails the
test; a synchronized breaking edit to both copies passes it, since app and
sidecar always deploy from the same pinned release (mixed-version
deployment is unsupported). Focused contract, gateway, and worker tests own
executable validation and runtime behavior.

The production path is live. The `whisper-turbo` and `moss` engine declarations in
`src/frisket/contracts/actions/schemas/_engines.py` select transport
`frisket.transcription.v1`; `TranscriptionV1Adapter` in
`src/frisket/sdk/ops/transcribe_engines.py` dispatches that transport. The
sidecar's `create_app` in
`sidecar/src/frisket_models/app.py` serves resident Whisper Turbo directly
and delegates configured isolated engines through
`TranscriptionWorkerGateway(worker_registry_from_env())`.
`sidecar/src/frisket_models/transcription/config.py` includes the reviewed
`MOSS_DEFINITION` in its production definitions and registers its endpoint
when the `FRISKET_TRANSCRIPTION_MOSS_WORKER_*` deployment settings are present.

## Boundary A: app to sidecar gateway

`POST /v1/transcribe` uses the existing sidecar bearer token and multipart:

| Field | Encoding | Cardinality |
| --- | --- | --- |
| `contract_version` | text, exactly `frisket.transcription.v1` | one |
| `engine` | text engine ID | one |
| `options` | canonical JSON object | one |
| `files` | binary file part | 1–16 |

Success is
`{"contract_version":"frisket.transcription.v1","results":[...]}` with one
result per input file, in order. Resident Whisper Turbo and configured
isolated workers share this route and contract.

## Boundary B: gateway to isolated worker

The worker exposes:

- `GET /health` for open liveness only;
- `GET /v1/capabilities`, optionally bearer-protected, which must never load a
  model; and
- `POST /v1/transcribe`, optionally bearer-protected, with the same
  `contract_version`, `engine`, and `options` text fields plus exactly one
  binary `file` part.

Success is
`{"contract_version":"frisket.transcription.v1","result":{...}}`. The
gateway verifies the result engine and the worker verifies engine, model IDs,
revision, and accepted option values against its registration.

After authentication, both HTTP boundaries use
`{"contract_version":"frisket.transcription.v1","error":{"code":...,"message":...,"retryable":...,"details":...}}`.
Boundary A deliberately retains the sidecar's pre-contract bearer dependency:
missing/invalid credentials fail first with its existing `401`/`403`
`{"detail":...}` response. Boundary B's optional bearer failures use the v1
error envelope.
The pinned status semantics are:

| Status | Meaning |
| --- | --- |
| `401`/`403` | missing/invalid bearer before request processing |
| `400` | malformed request, unsupported version, engine, or option |
| `413` | Boundary A aggregate batch bytes, Boundary B file bytes, or an adapter-declared duration/input bound exceeded |
| `429` | no inference slot; `Retry-After` is authoritative |
| `500` | adapter inference or worker-internal failure |
| `502` | worker unreachable or protocol-incompatible at the gateway |
| `503` | worker/model unavailable or failed to load |
| `504` | bounded inference/native-server timeout |

## Boundary C: worker wrapper to adapter

A worker leaf exports one light registration. Heavy model imports belong
inside `factory`, never at module import or capability-probe time.

```python
from pathlib import Path

from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionEngineDescriptor,
)


class Adapter:
    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        ...


def factory() -> Adapter:
    # Import/load the engine or construct its native-server client here.
    return Adapter()


REGISTRATION = AdapterRegistration(
    # The leaf descriptor leaves runtime_image_id=None. The container runtime
    # supplies the actual build identity rather than baking it into adapter code.
    descriptor=TranscriptionEngineDescriptor(..., runtime_image_id=None),
    factory=factory,
    probe=lambda: EngineProbe(available=True, loaded=False, error=None),
)
```

The reusable runtime is
`frisket_models.transcription.worker.create_worker_app(REGISTRATION, ...)`.
Pass `runtime_image_id="oci:sha256:<64 lowercase hex>"` or set
`FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID` to an exact `oci:sha256:...` or
`modal:im-...` runtime identity. A worker
without a valid runtime image identity remains truthfully unavailable and rejects
inference with `503`.
It validates the request, enforces no-queue admission and a byte bound, spools
the upload to a generated local `Path`, lazily constructs the adapter once,
validates its result, and removes the temporary file on every exit path.
The leaf may raise public `TranscriptionInputError` for caller-controlled
decode/format failures (`400`) or with `too_large=True` for a decoded-duration
or engine input bound (`413`). During inference, HTTP timeouts are retryable
`504 inference_timeout`; native-server network failures and remote disconnects
are retryable `503 engine_unavailable`. Local HTTP protocol/configuration errors
and other exceptions remain non-retryable server failures.

## Option and diarization semantics

Runtime options contain only caller knobs: `language`, `diarize`, exact or
min/max speaker counts, `vad`, and `context`. `context` is non-empty and at
most 500 characters — the same bound as the app-side product cap and the web
input. `diarization_mode` is descriptor truth, not caller input:

- `none`: no diarization controls;
- `optional`: `diarize` is accepted, with counts only when
  `speaker_hint="count"`; and
- `intrinsic`: diarization is always on and all diarization/count knobs are
  invalid wire inputs.

For an intrinsic product engine, the app's declaration-driven projection
removes the default `diarize=false` and all speaker-count controls before
Boundary A. A direct Boundary-A caller that nevertheless supplies one receives
`400 unsupported_option`; the gateway does not forward it across B, and the
worker repeats the same validation as a defense for direct Boundary-B calls.

The production MOSS descriptor in
`sidecar/src/frisket_models/transcription/moss.py` registers
`diarization_mode="intrinsic"` and `speaker_hint="none"`; it does not inherit
Whisper controls. `accepted_options` is a verbatim receipt of the caller values
that passed descriptor validation and were forwarded to the adapter. The
adapter must echo those values without normalizing or clamping them.
Applied/detected outcomes belong in result fields (for example `language`) or
`warnings`; a warning cannot excuse declaring and then ignoring a knob.

Segments are ordered by nondecreasing start time, may overlap across speakers,
and require nonnegative `start <= end`. Every segment from an intrinsic engine
(or an optional engine invoked with `diarize=true`) has a non-empty `speaker`;
an engine that did not declare/run diarization must not return speaker data.
The existing qualitative `speaker_confidence` marker (for example
`"approximate"`) requires a speaker label. Per-word timing arrays remain
optional. Word objects deliberately contain only `word`, `start`, and `end`;
speaker ownership is segment-level, and adding `words[].speaker` requires a
new contract version. Results also carry immutable model IDs and revision,
device/dtype, nonnegative timings, warnings, and accepted options. The
model-specific probe is authoritative for `loaded`; constructing its
lightweight Boundary-D client does not imply that native-server weights are
resident.

The golden error is the canonical Boundary-B shape for one unsupported-option
example. Error codes and HTTP statuses are machine-facing; human-readable
`message` and `details` provide diagnostics and must not be parsed for control
flow.

## Known limitations

`RemoteProtocolError` from the native model server (Boundary C) is classified
as retryable `503 engine_unavailable`
(`sidecar/src/frisket_models/transcription/worker.py`, in the inference
exception handling). That's correct for a genuine transient crash or
mid-response disconnect, but a persistent adapter bug that always sends a
malformed response for some payload shape would be retried forever under the
same "unavailable" label instead of surfacing as the deterministic failure it
is. This remains an accepted classification risk. The live MOSS adapter in
`sidecar/workers/moss/src/frisket_worker_moss/adapter.py` exercises this native
server path, so changes to the classification can now be tested against that
real adapter behavior.

## Consumer gate

Contract and worker changes should run the MOSS lane from `sidecar/`:

```sh
uv sync --dev
uv run --no-sync pytest -q \
  tests/test_transcription_contract.py \
  tests/test_transcription_worker.py \
  tests/test_transcription_gateway.py
```

The MOSS leaf exports its live `REGISTRATION` from
`sidecar/workers/moss/src/frisket_worker_moss/adapter.py`; its ASGI app binds
that registration to `create_worker_app`, and
`sidecar/workers/moss/entrypoint.sh` launches the app with uvicorn. The
Whisper Turbo is the fixed production Faster-Whisper v1 worker:
`sidecar/workers/whisper_turbo/src/frisket_whisper_turbo/app.py`
binds its `REGISTRATION`, and its Dockerfile launches that app with uvicorn's
factory mode.
