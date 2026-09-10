# Fixed production Parakeet TDT worker.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg python3 python3-venv python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

WORKDIR /app/sidecar
COPY pyproject.toml README.md ./
COPY src/ src/

WORKDIR /app/sidecar/workers/parakeet_tdt
COPY workers/parakeet_tdt/pyproject.toml \
     workers/parakeet_tdt/uv.lock \
     workers/parakeet_tdt/README.md ./
COPY workers/parakeet_tdt/src/ src/
RUN uv sync --locked --no-dev --default-index https://pypi.org/simple
RUN .venv/bin/python -m frisket_parakeet_tdt.provision

ENV PATH="/app/sidecar/workers/parakeet_tdt/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    FRISKET_PARAKEET_TDT_MODEL_ROOT=/models/parakeet-tdt \
    FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY=1

EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3).read()"]

CMD ["uvicorn", "--factory", \
     "frisket_parakeet_tdt.app:create_app", \
     "--host", "0.0.0.0", "--port", "9000"]
