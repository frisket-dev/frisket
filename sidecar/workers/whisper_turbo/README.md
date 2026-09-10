# Whisper Turbo worker

This production worker serves one fixed internal transcription-v1 engine:

- engine: `whisper-turbo`
- package: `faster-whisper==1.2.1`
- model: `dropbox-dash/faster-whisper-large-v3-turbo`
- revision: `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`

Frisket presents this as the named **Whisper Turbo** model. The repository and
revision are code-owned and cannot be overridden. Runtime configuration only
selects device placement, compute type, and the cache location.

The worker requires `FRISKET_TRANSCRIPTION_WORKER_TOKEN` and
`FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID`, admits one request at a time, and
reports repository, revision, image, device, and compute type in v1 provenance.

Useful runtime settings:

| Variable | Default |
| --- | --- |
| `FRISKET_WHISPER_TURBO_DEVICE` | `cuda` |
| `FRISKET_WHISPER_TURBO_DEVICE_INDEX` | `0` |
| `FRISKET_WHISPER_TURBO_COMPUTE_TYPE` | `float16` on CUDA, `float32` on CPU |
| `FRISKET_WHISPER_TURBO_MODEL_CACHE` | `/models/hf` |
| `FRISKET_WHISPER_TURBO_LOCAL_FILES_ONLY` | `true` |

Fresh caches need one explicit provisioning run: temporarily set
`FRISKET_WHISPER_TURBO_LOCAL_FILES_ONLY=false`, start the worker with network
access, and complete one successful transcription. Model construction is lazy,
so health checks and startup alone download nothing. Set the value back to
`true` and recreate the worker for normal operation. The offline capabilities
probe then refuses unless all files from the exact pinned snapshot are present
in the configured cache.

Build from the `sidecar` context:

```bash
docker build \
  --file sidecar/workers/whisper_turbo/Dockerfile \
  --tag frisket-whisper-turbo \
  sidecar
```

CPU-mock verification downloads no weights:

```bash
cd sidecar
uv sync --project workers/whisper_turbo --locked --dev
uv run --project workers/whisper_turbo --no-sync pytest -q workers/whisper_turbo/tests
```
