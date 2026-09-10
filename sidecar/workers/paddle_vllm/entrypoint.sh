#!/usr/bin/env bash
# Keep Paddle's official vLLM runtime dependency-isolated from the current
# PaddleOCR pipeline while sharing one GPU and one scale-to-zero container.
set -euo pipefail

readonly VLLM_PORT=8118
readonly WORKER_PORT=9000
readonly STARTUP_TIMEOUT=1700
readonly PIPELINE_PYTHON=/opt/frisket-paddle-pipeline/bin/python
readonly MODEL_NAME=PaddleOCR-VL-1.6-0.9B

VLLM_PID=""
WORKER_PID=""

stop_children() {
    local pid
    for pid in "${VLLM_PID:-}" "${WORKER_PID:-}"; do
        if [[ -n "${pid}" ]]; then
            kill -TERM "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${VLLM_PID:-}" "${WORKER_PID:-}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
}

on_signal() {
    trap '' TERM INT
    stop_children
    exit "$1"
}

trap 'on_signal 143' TERM
trap 'on_signal 130' INT

# This is Paddle's launcher, model identity, tuned defaults, and bundled chat
# template. Frisket does not supply a generic VLM prompt or call vLLM directly.
/usr/local/bin/paddleocr genai_server \
    --model_name "${MODEL_NAME}" \
    --host 127.0.0.1 \
    --port "${VLLM_PORT}" \
    --backend vllm &
VLLM_PID=$!

deadline=$((SECONDS + STARTUP_TIMEOUT))
until "${PIPELINE_PYTHON}" -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${VLLM_PORT}/health', timeout=2).read()" \
    >/dev/null 2>&1; do
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        wait "${VLLM_PID}"
        exit $?
    fi
    if (( SECONDS >= deadline )); then
        echo "PaddleOCR-VL vLLM did not become ready within ${STARTUP_TIMEOUT}s" >&2
        stop_children
        exit 1
    fi
    sleep 1
done

export FRISKET_PADDLE_VL_REC_SERVER_URL="http://127.0.0.1:${VLLM_PORT}/v1"
export PYTHONPATH=/opt/frisket-models/src

"${PIPELINE_PYTHON}" -m uvicorn \
    --factory app:create_app \
    --app-dir /opt/frisket-models/workers/paddle_vllm \
    --host 0.0.0.0 \
    --port "${WORKER_PORT}" &
WORKER_PID=$!

if wait -n "${VLLM_PID}" "${WORKER_PID}"; then
    status=1
else
    status=$?
fi

trap '' TERM INT
stop_children
exit "${status}"
