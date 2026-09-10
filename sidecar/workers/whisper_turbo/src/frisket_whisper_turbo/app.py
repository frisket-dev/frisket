"""Fail-closed HTTP runtime for the fixed Whisper Turbo worker."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI
from frisket_models.transcription import AdapterRegistration
from frisket_models.transcription.worker import (
    DEFAULT_MAX_UPLOAD_BYTES,
    create_worker_app,
)

from .adapter import REGISTRATION

_TOKEN_ENV = "FRISKET_TRANSCRIPTION_WORKER_TOKEN"
_CONCURRENCY_ENV = "FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY"
_MAX_UPLOAD_ENV = "FRISKET_TRANSCRIPTION_WORKER_MAX_UPLOAD_BYTES"
_SPOOL_DIR_ENV = "FRISKET_TRANSCRIPTION_WORKER_SPOOL_DIR"
_RUNTIME_IMAGE_ID_ENV = "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"


def _required_token(environ: Mapping[str, str]) -> str:
    token = environ.get(_TOKEN_ENV, "")
    if (
        not token
        or token != token.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise ValueError(f"{_TOKEN_ENV} must be a non-empty printable bearer token")
    return token


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1 or raw.strip() != str(value):
        raise ValueError(f"{name} must be a canonical positive integer")
    return value


def create_app(
    *,
    registration: AdapterRegistration | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Create the authenticated, one-request-at-a-time worker."""

    source = os.environ if environ is None else environ
    token = _required_token(source)
    concurrency = _positive_int(source, _CONCURRENCY_ENV, 1)
    if concurrency != 1:
        raise ValueError(f"{_CONCURRENCY_ENV} must be 1 for the Whisper Turbo worker")
    max_upload_bytes = _positive_int(source, _MAX_UPLOAD_ENV, DEFAULT_MAX_UPLOAD_BYTES)
    spool_dir_raw = source.get(_SPOOL_DIR_ENV)
    spool_dir = Path(spool_dir_raw) if spool_dir_raw else None

    return create_worker_app(
        registration or REGISTRATION,
        token=token,
        concurrency=concurrency,
        max_upload_bytes=max_upload_bytes,
        spool_dir=spool_dir,
        runtime_image_id=source.get(_RUNTIME_IMAGE_ID_ENV),
    )


__all__ = ["create_app"]
