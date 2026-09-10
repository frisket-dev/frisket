# MOSS-Transcribe-Diarize isolated worker image.
#
# This container is one engine's whole world: the pinned vLLM native server
# (Boundary D) plus the light Boundary-B/C worker app. Everything heavy is
# pinned here, never in the shared sidecar package.
#
# Pins (bumping any of these is a new revision + a fresh live burn):
#   * model weights ...... derived from frisket_worker_moss.constants.MODEL_REVISION
#                          (currently 4a1af868...), i.e. the SAME constant the
#                          adapter reports — the served weights cannot diverge
#                          from the receipt.
#   * vLLM build ......... wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa (cu129)
# The host injects its namespaced immutable image identity at runtime via
# FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID; the adapter descriptor leaves it None.
#
# Base and build-tool images are pinned by manifest-list digest.  Changing any
# dependency requires a rebuilt image (a new OCI digest).

FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04@sha256:d02c4310b6d57ca0b16cd80298bdb33a74187baafe2eccd8a6a16180ddc90802

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    # Ubuntu 24.04 marks /usr as externally managed; this image intentionally
    # owns that isolated Python installation.
    UV_BREAK_SYSTEM_PACKAGES=1 \
    # vLLM's TWO independent audio caps both have to be raised for MOSS's
    # ~90-minute capability, or an 11-minute clip is rejected before inference:
    #   * byte cap (default 25 MB) — a 90-min 16 kHz mono WAV is ~170 MB;
    #   * decode-duration cap (default 600 s / 10 min).
    # The native byte cap deliberately stays above the worker's authoritative
    # 250 MiB bound, avoiding unit/rounding rejection for a worker-accepted
    # upload while remaining unreachable to network peers (loopback only).
    VLLM_MAX_AUDIO_CLIP_FILESIZE_MB=300 \
    VLLM_MAX_AUDIO_DECODE_DURATION_S=5700 \
    # MOSS is always served greedily (temperature=0). vLLM's real greedy path
    # already uses its native sampler; disable FlashInfer's unused synthetic
    # sampling warmup so startup does not JIT-compile CUDA sampling kernels.
    VLLM_USE_FLASHINFER_SAMPLER=0

# Modal's runtime setup invokes `python`; provide it before dropping to the
# unprivileged worker user rather than mutating the imported image later.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 python3-pip git ffmpeg curl patch tini \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*

# uv drives all installs. Keep the exact patch fixed so resolver behavior cannot
# drift between builds.
COPY --from=ghcr.io/astral-sh/uv@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv

# Pinned vLLM nightly that includes MOSS-Transcribe-Diarize model registration.
# The [audio] extra is REQUIRED: plain `vllm` omits the av/soundfile/soxr audio
# decoders, so a valid WAV would fail to decode before inference.
#
# --torch-backend is pinned to cu129 (NOT auto): during a driverless Docker build
# `auto` detects no CUDA and silently installs CPU Torch, which then fails CUDA
# init at runtime even on a GPU host.
RUN uv pip install --system -U "vllm[audio]" \
    --torch-backend=cu129 \
    --extra-index-url https://wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa/cu129 \
    # PyPI's TorchCodec wheel targets CUDA 13, unlike this pinned cu129 stack.
    # vLLM decodes audio on CPU, so install its matching CPU wheel without
    # allowing that index to replace the cu129 Torch packages.
    && uv pip install --system --reinstall --no-deps "torchcodec==0.16.0+cpu" \
        --index-url https://download.pytorch.org/whl/cpu \
    && python3 -c 'from torchcodec.decoders import AudioDecoder' \
    && uv pip install --system "huggingface_hub==1.24.0"

# The pinned source correctly detects both metadata and decoded-sample duration
# overruns, but its PyAV fallback erases those errors as generic invalid audio.
# Guard the exact upstream blob before applying the leaf patch: a source drift
# must fail the build instead of silently producing different error semantics.
COPY workers/moss/patches/vllm-preserve-audio-duration-error.patch /tmp/
RUN VLLM_SITE="$(python3 -c 'import importlib.util, pathlib; print(pathlib.Path(importlib.util.find_spec("vllm").origin).parent.parent)')" \
    && VLLM_AUDIO_FILE="${VLLM_SITE}/vllm/multimodal/media/audio.py" \
    && echo "5f6d3f56beb0e7f629c66c96046635434012241a5c4f47434cc2ec22e64fca97  ${VLLM_AUDIO_FILE}" | sha256sum --check --strict - \
    && git -C "${VLLM_SITE}" apply --check /tmp/vllm-preserve-audio-duration-error.patch \
    && git -C "${VLLM_SITE}" apply /tmp/vllm-preserve-audio-duration-error.patch \
    && rm /tmp/vllm-preserve-audio-duration-error.patch

WORKDIR /app

# Install the shared contract package and this leaf. The build context is the
# sidecar root so frisket-models (../..) is available. The leaf's own pure-python
# deps are locked in workers/moss/uv.lock for standalone/dev installs; the heavy
# GPU layer above is pinned by the vLLM wheel commit + base image.
COPY . /app/sidecar
RUN uv pip install --system /app/sidecar \
    && uv pip install --system /app/sidecar/workers/moss

# Serve as a non-root user. Create it before baking weights so the model cache
# is owned by the runtime user: vLLM then finds the pinned weights via HF_HOME
# at runtime instead of re-downloading (which could drift from the pinned
# revision the receipt claims).
RUN useradd --create-home --uid 10001 moss
ENV HF_HOME=/home/moss/.cache/huggingface
USER moss

# Bake the pinned weights into the image, deriving the id/revision from the
# installed constant so the download can never drift from what the adapter
# reports as provenance.
RUN MODEL_ID="$(python3 -c 'import frisket_worker_moss.constants as c; print(c.MODEL_ID)')" \
    && MODEL_REVISION="$(python3 -c 'import frisket_worker_moss.constants as c; print(c.MODEL_REVISION)')" \
    && hf download "${MODEL_ID}" --revision "${MODEL_REVISION}"

# Torch Inductor/Triton compiles a small runtime component when vLLM starts, so
# keep the minimal host C compiler and Python headers available at runtime.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3.12-dev \
    && rm -rf /var/lib/apt/lists/*
USER moss

EXPOSE 9000
# tini as PID 1 reaps reparented orphans (e.g. vLLM's own worker subprocesses if
# it dies abnormally); entrypoint.sh still owns signal forwarding to its direct
# children. Belt-and-suspenders so no zombie survives a crash.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/sidecar/workers/moss/entrypoint.sh"]
