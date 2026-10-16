"""Zero-import identity for the optional EmbeddingGemma 2 runtime."""

from __future__ import annotations


MODEL_ID = "google/embeddinggemma-2"
MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
MODEL_REF = f"hf-snapshot:{MODEL_ID}@{MODEL_REVISION}"
SETUP_REF = "embeddinggemma-2"
RUNTIME_PROFILE = "embeddinggemma-v1"
RUNTIME_EXTRA = "retrieval-multimodal"
TRANSFORMERS_VERSION = "5.19.0"
TORCH_VERSION = "2.14.1"
TORCHVISION_VERSION = "0.29.1"
MODEL_SAFETENSORS_SHA256 = (
    "197a32965d4b1105faf060417baa899e193fb73cd401f42ec9295234d5553d79"
)

# This is the exact minimal snapshot exercised by the phase-one direct-runtime
# gate.  Sentence Transformers metadata is not required by the production
# AutoModel/AutoProcessor path.
REQUIRED_SNAPSHOT_FILES = (
    "config.json",
    "chat_template.jinja",
    "model.safetensors",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
)


__all__ = [
    "MODEL_ID",
    "MODEL_REF",
    "MODEL_REVISION",
    "MODEL_SAFETENSORS_SHA256",
    "REQUIRED_SNAPSHOT_FILES",
    "RUNTIME_EXTRA",
    "RUNTIME_PROFILE",
    "SETUP_REF",
    "TORCH_VERSION",
    "TORCHVISION_VERSION",
    "TRANSFORMERS_VERSION",
]
