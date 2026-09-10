#!/usr/bin/env bash
# Co-locate the native vLLM server and authenticated Frisket proxy. The native
# OpenAI-compatible endpoint stays loopback-only.
set -euo pipefail

readonly MODEL_ID="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_ID; print(MODEL_ID)')"
readonly MODEL_REVISION="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_REVISION; print(MODEL_REVISION)')"
readonly MODEL_CACHE_DIR="models--${MODEL_ID//\//--}"
readonly MODEL_PATH="${HF_HOME}/hub/${MODEL_CACHE_DIR}/snapshots/${MODEL_REVISION}"
readonly MAX_MODEL_TOKENS="$(python3 -c 'from frisket_models.glm_ocr_identity import PROFILE; print(PROFILE.max_model_tokens)')"
readonly NATIVE_PORT=8000
readonly WORKER_PORT=9000
readonly STARTUP_TIMEOUT=1200

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

vllm serve "${MODEL_PATH}" \
    --revision "${MODEL_REVISION}" \
    --served-model-name glm-ocr \
    --max-model-len "${MAX_MODEL_TOKENS}" \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization 0.9 \
    --speculative-config '{"method":"mtp","num_speculative_tokens":3}' \
    --host 127.0.0.1 \
    --port "${NATIVE_PORT}" &
VLLM_PID=$!

deadline=$((SECONDS + STARTUP_TIMEOUT))
until python3 -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${NATIVE_PORT}/health', timeout=2).read()" \
    >/dev/null 2>&1; do
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        wait "${VLLM_PID}"
        exit $?
    fi
    if (( SECONDS >= deadline )); then
        echo "vLLM did not become ready within ${STARTUP_TIMEOUT}s" >&2
        stop_children
        exit 1
    fi
    sleep 1
done

python3 -m uvicorn \
    --factory app:create_app \
    --app-dir /app/sidecar/workers/glm_ocr \
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
