# frisket-models sidecar; the same Compose stack supports CPU or GPU hosts.
# Torch is pinned to the CPU wheel index: no CUDA blobs in this image.
# Models are NOT pre-baked (multi-GB) — they download at first use into the
# /models volume (HF_HOME + the fastembed/CT2 caches all live under it).
FROM python:3.12-slim
# libgl/libglib: image processing; ffmpeg: audio decode (faster-whisper)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
# CPU-only torch via uv's torch backend selector — the GPU box later swaps
# this for UV_TORCH_BACKEND=cu126 (or auto) and nothing else changes.
ENV UV_TORCH_BACKEND=cpu
RUN uv venv && uv pip install '.[all]'

ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/models/hf \
    FASTEMBED_CACHE_PATH=/models/fastembed \
    FRISKET_MODELS_CONCURRENCY=2
VOLUME /models
EXPOSE 8500
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -sf http://localhost:8500/health || exit 1
CMD ["uvicorn", "--factory", "frisket_models.app:create_app", \
     "--host", "0.0.0.0", "--port", "8500"]
