# Isolated GLM-OCR worker on the publisher-supported vLLM release. The image
# and model revision are immutable so production never follows mutable tags.
FROM vllm/vllm-openai:v0.19.0-ubuntu2404@sha256:527873ce286de09ebc8cf3029b7304d153e00ca934117326b14e1866d5833291

RUN ln -sf /usr/bin/python3.12 /usr/local/bin/python

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface \
    PYTHONPATH=/app/sidecar/src

COPY src/frisket_models/__init__.py src/frisket_models/glm_ocr_identity.py /app/sidecar/src/frisket_models/

RUN python3 -m pip install --no-cache-dir "transformers==5.3.0" \
    && MODEL_ID="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_ID; print(MODEL_ID)')" \
    && MODEL_REVISION="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_REVISION; print(MODEL_REVISION)')" \
    && hf download "${MODEL_ID}" --revision "${MODEL_REVISION}"

COPY workers/glm_ocr /app/sidecar/workers/glm_ocr
RUN chmod 0755 /app/sidecar/workers/glm_ocr/entrypoint.sh

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

EXPOSE 9000
ENTRYPOINT ["/app/sidecar/workers/glm_ocr/entrypoint.sh"]
