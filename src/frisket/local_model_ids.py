"""Pure canonical identity grammar for local model endpoints.

This module sits above the contracts, AI, engine, and server layers so every
consumer can share one parser without making the wire-contract package depend
on runtime implementation modules.
"""

from __future__ import annotations

import re


_ENDPOINT_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def validate_local_endpoint_id(endpoint_id: str) -> str:
    """Validate the single stable identity used by storage, HTTP and models."""
    value = (endpoint_id or "").strip()
    if _ENDPOINT_ID.fullmatch(value) is None:
        raise ValueError(
            "local endpoint id must start with a letter and contain only "
            "lowercase letters, digits, or hyphens (maximum 63 characters)"
        )
    return value


def format_local_model_id(endpoint_id: str, model: str) -> str:
    """Build the only accepted local model identity."""
    endpoint_id = validate_local_endpoint_id(endpoint_id)
    bare_model = (model or "").strip().strip("/")
    if not bare_model or bare_model.startswith("@"):
        raise ValueError("local model name is required")
    return f"ollama/@{endpoint_id}/{bare_model}"


def parse_local_model_id(model_id: str) -> tuple[str, str]:
    """Parse ``ollama/@<endpoint-id>/<model>`` and reject every alias."""
    prefix = "ollama/@"
    if not isinstance(model_id, str) or not model_id.startswith(prefix):
        raise ValueError("local model id must use ollama/@<endpoint-id>/<model>")
    endpoint_id, separator, bare_model = model_id[len(prefix) :].partition("/")
    validate_local_endpoint_id(endpoint_id)
    if not separator or not bare_model or bare_model.startswith("/"):
        raise ValueError("local model id must use ollama/@<endpoint-id>/<model>")
    return endpoint_id, bare_model


def bare_model_name(model_id: str) -> str:
    """Return the provider-wire model name using the canonical local parser."""
    provider, separator, tail = model_id.partition("/")
    if not separator:
        return provider
    if provider == "ollama":
        return parse_local_model_id(model_id)[1]
    return tail


__all__ = [
    "bare_model_name",
    "format_local_model_id",
    "parse_local_model_id",
    "validate_local_endpoint_id",
]
