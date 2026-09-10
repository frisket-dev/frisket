# VibeVoice-ASR worker

This is a fixed native Transformers worker for `microsoft/VibeVoice-ASR-HF`
at revision `f22241c2062b3b25272bf117397e03d73381037a`, with
`transformers==5.3.0`. It runs BF16 on CUDA only, accepts one request at a
time, and serves the existing authenticated transcription-v1 worker routes.

The model returns timestamped anonymous speaker turns. Frisket maps those
recording-local IDs to `S1`, `S2`, and so on in first-appearance order. It
preserves bracketed acoustic tags in segment text. It does not return word
timestamps, a detected language, speaker confidence, speaker names, VAD,
speaker-count controls, or language forcing. Free-text `context` is passed as
the model prompt for names, terms, topics, and similar hints.

Model identity, revision, CUDA, BF16, greedy generation, and the 32,768-token
generation limit are code-owned. The worker rejects malformed or partial
structured output, a missing speaker label, and a generation that lacks EOS;
it never returns a parsed prefix. The upstream acoustic encoder is stochastic
even with greedy decoding, so repeated requests are not guaranteed bitwise
identical. Audio is decoded by ffmpeg to 24 kHz mono float32 and rejected over
60 minutes rather than cropped.

Build from the `sidecar` context:

```bash
docker build \
  --file sidecar/workers/vibevoice_asr/Dockerfile \
  --tag frisket-vibevoice-asr \
  sidecar
```

Normal operation is offline. Provision the named Docker volume explicitly on
a GPU host before starting the worker:

```bash
docker run --rm --gpus all \
  --user 0 \
  -v frisket-transcription-model-cache:/models \
  --entrypoint sh \
  frisket-vibevoice-asr \
  -c 'hf download microsoft/VibeVoice-ASR-HF \
      --revision f22241c2062b3b25272bf117397e03d73381037a \
      --cache-dir /models/hf && chown -R 10001:10001 /models'
```

The runtime always uses `local_files_only=true`; it never downloads weights
while serving requests. The snapshot is about 16.69 GB. A 24 GB GPU is only a
plausible short-audio minimum. Plan at least 40 GB for practical longer jobs;
80 GB is the safer operational tier. These are provisioning estimates, not a
guarantee that a one-hour file will fit. A live GPU burn is still required to
validate the selected hardware.

Runtime placement settings are deliberately narrow:

| Variable | Default |
| --- | --- |
| `FRISKET_VIBEVOICE_ASR_DEVICE_INDEX` | `0` |
| `FRISKET_VIBEVOICE_ASR_MODEL_CACHE` | `/models/hf` |
| `FRISKET_VIBEVOICE_ASR_LOCAL_FILES_ONLY` | `true` (required) |
| `FRISKET_VIBEVOICE_ASR_ACOUSTIC_TOKENIZER_CHUNK_SIZE` | `1440000` |

The chunk size is an encoder-memory tuning value and must be a positive
multiple of 3200; it does not split transcript output. The shared worker also
requires `FRISKET_TRANSCRIPTION_WORKER_TOKEN` and
`FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID`, with concurrency fixed to one.

To use the repository's GPU-host Compose setup, set these variables in the
host environment (the two bearers must be different):

```bash
export FRISKET_MODELS_HOST=models.example.com
export FRISKET_MODELS_TOKEN="$(openssl rand -hex 32)"
export FRISKET_GPU_WORKER_TOKEN="$(openssl rand -hex 32)"
export FRISKET_GPU_WORKER_IMAGE=frisket-vibevoice-asr
export FRISKET_GPU_WORKER_COMMAND='uvicorn --factory frisket_vibevoice_asr.app:create_app --host 0.0.0.0 --port 9000'
export FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID="$(docker image inspect frisket-vibevoice-asr --format '{{.Id}}')"
export FRISKET_TRANSCRIPTION_VIBEVOICE_ASR_WORKER_URL=http://gpu-worker:9000
export FRISKET_TRANSCRIPTION_VIBEVOICE_ASR_WORKER_TOKEN="$FRISKET_GPU_WORKER_TOKEN"
export FRISKET_TRANSCRIPTION_VIBEVOICE_ASR_WORKER_TIMEOUT_SECONDS=3600

docker compose -f docker-compose.models-gpu.yml --profile gpu up -d --build
```

On the Frisket app, configure `FRISKET_MODELS_URL=https://models.example.com`
and the same `FRISKET_MODELS_TOKEN`. The gateway probes the worker before
advertising VibeVoice-ASR. The worker stays on the private Compose network;
only the gateway's TLS entrypoint is published. The existing host-wide GPU
lease prevents another worker profile from using the same GPU concurrently.

For offline boundary tests without GPU dependencies or model downloads:

```bash
cd sidecar
uv sync --project workers/vibevoice_asr --locked --dev
uv run --project workers/vibevoice_asr --no-sync pytest -q workers/vibevoice_asr/tests
```

Serbian is listed in the upstream checkpoint's training-language inventory.
Macedonian is not listed and remains unverified. Neither language has been
quality-tested by this integration. Context is recognition guidance, not a
reliable instruction channel for forcing language, speaker identities, or
verbatim/clean output.

References: [Microsoft checkpoint](https://huggingface.co/microsoft/VibeVoice-ASR-HF/tree/f22241c2062b3b25272bf117397e03d73381037a),
[Transformers 5.3 implementation](https://github.com/huggingface/transformers/tree/v5.3.0/src/transformers/models/vibevoice_asr),
and [model limitations](https://arxiv.org/html/2601.18184v2#S4).
