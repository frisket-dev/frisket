#!/usr/bin/env bash
# Launch the MOSS native server (Boundary D) and the worker app (Boundary B/C)
# co-located in one container.
#
# The native server binds LOOPBACK only: the sole network entrypoint is the
# worker (bearer auth, no-queue admission, byte bound, option + result
# validation). A network peer cannot reach vLLM's port 8000 and bypass all of
# that. The adapter's base_url is fixed to this loopback server on purpose — a
# split external server is not offered because it could not guarantee the pinned
# revision the receipt claims.
set -euo pipefail

# Single source of truth: the served weights are exactly the pinned constant the
# adapter reports as provenance, so a receipt can never claim a revision the
# server did not run.
MODEL_ID="$(python3 -c 'import frisket_worker_moss.constants as c; print(c.MODEL_ID)')"
MODEL_REVISION="$(python3 -c 'import frisket_worker_moss.constants as c; print(c.MODEL_REVISION)')"
WORKER_PORT="${FRISKET_MOSS_WORKER_PORT:-9000}"
NATIVE_STARTUP_TIMEOUT="${FRISKET_MOSS_NATIVE_STARTUP_TIMEOUT:-1700}"

if ! [[ "${NATIVE_STARTUP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
    echo "FRISKET_MOSS_NATIVE_STARTUP_TIMEOUT must be a positive integer" >&2
    exit 2
fi

VLLM_PID=""
WORKER_PID=""

signal_children() {
    local signal="$1"
    local pid

    for pid in "${VLLM_PID:-}" "${WORKER_PID:-}"; do
        if [[ -n "${pid}" ]]; then
            kill -s "${signal}" "${pid}" 2>/dev/null || true
        fi
    done
}

reap_children() {
    local pid

    for pid in "${VLLM_PID:-}" "${WORKER_PID:-}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
}

on_signal() {
    local signal="$1"
    local status="$2"

    # Do not let a second signal interrupt cleanup and leave an orphaned child.
    trap '' TERM INT
    signal_children "${signal}"
    reap_children
    exit "${status}"
}

trap 'on_signal TERM 143' TERM
# Non-interactive Bash background jobs can inherit SIGINT as ignored while
# vLLM is still importing. Translate supervisor INT to child TERM, but preserve
# the conventional container exit status 130.
trap 'on_signal TERM 130' INT

# MOSS emits 12.5 encoder tokens per audio second. vLLM otherwise uses its
# 2,048-token API-server default, rejecting ordinary clips over ~2.7 min.
# 73,728 tokens covers the model's documented 90-minute envelope plus enough
# rounding margin for container/codec duration differences.
vllm serve "${MODEL_ID}" \
    --revision "${MODEL_REVISION}" \
    --trust-remote-code \
    --dtype bfloat16 \
    --max-num-batched-tokens 73728 \
    --host 127.0.0.1 \
    --port 8000 &
VLLM_PID=$!

# Do not bind the public worker port until vLLM can accept inference. Modal uses
# that bind as its web-server readiness signal, so this lets its startup timeout
# and result redirect carry the first cold request instead of returning a 503.
native_deadline=$((SECONDS + NATIVE_STARTUP_TIMEOUT))
until curl --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null; do
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        if wait "${VLLM_PID}"; then
            vllm_status=1
        else
            vllm_status=$?
        fi
        exit "${vllm_status}"
    fi
    if (( SECONDS >= native_deadline )); then
        echo "vLLM did not become ready within ${NATIVE_STARTUP_TIMEOUT}s" >&2
        signal_children TERM
        reap_children
        exit 1
    fi
    sleep 1
done

uvicorn frisket_worker_moss.app:app --host 0.0.0.0 --port "${WORKER_PORT}" &
WORKER_PID=$!

# Supervise both: if EITHER exits (vLLM crash on load, worker death), tear the
# other down and fail so the orchestrator restarts the container rather than
# leaving a half-dead pair answering /health forever.
if wait -n "${VLLM_PID}" "${WORKER_PID}"; then
    child_status=1
else
    child_status=$?
fi

trap '' TERM INT
signal_children TERM
reap_children
exit "${child_status}"
