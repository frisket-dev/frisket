# Isolated dots.mocr OCR worker. The base tag and multi-architecture manifest
# are immutable; changing either the runtime or weights requires a new image.
FROM vllm/vllm-openai:v0.17.1@sha256:0dc46f74eb0e630675d83101dc66c6441c4475cceedcf9235ee42b87c3affd23

# Modal probes ``python`` when registering a Function image. Reuse vLLM's
# existing interpreter so the worker sees the packages already installed in it.
RUN ln -s /usr/bin/python3.12 /usr/local/bin/python

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface \
    PYTHONPATH=/app/sidecar/src

COPY workers/dots_mocr /app/sidecar/workers/dots_mocr
COPY src/frisket_models/__init__.py src/frisket_models/dots_mocr_identity.py /app/sidecar/src/frisket_models/

# Bake the exact weights into the image so a running worker never follows a
# mutable Hugging Face branch.
RUN MODEL_ID="$(python3 -c 'from frisket_models.dots_mocr_identity import MODEL_ID; print(MODEL_ID)')" \
    && MODEL_REVISION="$(python3 -c 'from frisket_models.dots_mocr_identity import MODEL_REVISION; print(MODEL_REVISION)')" \
    && hf download "${MODEL_ID}" --revision "${MODEL_REVISION}" \
    && chmod 0755 /app/sidecar/workers/dots_mocr/entrypoint.sh

EXPOSE 9000
ENTRYPOINT ["/app/sidecar/workers/dots_mocr/entrypoint.sh"]
