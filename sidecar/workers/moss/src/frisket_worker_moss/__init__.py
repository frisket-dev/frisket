"""MOSS-Transcribe-Diarize worker leaf (Boundary C over native server D).

Re-exports the lazy worker :data:`REGISTRATION` that
``frisket_worker_moss.app`` binds to the live worker runtime, plus its reusable
pieces. Importing this package pulls in no torch, vLLM, or model weights.
"""

from __future__ import annotations

from .adapter import (
    DESCRIPTOR,
    REGISTRATION,
    MossAdapter,
    MossConfig,
    build_registration,
)
from .constants import ENGINE, MODEL_ID, MODEL_REVISION
from .transcript import MossTranscriptError, ParsedSegment, parse_moss_transcript

__all__ = [
    "DESCRIPTOR",
    "ENGINE",
    "MODEL_ID",
    "MODEL_REVISION",
    "MossAdapter",
    "MossConfig",
    "MossTranscriptError",
    "ParsedSegment",
    "REGISTRATION",
    "build_registration",
    "parse_moss_transcript",
]
