# MOSS transcription worker leaf

Isolated Phase-2 worker for **MOSS-Transcribe-Diarize 0.9B** — an end-to-end,
single-pass multi-speaker transcription + diarization model (Apache-2.0,
[OpenMOSS-Team/MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize)).

This leaf owns only the model-specific edge of `frisket.transcription.v1`. The
shared HTTP transport (validation, no-queue admission, byte bound, spooling,
cleanup) is the reusable
`frisket_models.transcription.worker.create_worker_app`; this directory supplies
the adapter, descriptor, dependency lock, and image.

## Boundaries

- **C (adapter):** `MossAdapter.transcribe(path, options)` in
  `src/frisket_worker_moss/adapter.py`.
- **D (native server):** MOSS is served by a pinned vLLM build behind the
  OpenAI-compatible `POST /v1/audio/transcriptions`. The adapter is a thin httpx
  client to it — **no torch/vLLM import in adapter code** — so the whole path is
  testable on CPU with a mocked transport.

## Contract declaration

| Field | Value | Why |
| --- | --- | --- |
| `engine` | `moss` | |
| `diarization_mode` | `intrinsic` | MOSS always diarizes in one pass; speaker knobs are rejected before B/C |
| `speaker_hint` | `none` | no exact/min/max speaker controls exist |
| `context` | `true` | forwarded as hotwords appended to the default diarization prompt (`热词提示：…`) |
| `language` | `false` | the pinned MOSS build ignores the endpoint's language field (always auto-detects), so accepting it would violate the no-ignore rule; a supplied `language` is rejected upstream |
| `vad` | `false` | MOSS has no VAD control |

Words are **not** emitted (MOSS produces `{start,end,speaker,text}` only);
`words` stays absent rather than fabricated. Segment overlap across speakers is
preserved, never flattened. `result.language` is `null` — MOSS auto-detects and
does not report the detected language, so none is asserted.

## Output parsing

MOSS emits one flat transcript string — `[start][Sxx]text[end]…` — parsed by
`transcript.py`. The final numeric bracket before the next complete opener is
the segment close, so inner bracketed text (`第[2024]年`, `[laughter]`) is
preserved. Detectably malformed output fails closed: truncated tails,
backwards spans, ambiguous adjacent timestamps, prefix junk, and decreasing
starts are server faults rather than partial or silently reordered successes.
Whitespace-only output remains valid silence.

The flat grammar has no explicit completion marker. A decoder cut immediately
after one numeric text token is indistinguishable from a valid segment close;
no non-streaming string parser can resolve that case. The raised 65k decoder
budget reduces that risk, and the live GPU burn remains the required proof that
real long-form output finishes cleanly.

## Immutable pins

Constants in `src/frisket_worker_moss/constants.py`, mirrored in the Dockerfile:

- **model:** `OpenMOSS-Team/MOSS-Transcribe-Diarize` @ `4a1af868018e7974197f4f018730758012b28c27`
- **vLLM build:** `wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa` (cu129)

The host injects an `oci:sha256:...` or `modal:im-...` identity via
`FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID`; the descriptor leaves it `None`.

## Runtime configuration

`MossConfig.from_env` reads `FRISKET_MOSS_TIMEOUT_SECONDS`,
`FRISKET_MOSS_PROBE_TIMEOUT_SECONDS`, and `FRISKET_MOSS_MAX_COMPLETION_TOKENS`.
The worker app also reads `FRISKET_MOSS_WORKER_TOKEN`,
`FRISKET_MOSS_WORKER_CONCURRENCY`, and `FRISKET_MOSS_WORKER_PORT`.

Deliberately **not** env-configurable: `base_url` is fixed to the co-located
loopback server (an arbitrary external server could not guarantee the pinned
revision a receipt claims), and `device`/`dtype` are image-committed constants
(`SERVED_DEVICE` / `SERVED_DTYPE`), not forgeable per-deploy values. The image
sets vLLM's audio caps (`VLLM_MAX_AUDIO_CLIP_FILESIZE_MB`,
`VLLM_MAX_AUDIO_DECODE_DURATION_S`) so MOSS's ~90-minute capability is reachable.
The pinned vLLM wheel is amended by one leaf-owned, checksum-guarded patch that
preserves its specific duration error through the PyAV fallback. Without that
patch vLLM collapses decoded-duration overruns into its generic corrupt-audio
400, making the contract-required 413 classification impossible at Boundary C.

## Gates

**Fixture-green (this leaf, CPU) — the adapter gate, not "done":**

```sh
cd sidecar
.venv/bin/python -m pytest workers/moss/tests/ -q
```

- `test_transcript.py` — recorded-output parser fixtures.
- `test_conformance.py` — `MossAdapter` through the real `create_worker_app`
  with a mocked Boundary-D server (happy path, intrinsic-knob rejection,
  input/too-large/inference error mapping, capabilities).

Before deployment, validate the real Docker context from `sidecar/` with
`docker build --check -f workers/moss/Dockerfile .`. This is a deploy preflight,
not a unit test, because registry metadata resolution may use the network.

**Live GPU diarized burn — shared, later.** Fixture-green ≠ MOSS done: the true
multi-speaker burn on the real GPU host (nondecreasing starts, `start <= end`,
preserved overlap, permutation-invariant speaker comparison, real
device/dtype/revision/digest) is a joint proof owned with the platform lane and
is **not** satisfied by this directory alone.
