# Fixed production Whisper Turbo worker.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg python3 python3-venv python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

# Keep the source layout used by the leaf's ../.. path dependency.
WORKDIR /app/sidecar
COPY pyproject.toml README.md ./
COPY src/ src/

WORKDIR /app/sidecar/workers/whisper_turbo
COPY workers/whisper_turbo/pyproject.toml \
     workers/whisper_turbo/uv.lock \
     workers/whisper_turbo/README.md ./
COPY workers/whisper_turbo/src/ src/
RUN uv sync --locked --no-dev --default-index https://pypi.org/simple

ENV PATH="/app/sidecar/workers/whisper_turbo/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/hf \
    FRISKET_WHISPER_TURBO_DEVICE=cuda \
    FRISKET_WHISPER_TURBO_DEVICE_INDEX=0 \
    FRISKET_WHISPER_TURBO_MODEL_CACHE=/models/hf \
    FRISKET_WHISPER_TURBO_LOCAL_FILES_ONLY=true \
    FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY=1

VOLUME /models
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3).read()"]

CMD ["uvicorn", "--factory", \
     "frisket_whisper_turbo.app:create_app", \
     "--host", "0.0.0.0", "--port", "9000"]
