# Parakeet TDT worker

This isolated production worker serves one fixed transcription-v1 engine:

- engine: `parakeet-tdt`
- ASR: `istupakov/parakeet-tdt-0.6b-v2-onnx`
- VAD: `istupakov/silero-vad-onnx`
- diarizer: `nvidia/diar_streaming_sortformer_4spk-v2.1`

All three model snapshots are revision-pinned in the shared descriptor and
baked into the image. Runtime model downloads and model-identity overrides are
not supported. Parakeet ASR runs through ONNX Runtime on CPU; optional
Sortformer diarization uses the attached CUDA device.

The worker requires `FRISKET_TRANSCRIPTION_WORKER_TOKEN` and
`FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID`, admits one request at a time, and
listens on port 9000.

Build from the `sidecar` context:

```bash
docker build \
  --file sidecar/workers/parakeet_tdt/Dockerfile \
  --tag frisket-parakeet-tdt \
  sidecar
```

Focused tests use fakes and do not download weights:

```bash
cd sidecar
uv run --project workers/parakeet_tdt --no-sync pytest -q \
  workers/parakeet_tdt/tests
```
