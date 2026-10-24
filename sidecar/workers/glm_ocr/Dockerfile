# Isolated GLM-OCR worker on the publisher-supported vLLM release. The image
# and model revision are immutable so production never follows mutable tags.
FROM vllm/vllm-openai:v0.20.0@sha256:04563c302537a91aa49ebdfbceda96111c5712275999b7e8804fa598f0b5641d

RUN ln -sf /usr/bin/python3.12 /usr/local/bin/python

# The upstream Ubuntu image includes PyGObject without its Cairo dependency.
RUN apt-get update && apt-get install -y --no-install-recommends python3-cairo \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface \
    PYTHONPATH=/app/sidecar/src

COPY src/frisket_models/__init__.py src/frisket_models/glm_ocr_identity.py src/frisket_models/server_startup.py /app/sidecar/src/frisket_models/

# GLM-OCR requires Transformers 5; vLLM 0.19 declared <5. Version 0.20
# supports 5.5.1 without overriding either package's dependency requirements.
RUN python3 -m pip install --no-cache-dir "transformers==5.5.1" \
    && python3 -m pip check \
    && python3 -c 'import transformers; import vllm.entrypoints.openai.api_server; import frisket_models.server_startup' \
    && MODEL_ID="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_ID; print(MODEL_ID)')" \
    && MODEL_REVISION="$(python3 -c 'from frisket_models.glm_ocr_identity import MODEL_REVISION; print(MODEL_REVISION)')" \
    && hf download "${MODEL_ID}" --revision "${MODEL_REVISION}"

COPY workers/glm_ocr /app/sidecar/workers/glm_ocr
RUN chmod 0755 /app/sidecar/workers/glm_ocr/entrypoint.sh

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

EXPOSE 9000
ENTRYPOINT ["/app/sidecar/workers/glm_ocr/entrypoint.sh"]
