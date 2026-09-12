"""Pinned Parakeet model/VAD identity, shared by the artifact resolver and
the inference worker.

No imports: the worker (``parakeet_worker.py``) must stay standard-library-
only at import time, and the artifact resolver (``parakeet_artifacts.py``)
imports the sandbox shim. A leaf module with zero imports is the only way
both can share these five values without either pulling in the other's
dependencies.
"""

MODEL = "nemo-parakeet-tdt-0.6b-v3"
MODEL_REVISION = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
VAD_REVISION = "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6"
MODEL_FILES = (
    "config.json",
    "vocab.txt",
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
)
VAD_FILES = ("config.json", "silero_vad.onnx")
