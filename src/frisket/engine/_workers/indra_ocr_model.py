"""Pinned identity for the optional local Indra OCR engine."""

from __future__ import annotations

ENGINE_ID = "indra-ocr"
PACKAGE_VERSION = "0.3.1"
SETUP_REF = "engine-setup:indra-ocr.local@1"

MODEL_REPO = "datalab-to/indra-ocr"
MODEL_REVISION = "d7c839c892bfe4b8993373db64c62f154d05d70b"
MODEL_FILES = (
    "LICENSE",
    "det.onnx",
    "meta.json",
    "order.onnx",
    "orient.onnx",
    "rec.onnx",
    "rec_cjk.onnx",
    "script.onnx",
)
MODEL_DOWNLOAD_SIZE = 18_312_922
MODEL_LICENSE = "AI PUBS OPEN RAIL-M LICENSE (MODIFIED)"
MODEL_LICENSE_URL = (
    f"https://huggingface.co/datalab-to/indra-ocr/blob/{MODEL_REVISION}/LICENSE"
)
MODEL_REF = f"hf-snapshot:{MODEL_REPO}@{MODEL_REVISION}"
MODEL_IDENTITY = f"{MODEL_REPO}@{MODEL_REVISION}"


__all__ = [
    "ENGINE_ID",
    "MODEL_DOWNLOAD_SIZE",
    "MODEL_FILES",
    "MODEL_IDENTITY",
    "MODEL_LICENSE",
    "MODEL_LICENSE_URL",
    "MODEL_REF",
    "MODEL_REPO",
    "MODEL_REVISION",
    "PACKAGE_VERSION",
    "SETUP_REF",
]
