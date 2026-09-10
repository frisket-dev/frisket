"""Fail-closed worker routing configuration.

Operators configure endpoints, credentials, and timeouts; engine identity and
capabilities remain code-owned.
"""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from frisket_models.transcription.contract import (
    TranscriptionEngineDescriptor,
)
from frisket_models.transcription.moss import (
    MOSS_DESCRIPTOR,
    MOSS_ENGINE,
    MOSS_ENV_PREFIX,
)
from frisket_models.transcription.parakeet_tdt import (
    PARAKEET_DESCRIPTOR,
    PARAKEET_ENGINE,
    PARAKEET_ENV_PREFIX,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_DESCRIPTOR,
    VIBEVOICE_ASR_ENGINE,
    VIBEVOICE_ASR_ENV_PREFIX,
)
from frisket_models.transcription.gateway import WorkerEndpoint, WorkerRegistry

DEFAULT_WORKER_TIMEOUT_SECONDS = 3600.0
DEFAULT_WORKER_PROBE_TIMEOUT_SECONDS = 5.0
MAX_WORKER_PROBE_TIMEOUT_SECONDS = 30.0
_MAX_TOKEN_BYTES = 4096
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CONFIG_SUFFIXES = (
    "URL",
    "TOKEN",
    "TOKEN_FILE",
    "TIMEOUT_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
)


@dataclass(frozen=True, slots=True)
class WorkerDefinition:
    """Code-owned descriptor, deployment prefix, and catalog policy."""

    engine: str
    env_prefix: str
    descriptor: TranscriptionEngineDescriptor
    catalog_from_config: bool = False

    def __post_init__(self) -> None:
        engine = self.engine.strip()
        prefix = self.env_prefix.strip()
        if not engine:
            raise ValueError("worker definition engine must not be empty")
        if self.descriptor.engine != engine:
            raise ValueError(
                "worker definition descriptor engine does not match engine"
            )
        if not _ENV_PREFIX_RE.fullmatch(prefix):
            raise ValueError("worker definition env_prefix is invalid")
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "env_prefix", prefix)


MOSS_DEFINITION = WorkerDefinition(
    engine=MOSS_ENGINE,
    env_prefix=MOSS_ENV_PREFIX,
    descriptor=MOSS_DESCRIPTOR,
    catalog_from_config=True,
)

PARAKEET_DEFINITION = WorkerDefinition(
    engine=PARAKEET_ENGINE,
    env_prefix=PARAKEET_ENV_PREFIX,
    descriptor=PARAKEET_DESCRIPTOR,
    catalog_from_config=True,
)

VIBEVOICE_ASR_DEFINITION = WorkerDefinition(
    engine=VIBEVOICE_ASR_ENGINE,
    env_prefix=VIBEVOICE_ASR_ENV_PREFIX,
    descriptor=VIBEVOICE_ASR_DESCRIPTOR,
    # Worker capability is authoritative; configured URL alone is not readiness.
    catalog_from_config=False,
)

# Endpoints remain opt-in through paired URL and token configuration.
PRODUCTION_WORKER_DEFINITIONS: tuple[WorkerDefinition, ...] = (
    MOSS_DEFINITION,
    PARAKEET_DEFINITION,
    VIBEVOICE_ASR_DEFINITION,
)


def worker_registry_from_env(
    *,
    definitions: Iterable[WorkerDefinition] = PRODUCTION_WORKER_DEFINITIONS,
    environ: Mapping[str, str] | None = None,
) -> WorkerRegistry:
    """Build a registry without probing or importing worker leaves."""

    source = os.environ if environ is None else environ
    endpoints: list[WorkerEndpoint] = []
    seen_engines: set[str] = set()
    seen_prefixes: set[str] = set()
    for definition in definitions:
        if definition.engine in seen_engines:
            raise ValueError(
                f"duplicate transcription worker engine {definition.engine!r}"
            )
        if definition.env_prefix in seen_prefixes:
            raise ValueError(
                f"duplicate transcription worker env prefix {definition.env_prefix!r}"
            )
        seen_engines.add(definition.engine)
        seen_prefixes.add(definition.env_prefix)
        endpoint = _endpoint_from_env(definition, source)
        if endpoint is not None:
            endpoints.append(endpoint)
    return WorkerRegistry(endpoints)


def _endpoint_from_env(
    definition: WorkerDefinition,
    environ: Mapping[str, str],
) -> WorkerEndpoint | None:
    prefix = definition.env_prefix
    keys = {suffix: f"{prefix}_{suffix}" for suffix in _CONFIG_SUFFIXES}
    present = {suffix for suffix, key in keys.items() if key in environ}
    if not present:
        return None

    url_key = keys["URL"]
    if "URL" not in present:
        raise ValueError(
            f"{url_key} must be set when any {prefix}_* setting is configured"
        )
    url = environ[url_key]
    if not url:
        raise ValueError(f"{url_key} must not be empty")

    token_present = "TOKEN" in present
    token_file_present = "TOKEN_FILE" in present
    token_key = keys["TOKEN"]
    token_file_key = keys["TOKEN_FILE"]
    if token_present == token_file_present:
        if not token_present:
            raise ValueError(
                f"{url_key} is set but no worker token source is configured"
            )
        raise ValueError(f"set exactly one of {token_key} and {token_file_key}")
    if token_present:
        token = _validate_token(environ[token_key], source_name=token_key)
    else:
        token = _read_token_file(environ[token_file_key], source_name=token_file_key)

    timeout = _timeout_from_env(
        environ,
        keys["TIMEOUT_SECONDS"],
        default=DEFAULT_WORKER_TIMEOUT_SECONDS,
    )
    probe_timeout = _timeout_from_env(
        environ,
        keys["PROBE_TIMEOUT_SECONDS"],
        default=DEFAULT_WORKER_PROBE_TIMEOUT_SECONDS,
        maximum=MAX_WORKER_PROBE_TIMEOUT_SECONDS,
    )

    try:
        return WorkerEndpoint(
            expected_engine=definition.engine,
            base_url=url,
            token=token,
            descriptor=definition.descriptor,
            timeout_seconds=timeout,
            probe_timeout_seconds=probe_timeout,
            catalog_from_config=definition.catalog_from_config,
        )
    except ValueError as exc:
        if str(exc).startswith("base_url"):
            raise ValueError(
                f"{url_key} must be an absolute credential-free http(s) root origin"
            ) from None
        raise


def _timeout_from_env(
    environ: Mapping[str, str],
    key: str,
    *,
    default: float,
    maximum: float | None = None,
) -> float:
    if key not in environ:
        return default
    raw = environ[key]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a positive finite number")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} must not exceed {maximum:g}")
    return value


def _read_token_file(path_value: str, *, source_name: str) -> str:
    if not path_value:
        raise ValueError(f"{source_name} could not be read as a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path_value, flags)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAX_TOKEN_BYTES
            ):
                raise OSError
            chunks: list[bytes] = []
            remaining = _MAX_TOKEN_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAX_TOKEN_BYTES:
                raise OSError
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        raise ValueError(f"{source_name} could not be read as a regular file") from None

    if payload.endswith(b"\r\n"):
        payload = payload[:-2]
    elif payload.endswith(b"\n"):
        payload = payload[:-1]
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"{source_name} contains an invalid bearer token") from None
    return _validate_token(value, source_name=source_name)


def _validate_token(value: str, *, source_name: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError(f"{source_name} contains an invalid bearer token")
    return value


__all__ = [
    "DEFAULT_WORKER_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_WORKER_TIMEOUT_SECONDS",
    "MAX_WORKER_PROBE_TIMEOUT_SECONDS",
    "MOSS_DEFINITION",
    "PARAKEET_DEFINITION",
    "VIBEVOICE_ASR_DEFINITION",
    "PRODUCTION_WORKER_DEFINITIONS",
    "WorkerDefinition",
    "worker_registry_from_env",
]
