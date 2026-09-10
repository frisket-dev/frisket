# Native Transformers VibeVoice-ASR runtime, pinned for reproducible GPU builds.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg python3 python3-venv python-is-python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin frisket

COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

WORKDIR /app/sidecar
COPY pyproject.toml README.md ./
COPY src/ src/

WORKDIR /app/sidecar/workers/vibevoice_asr
COPY workers/vibevoice_asr/pyproject.toml \
     workers/vibevoice_asr/uv.lock \
     workers/vibevoice_asr/README.md ./
COPY workers/vibevoice_asr/src/ src/
RUN uv sync --locked --no-dev --extra gpu --default-index https://pypi.org/simple

ENV PATH="/app/sidecar/workers/vibevoice_asr/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/hf \
    FRISKET_VIBEVOICE_ASR_DEVICE=cuda \
    FRISKET_VIBEVOICE_ASR_DEVICE_INDEX=0 \
    FRISKET_VIBEVOICE_ASR_MODEL_CACHE=/models/hf \
    FRISKET_VIBEVOICE_ASR_LOCAL_FILES_ONLY=true \
    FRISKET_VIBEVOICE_ASR_ACOUSTIC_TOKENIZER_CHUNK_SIZE=1440000 \
    FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY=1

VOLUME /models
USER 10001
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3).read()"]

CMD ["uvicorn", "--factory", "frisket_vibevoice_asr.app:create_app", "--host", "0.0.0.0", "--port", "9000"]
