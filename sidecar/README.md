# frisket-models

The stateless model gateway for [frisket](../README.md). Surya 2, PaddleOCR-VL,
Docling, Chandra 2, GLiNER, Faster Whisper, and cross-encoder reranking run in
the self-hosted sidecar. Larger dots.mocr, GLM-OCR, and MOSS deployments use
isolated workers behind the same API. Frisket Cloud deliberately omits Surya 2;
local/team operators can opt into Surya without adding it to the hosted bundle.

Design:

- **Stateless.** The app POSTs blob *bytes* (multipart) — this service may be
  a different machine and never reads the app's disk. Caching and provenance
  are the app's job.
- **Never anonymous.** `FRISKET_MODELS_TOKEN` is a shared bearer secret; the
  service **refuses to start** without it. The app finds us via
  `FRISKET_MODELS_URL`.
- **Backpressure, not queueing.** A concurrency limiter
  (`FRISKET_MODELS_CONCURRENCY`, default 2) returns **429 + Retry-After** when
  full; the app's clients sleep-and-retry. The job queue lives app-side.
- **Lazy engines.** Nothing imports torch at startup; models load on first
  request and stay resident. `GET /capabilities` reports only what imports
  cleanly, so a partial install (one extra) still serves what it has.
- Long-job streaming (SSE/websockets) is **deferred** to the app's job queue.

## Routes

| Route | Body | Returns |
| --- | --- | --- |
| `GET /health` | — (unauthenticated liveness) | `{ok: true}` |
| `GET /capabilities` | — | `{service, version, engines: [{name, route, available, loaded, models, error}], concurrency}` |
| `POST /ocr` | multipart `files` (page images) + form `engine=dots.mocr`\|`glm-ocr`\|`surya2`\|`pp-ocrv6`\|`paddleocr-vl` | `{pages: [{text, blocks: [{text, bbox, score?}]}]}` per part, in order |
| `POST /to-markdown` | multipart `files` (document blobs) + form `engine=docling`\|`chandra` | `{documents: [{markdown, ocr_used}]}` per part |
| `POST /v1/transcribe` | versioned multipart `files`, `engine`, JSON `options` | strict `{contract_version, results}` envelope for resident or isolated engines |
| `POST /ner` | JSON `{texts, labels, threshold?}` | `{results: [[{text, label, start, end, score}]]}` per text |
| `POST /rerank` | JSON `{query, documents, top_k?}` | `{results: [{index, score}]}` best first |
| `POST /v1/embeddings` | JSON `{model?, input}` — the ONLY OpenAI-shaped route | `{object, model, data: [{index, embedding}], usage}` |

The clients in the main repo are the contract: `src/frisket/sdk/ops/ocr.py`
(`_ocr_sidecar`), `src/frisket/sdk/ops/to_markdown.py` (`_convert_sidecar`), and
`src/frisket/sdk/ops/transcribe_engines.py` (`TranscriptionV1Adapter`) POST
these exact shapes
(`files` parts, `engine` form field, `Authorization: Bearer`, retry on 429);
`src/frisket/ai/llm/adapters.py` `embed()` defines the embeddings dialect. All
POST bodies take arrays — batching everywhere.

`pp-ocrv6` is the fast, task-specific text OCR pipeline: it returns recognized
lines, confidence scores, and polygons and has no generative prompt. It uses
the publisher's standard PP-OCRv6 medium detector and recognizer; orientation,
unwarping, and text-line classifiers are disabled as in the official minimal
integration. `paddleocr-vl` is the separate generative document-layout model.

Transcription uses one strictly versioned path for resident Faster Whisper and
isolated GPU workers. Its A/B/C schemas, adapter protocol, intrinsic
diarization semantics, golden fixture, and worker example are documented in
[`src/frisket_models/transcription/PROTOCOL.md`](src/frisket_models/transcription/PROTOCOL.md).
The gateway has a code-owned production MOSS descriptor and registers its endpoint
when the `FRISKET_TRANSCRIPTION_MOSS_WORKER_*` deployment settings are present;
worker containers use
`frisket_models.transcription.worker.create_worker_app(...)`. Operators may
configure routing, credentials, and timeouts for reviewed descriptors, but
cannot define an engine/model/options manifest through environment data. With
every worker group absent, startup performs no worker probe and the remote
registry stays empty. A configured but unreachable worker starts truthfully as
unavailable; probing `/capabilities` never cold-loads it.

## GPU transcription benchmark

`scripts/transcription_benchmark.py` pressure-tests the deployed public gateway,
not a worker or adapter in process. It takes one capability snapshot, labels the
first request cold only when that snapshot reports `loaded=false`, performs
sequential warmups, then releases fixed-size concurrency waves together. Use
`--require-cold` to reject an already-loaded run. A `429` is recorded and never
retried. Request records include latency, real-time factor, structural output
counts, result provenance, and numeric worker timings.

Deploy the existing `contract-stub` worker behind the public gateway to validate
the complete gateway → worker wrapper → adapter path without a model download,
then run:

```sh
cd sidecar
export FRISKET_MODELS_TOKEN=<public gateway token>
uv run python scripts/transcription_benchmark.py \
  --base-url https://models-gpu.example.com \
  --engine contract-stub \
  --audio /path/to/public-domain.wav \
  --require-cold \
  --concurrency 1,2,4 \
  --waves 3 \
  --output contract-stub.jsonl
```

The public token is read only from `FRISKET_MODELS_TOKEN` (or `--token-env`).
Use `--options-file` rather than command-line JSON when context is sensitive.
Output files are created exclusively and never overwritten. JSONL omits audio
paths and names, transcript/segment text, context text, warning text, error
messages/details, and tokens. It retains the fixture SHA-256, safe option
receipt, capability provenance, per-request observations, and one compact run
summary per requested concurrency so configurations remain comparable.

GPU/RAM measurements intentionally stay outside the wire contract and the
timed request process. Run `nvidia-smi` sampling and `docker stats` alongside a
benchmark, save those logs under the same experiment label, and compare their
sampled peaks with the JSONL concurrency summaries. This avoids making
host-specific telemetry a model-worker dependency.

## Run it

```sh
cd sidecar
uv sync                       # light: contract deps only, engines mocked
uv run pytest -q              # contract tests (real-engine tests skip)

uv sync --extra all           # or one of: ocr ocr-paddle convert ner transcribe rerank embed
FRISKET_MODELS_TOKEN=$(openssl rand -hex 24) \
  uv run uvicorn --factory frisket_models.app:create_app --port 8500
```

Surya 2 is intentionally a separate `ocr` extra because its dependency set
does not coexist with the `all` extra's FastEmbed stack. It also needs the
upstream inference backend: Docker plus NVIDIA Container Toolkit for vLLM, or
the `llama-server` executable from llama.cpp for CPU/Apple Silicon. Set
`SURYA_INFERENCE_URL` to attach to an already-running compatible backend.
Surya's code is Apache-2.0, while its model weights carry Datalab's modified
OpenRAIL-M terms; review those terms before enabling it for an organization.
The standard Docker image below installs `all`, not `ocr`, and therefore does
not claim to provide Surya or its external inference backend.

Docker (CPU-only torch; models download at first use into the volume):

```sh
docker build -t frisket-models sidecar/
docker run -e FRISKET_MODELS_TOKEN=... -v frisket-models-cache:/models -p 8500:8500 frisket-models
```

Hosted Modal deployments use `frisket.ai.models.modal_sidecar`. One deploy
registers the gateway plus isolated Parakeet, dots.mocr, GLM-OCR, and MOSS workers
in the same Modal app; each function keeps its own image and scales to zero independently.
Create the `frisket-models-edge`, `frisket-dots-link`, and `frisket-moss-link`
secrets, then deploy once:

```sh
modal deploy --env main -m frisket.ai.models.modal_sidecar
```

The gateway obtains its co-deployed worker URLs from Modal, so the workers do
not need separate deployments.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `FRISKET_MODELS_TOKEN` | — (required) | shared bearer secret; no token = refuse to start |
| `FRISKET_MODELS_CONCURRENCY` | `2` | model-work slots before 429 |
| `FRISKET_OCR_DOTS_WORKER_URL` | — | dots.mocr worker URL; Modal wires this automatically |
| `FRISKET_OCR_DOTS_WORKER_TOKEN` | — | shared bearer for the dots.mocr worker |
| `FRISKET_OCR_GLM_WORKER_URL` | — | GLM-OCR worker URL; Modal wires this automatically |
| `FRISKET_OCR_GLM_WORKER_TOKEN` | — | shared bearer for the GLM-OCR worker |
| `FRISKET_TRANSCRIPTION_MAX_UPLOAD_BYTES` | `262144000` | Boundary-A aggregate audio bytes per request (maximum 16 files) before worker forwarding |
| `FRISKET_TRANSCRIPTION_<ENGINE>_WORKER_URL` | — | Opt-in Boundary-B root origin for a code-owned worker descriptor |
| `FRISKET_TRANSCRIPTION_<ENGINE>_WORKER_TOKEN` | — | Boundary-B bearer; set exactly one of this or `..._TOKEN_FILE` |
| `FRISKET_TRANSCRIPTION_<ENGINE>_WORKER_TOKEN_FILE` | — | Preferred mounted bearer file; regular/no-follow, bounded, with at most one terminal newline |
| `FRISKET_TRANSCRIPTION_<ENGINE>_WORKER_TIMEOUT_SECONDS` | `3600` | Positive finite inference deadline when that worker is configured |
| `FRISKET_TRANSCRIPTION_<ENGINE>_WORKER_PROBE_TIMEOUT_SECONDS` | `5` | Cheap capability deadline, maximum `30` seconds |
| `FRISKET_MODELS_GLINER` | `urchade/gliner_multi-v2.1` | /ner model |
| Whisper Turbo model | pinned `dropbox-dash/faster-whisper-large-v3-turbo` | fixed served model reported in transcription v1 provenance; `model_size` is not authorable |
| `FRISKET_MODELS_RERANK` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | /rerank model |
| `FRISKET_MODELS_EMBED` | `BAAI/bge-small-en-v1.5` | /v1/embeddings model |

App-side: set `FRISKET_MODELS_URL` + `FRISKET_MODELS_TOKEN` on the frisket app
or run the repo compose stack with `--profile models`. Recipes route here when
their engine is one of the sidecar tiers: `ocr` uses `engine='dots.mocr'`,
`engine='glm-ocr'`, `engine='surya2'`, `engine='pp-ocrv6'`, or
`engine='paddleocr-vl'`,
`to_markdown` uses `engine='docling'`, and the named Whisper Turbo model uses
`engine='whisper-turbo'`; hosted Parakeet uses `engine='parakeet-tdt'` through
the same gateway. The app keeps the public run API `run_id`-centric;
work is finished by the app worker queue, while this service only reports
capacity with `429 + Retry-After`. The web recipe picker reads
`GET /api/recipes`, which probes `/capabilities` and disables unavailable
sidecar engines with the reported error.

### VibeVoice-ASR GPU worker

The full VibeVoice-ASR model runs in an isolated CUDA worker behind the
transcription gateway. It supplies segment timestamps, anonymous speaker
labels, and free-text context hints. See the [worker setup guide](workers/vibevoice_asr/README.md)
for the pinned image build, explicit model provisioning, GPU memory guidance,
and authenticated gateway configuration.
