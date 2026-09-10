"""Local-tier provider key config, model catalog, and validate probes.

The hosted tier keeps encrypted ``org_secrets``; the local tier has no
account, so UI-entered provider keys persist to a gitignored workspace config
file (``<workspace>/.frisket/provider_keys.json``, owner-only perms + a
belt-and-suspenders ``.gitignore``). Keys load at startup through
:func:`resolve_effective_keys`; environment variables win on conflict so a
shell-exported key always overrides a file-stored one.

Model/pricing facts come from the live pricing table (``frisket.llm.pricing``);
unknown prices surface as ``None``, never a fabricated number. The validate
probe issues the cheapest possible real request per provider (a models
listing — no tokens billed) and never returns the key value to the caller.
Local-server reachability is probed for real for each configured endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from filelock import FileLock

from frisket.ai.llm import pricing
from frisket.ai.llm.endpoint_config import (
    LocalModelEndpointConfig,
    normalize_ollama_url,
    origin_is_https_or_loopback,
    resolve_env_local_endpoint,
)
from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.local_model_ids import format_local_model_id, validate_local_endpoint_id
from frisket.redaction import redact_text
from frisket.team.security.secrets import key_hint

# Env parsing and origin validation live in the LLM layer so server, worker,
# and team composition share one canonical endpoint record and model grammar.

# Providers that take an API key the local UI can manage.
KEY_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini", "openrouter")

PROVIDER_ORDER: tuple[str, ...] = (*KEY_PROVIDERS, "ollama")

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    # Qualified model IDs retain the ``ollama`` provider segment, while each
    # endpoint has its own ordinary endpoint_id. The slot supports any
    # OpenAI-compatible local server — Ollama, LM Studio, llama.cpp, or vLLM.
    "ollama": "Local server",
}

# API-key env var per provider — the source that wins over the file layer.
ENV_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Capability-contract provider_kind (mirrors models/metadata.py PROVIDER_KIND):
# remote hosted APIs are platform_api; ollama is an operator-local HTTP server.
PROVIDER_KIND: dict[str, str] = {
    "anthropic": "platform_api",
    "openai": "platform_api",
    "gemini": "platform_api",
    "openrouter": "platform_api",
    "ollama": "local_http",
}

# Probe endpoints: base URLs the router's adapters talk to. A models listing is
# the cheapest authenticated request each API offers (no tokens billed).
_PROBE_BASE: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

VALIDATION_TOKEN_TTL_SECONDS = 10 * 60
_VALIDATION_TOKEN_SECRET = secrets.token_bytes(32)


def configure_validation_token_secret(secret: bytes) -> None:
    """Bind validation receipts to one deployment's shared secret material."""
    if len(secret) < 16:
        raise ValueError("validation token signing secret is too short")
    global _VALIDATION_TOKEN_SECRET
    _VALIDATION_TOKEN_SECRET = hmac.new(
        secret, b"frisket.provider-validation.v1", hashlib.sha256
    ).digest()


class UnknownProviderError(ValueError):
    """Raised when a provider id is not one the local tier manages."""


class InvalidProviderConfigError(ValueError):
    """Raised when an unsafe mutation of the workspace config is refused."""


def normalize_provider(value: str) -> str:
    clean = (value or "").strip().lower()
    if clean not in KEY_PROVIDERS:
        raise UnknownProviderError(f"unsupported provider: {value}")
    return clean


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode())


def issue_validation_token(
    provider: str,
    key: str,
    *,
    now: float | None = None,
) -> str:
    """Return a short-lived receipt for a successful provider/key probe.

    The signature binds to the normalized provider id and the exact key text.
    Neither the key nor a reusable digest of it appears in the opaque token.
    """

    normalized = normalize_provider(provider)
    issued_at = int(time.time() if now is None else now)
    payload = {
        "provider": normalized,
        "exp": issued_at + VALIDATION_TOKEN_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(12),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signed = body + b"\0" + key.strip().encode()
    sig = hmac.new(_VALIDATION_TOKEN_SECRET, signed, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def validation_token_is_valid(
    provider: str,
    key: str,
    token: str | None,
    *,
    now: float | None = None,
) -> bool:
    if not token or "." not in token:
        return False
    try:
        body_part, sig_part = token.split(".", 1)
        body = _b64url_decode(body_part)
        sig = _b64url_decode(sig_part)
        signed = body + b"\0" + key.strip().encode()
        expected = hmac.new(_VALIDATION_TOKEN_SECRET, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, sig):
            return False
        payload = json.loads(body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    normalized = normalize_provider(provider)
    expires_at = int(payload.get("exp") or 0)
    current = int(time.time() if now is None else now)
    return payload.get("provider") == normalized and expires_at >= current


def require_validation_token(provider: str, key: str, token: str | None) -> str:
    normalized = normalize_provider(provider)
    if not validation_token_is_valid(normalized, key, token):
        raise ValueError("test this provider key successfully before saving")
    return normalized


# ---------------------------------------------------------------------------
# gitignored workspace key file
# ---------------------------------------------------------------------------


def local_secrets_path(root: str | Path) -> Path:
    return Path(root) / ".frisket" / "provider_keys.json"


def _ensure_secret_dir(path: Path) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        # Belt-and-suspenders: even if the workspace itself is a git repo, the
        # secrets never get committed.
        gitignore.write_text("*\n")


def _read_config_file(root: str | Path) -> dict[str, str]:
    """Return the raw config to tolerant readers, or ``{}`` when invalid.

    Mutations deliberately use the strict decoder inside
    :func:`_mutate_config_file`; tolerant reads must never become a lossy
    preimage for a later write.
    """
    path = local_secrets_path(root)
    try:
        return _read_valid_config(path)
    except InvalidProviderConfigError:
        return {}


def _read_valid_config(path: Path) -> dict[str, str]:
    """Decode the complete, losslessly round-trippable config at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}"
        ) from exc
    except OSError as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update provider settings at {path}"
        ) from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}"
        ) from exc
    return _validate_config_shape(data, path=path)


def _validate_config_shape(data: Any, *, path: Path) -> dict[str, str]:
    """Return ``data`` only when JSON serialization cannot filter or coerce it."""
    if not isinstance(data, dict):
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}: "
            "expected a JSON object"
        )
    if any(
        not isinstance(name, str) or not isinstance(value, str) or not value
        for name, value in data.items()
    ):
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}: "
            "every name must be a string and every value a non-empty string"
        )
    return dict(data)


def load_local_provider_keys(root: str | Path) -> dict[str, str]:
    return {
        provider: value
        for provider, value in _read_config_file(root).items()
        if provider in KEY_PROVIDERS
    }


_MutationResult = TypeVar("_MutationResult")


def _mutate_config_file(
    root: str | Path,
    mutation: Callable[[dict[str, str]], tuple[_MutationResult, bool]],
) -> _MutationResult:
    """Run one lossless read-modify-publish cycle under a process-safe lock."""
    path = local_secrets_path(root)
    _ensure_secret_dir(path)
    lock_path = path.with_name(f".{path.name}.lock")
    with FileLock(str(lock_path), timeout=-1):
        config = _read_valid_config(path)
        result, changed = mutation(config)
        config = _validate_config_shape(config, path=path)
        if not changed:
            return result

        fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            handle = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with handle:
                handle.write(json.dumps(config, indent=2))
            os.replace(temp_name, path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return result


def save_local_provider_key(root: str | Path, provider: str, key: str) -> None:
    provider = normalize_provider(provider)
    if not (key or "").strip():
        raise ValueError("provider key is required")

    def save(config: dict[str, str]) -> tuple[None, bool]:
        config[provider] = key.strip()
        return None, True

    _mutate_config_file(root, save)


def delete_local_provider_key(root: str | Path, provider: str) -> bool:
    provider = normalize_provider(provider)

    def delete(config: dict[str, str]) -> tuple[bool, bool]:
        if provider not in config:
            return False, False
        del config[provider]
        return True, True

    return _mutate_config_file(root, delete)


LOCAL_MODEL_ENDPOINTS_KEY = "local_model_endpoints_v1"
MAX_LOCAL_ENDPOINTS = 16
LOCAL_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 0.5
LOCAL_ENDPOINT_DISCOVERY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Ollama", "http://localhost:11434"),
    ("LM Studio", "http://localhost:1234"),
    ("llama.cpp", "http://localhost:8080"),
    ("vLLM", "http://localhost:8000"),
)


def _normalize_local_endpoint_name(name: str) -> str:
    normalized = " ".join((name or "").split())
    if not normalized:
        raise ValueError("local model server name is required")
    if len(normalized) > 80:
        raise ValueError("local model server name is too long")
    return normalized


def _decode_local_endpoints(
    value: str | None,
    *,
    strict: bool,
) -> list[dict[str, str]]:
    if value is None:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise InvalidProviderConfigError(
                "cannot safely update malformed local model endpoint settings"
            ) from exc
        return []
    valid = isinstance(decoded, list) and len(decoded) <= MAX_LOCAL_ENDPOINTS
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if valid:
        for raw in decoded:
            if not isinstance(raw, dict) or set(raw) != {
                "id",
                "name",
                "url",
                "inference_token",
                "provisioning_token",
                "edge_auth",
                "pull_enabled",
            }:
                valid = False
                break
            endpoint_id = raw.get("id")
            name = raw.get("name")
            url = raw.get("url")
            if (
                not isinstance(endpoint_id, str)
                or endpoint_id in seen_ids
                or not isinstance(name, str)
                or not isinstance(url, str)
                or raw.get("inference_token") is not None
                and not isinstance(raw.get("inference_token"), str)
                or raw.get("provisioning_token") is not None
                and not isinstance(raw.get("provisioning_token"), str)
                or not isinstance(raw.get("edge_auth"), bool)
                or not isinstance(raw.get("pull_enabled"), bool)
            ):
                valid = False
                break
            try:
                endpoint_id = validate_local_endpoint_id(endpoint_id)
                normalized_name = _normalize_local_endpoint_name(name)
                normalized_url = normalize_ollama_url(url)
            except ValueError:
                valid = False
                break
            seen_ids.add(endpoint_id)
            inference_token = raw.get("inference_token") or None
            provisioning_token = raw.get("provisioning_token") or None
            if (
                inference_token or provisioning_token
            ) and not origin_is_https_or_loopback(normalized_url):
                valid = False
                break
            rows.append(
                {
                    "id": endpoint_id,
                    "name": normalized_name,
                    "url": normalized_url,
                    "inference_token": inference_token,
                    "provisioning_token": provisioning_token,
                    "edge_auth": raw["edge_auth"],
                    "pull_enabled": raw["pull_enabled"],
                }
            )
    if not valid:
        if strict:
            raise InvalidProviderConfigError(
                "cannot safely update malformed local model endpoint settings"
            )
        return []
    return rows


def load_local_endpoints(
    root: str | Path,
) -> tuple[LocalModelEndpointConfig, ...]:
    rows = _decode_local_endpoints(
        _read_config_file(root).get(LOCAL_MODEL_ENDPOINTS_KEY),
        strict=False,
    )
    return tuple(
        LocalModelEndpointConfig(
            origin=row["url"],
            inference_token=row["inference_token"],
            provisioning_token=row["provisioning_token"],
            edge_auth=row["edge_auth"],
            pull_enabled=row["pull_enabled"],
            source="local_file",
            endpoint_id=row["id"],
            display_name=row["name"],
        )
        for row in rows
    )


def create_local_endpoint(
    root: str | Path,
    *,
    name: str,
    url: str,
    inference_token: str | None = None,
    provisioning_token: str | None = None,
    edge_auth: bool = False,
    pull_enabled: bool = False,
) -> LocalModelEndpointConfig:
    normalized_name = _normalize_local_endpoint_name(name)
    normalized_url = normalize_ollama_url(url)
    env_endpoint, _notes = resolve_env_local_endpoint()
    if env_endpoint is not None and env_endpoint.origin == normalized_url:
        raise ValueError("an environment local endpoint already uses that URL")
    if (inference_token or provisioning_token) and not origin_is_https_or_loopback(
        normalized_url
    ):
        raise ValueError("a token-bearing origin must be https (or loopback)")

    def save(config: dict[str, str]) -> tuple[LocalModelEndpointConfig, bool]:
        rows = _decode_local_endpoints(
            config.get(LOCAL_MODEL_ENDPOINTS_KEY),
            strict=True,
        )
        if any(row["url"] == normalized_url for row in rows):
            raise ValueError("a local model server already uses that URL")
        if len(rows) >= MAX_LOCAL_ENDPOINTS:
            raise ValueError("too many local model servers configured")
        selected_id = f"local-{secrets.token_hex(6)}"
        rows.append(
            {
                "id": selected_id,
                "name": normalized_name,
                "url": normalized_url,
                "inference_token": inference_token,
                "provisioning_token": provisioning_token,
                "edge_auth": edge_auth,
                "pull_enabled": pull_enabled,
            }
        )
        config[LOCAL_MODEL_ENDPOINTS_KEY] = json.dumps(
            rows,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            LocalModelEndpointConfig(
                origin=normalized_url,
                inference_token=inference_token,
                provisioning_token=provisioning_token,
                edge_auth=edge_auth,
                pull_enabled=pull_enabled,
                source="local_file",
                endpoint_id=selected_id,
                display_name=normalized_name,
            ),
            True,
        )

    return _mutate_config_file(root, save)


def patch_local_endpoint(
    root: str | Path,
    endpoint_id: str,
    changes: dict[str, Any],
) -> LocalModelEndpointConfig:
    """Atomically patch one persisted endpoint while preserving omitted fields."""
    endpoint_id = validate_local_endpoint_id(endpoint_id)
    allowed = {
        "display_name",
        "inference_token",
        "provisioning_token",
        "edge_auth",
        "pull_enabled",
    }
    if set(changes) - allowed:
        raise ValueError("unknown local endpoint patch field")

    def patch(config: dict[str, str]) -> tuple[LocalModelEndpointConfig, bool]:
        rows = _decode_local_endpoints(
            config.get(LOCAL_MODEL_ENDPOINTS_KEY), strict=True
        )
        index = next(
            (i for i, row in enumerate(rows) if row["id"] == endpoint_id), None
        )
        if index is None:
            raise KeyError(endpoint_id)
        current = rows[index]
        name = (
            _normalize_local_endpoint_name(changes["display_name"])
            if "display_name" in changes
            else current["name"]
        )
        origin = current["url"]
        inference_token = changes.get("inference_token", current["inference_token"])
        provisioning_token = changes.get(
            "provisioning_token", current["provisioning_token"]
        )
        edge_auth = changes.get("edge_auth", current["edge_auth"])
        pull_enabled = changes.get("pull_enabled", current["pull_enabled"])
        if not isinstance(edge_auth, bool) or not isinstance(pull_enabled, bool):
            raise ValueError("endpoint boolean settings cannot be null")
        if (inference_token or provisioning_token) and not origin_is_https_or_loopback(
            origin
        ):
            raise ValueError("a token-bearing origin must be https (or loopback)")
        rows[index] = {
            "id": endpoint_id,
            "name": name,
            "url": origin,
            "inference_token": inference_token,
            "provisioning_token": provisioning_token,
            "edge_auth": edge_auth,
            "pull_enabled": pull_enabled,
        }
        config[LOCAL_MODEL_ENDPOINTS_KEY] = json.dumps(
            rows, separators=(",", ":"), sort_keys=True
        )
        return (
            LocalModelEndpointConfig(
                endpoint_id=endpoint_id,
                display_name=name,
                origin=origin,
                source="local_file",
                inference_token=inference_token,
                provisioning_token=provisioning_token,
                edge_auth=edge_auth,
                pull_enabled=pull_enabled,
            ),
            True,
        )

    return _mutate_config_file(root, patch)


def delete_local_endpoint(root: str | Path, endpoint_id: str) -> bool:
    endpoint_id = validate_local_endpoint_id(endpoint_id)

    def delete(config: dict[str, str]) -> tuple[bool, bool]:
        rows = _decode_local_endpoints(
            config.get(LOCAL_MODEL_ENDPOINTS_KEY),
            strict=True,
        )
        kept = [row for row in rows if row["id"] != endpoint_id]
        if len(kept) == len(rows):
            return False, False
        if kept:
            config[LOCAL_MODEL_ENDPOINTS_KEY] = json.dumps(
                kept,
                separators=(",", ":"),
                sort_keys=True,
            )
        else:
            config.pop(LOCAL_MODEL_ENDPOINTS_KEY, None)
        return True, True

    return _mutate_config_file(root, delete)


def discover_local_endpoints(root: str | Path) -> dict[str, Any]:
    """Probe and persist the fixed localhost candidate set on explicit request.

    There is deliberately no URL parameter: this helper is not a general
    server-side fetch seam. A candidate qualifies only when the existing
    local-model probe receives a schema-valid model listing.
    """

    configured, _notes = resolve_local_endpoints(root)
    configured_origins = {endpoint.origin for endpoint in configured}
    pending = [
        (label, origin)
        for label, origin in LOCAL_ENDPOINT_DISCOVERY_CANDIDATES
        if origin not in configured_origins
    ]
    probes: dict[str, dict[str, Any]] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            results = pool.map(
                lambda candidate: ollama_reachable(
                    candidate[1],
                    timeout=LOCAL_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS,
                ),
                pending,
            )
            probes = {
                origin: result
                for (_label, origin), result in zip(pending, results, strict=True)
            }

    candidates: list[dict[str, str]] = []
    for label, origin in LOCAL_ENDPOINT_DISCOVERY_CANDIDATES:
        if origin in configured_origins:
            candidates.append(
                {"label": label, "origin": origin, "outcome": "already_added"}
            )
            continue
        probe = probes[origin]
        protocol = probe.get("protocol")
        if protocol not in ("ollama_native", "openai_compatible"):
            candidates.append(
                {"label": label, "origin": origin, "outcome": "not_found"}
            )
            continue
        try:
            create_local_endpoint(root, name=label, url=origin)
        except ValueError:
            # A concurrent discovery/add may have won after the initial read.
            current, _notes = resolve_local_endpoints(root)
            if not any(endpoint.origin == origin for endpoint in current):
                raise
            outcome = "already_added"
        else:
            outcome = "added"
        configured_origins.add(origin)
        candidates.append({"label": label, "origin": origin, "outcome": outcome})

    return {"candidates": candidates}


def resolve_local_endpoints(
    root: str | Path,
    env: dict[str, str] | None = None,
) -> tuple[tuple[LocalModelEndpointConfig, ...], list[str]]:
    """Resolve one explicit endpoint collection with no singular aliases."""
    env = os.environ if env is None else env
    env_endpoint, notes = resolve_env_local_endpoint(env)
    persisted = load_local_endpoints(root)
    endpoints = ((env_endpoint,) if env_endpoint is not None else ()) + persisted
    ids = [endpoint.endpoint_id for endpoint in endpoints]
    origins = [endpoint.origin for endpoint in endpoints]
    if len(ids) != len(set(ids)):
        raise InvalidProviderConfigError("local model endpoint ids must be unique")
    if len(origins) != len(set(origins)):
        raise InvalidProviderConfigError("local model endpoint origins must be unique")
    return endpoints, notes


# ---------------------------------------------------------------------------
def resolve_effective_keys(
    root: str | Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    """File-stored remote-provider keys with environment overrides removed."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for provider, value in load_local_provider_keys(root).items():
        env_name = ENV_VAR.get(provider)
        if env_name and env.get(env_name):
            continue
        out[provider] = value
    return out


def provider_key_status(
    root: str | Path, env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Per key-provider config status. Reveals only the key tail, never the
    value; reports whether the effective key comes from env or the file."""
    env = os.environ if env is None else env
    file_keys = load_local_provider_keys(root)
    rows: list[dict[str, Any]] = []
    for provider in KEY_PROVIDERS:
        env_value = env.get(ENV_VAR[provider])
        if env_value:
            rows.append(
                {
                    "id": provider,
                    "configured": True,
                    "source": "env",
                    "hint": key_hint(env_value),
                }
            )
        elif provider in file_keys:
            rows.append(
                {
                    "id": provider,
                    "configured": True,
                    "source": "local_file",
                    "hint": key_hint(file_keys[provider]),
                }
            )
        else:
            rows.append(
                {
                    "id": provider,
                    "configured": False,
                    "source": None,
                    "hint": None,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def _price_for(model_id: str) -> dict[str, float] | None:
    price = pricing.model_price(model_id)
    if price is None:
        return None
    return {"input": price[0], "output": price[1]}


def probe_provider(
    provider: str,
    key: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 6.0,
) -> dict[str, Any]:
    """Cheapest real validation request for a cloud provider: a models
    listing. Returns reachability + whether the key was accepted. NEVER
    includes the key value."""
    provider = (provider or "").strip().lower()
    base = _PROBE_BASE.get(provider)
    if base is None:
        return {
            "provider": provider,
            "reachable": False,
            "ok": False,
            "status": None,
            "detail": f"unsupported provider: {provider}",
        }
    url = f"{base}/models"
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}

    own = client is None
    http_client = client or httpx.Client()
    try:
        resp = http_client.get(url, headers=headers, timeout=timeout)
        detail = None
        if resp.status_code != 200:
            label = PROVIDER_LABELS.get(provider, provider)
            detail = (
                f"{label} rejected the key (HTTP {resp.status_code}). "
                "Check the provider, key value, and account access."
            )
        return {
            "provider": provider,
            "reachable": True,
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "detail": detail,
        }
    except httpx.HTTPError as exc:
        return {
            "provider": provider,
            "reachable": False,
            "ok": False,
            "status": None,
            "detail": str(exc)[:200] or type(exc).__name__,
        }
    finally:
        if own:
            http_client.close()


def _parse_tags_models(resp: httpx.Response) -> list[str] | None:
    """``{"models": [...]}``, names extracted — or None when the body does
    not have Ollama's native shape (an SPA answering 200 HTML for unknown
    paths is not native evidence). An empty list IS the valid shape: a fresh
    install with nothing pulled is still authoritatively native."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return None
    return [
        str(m.get("name"))
        for m in body["models"]
        if isinstance(m, dict) and m.get("name")
    ]


def _parse_compat_models(resp: httpx.Response) -> list[str] | None:
    """OpenAI-compat ``{"data": [{"id": ...}]}`` — or None when the body
    isn't that shape."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    return [
        str(m.get("id")) for m in body["data"] if isinstance(m, dict) and m.get("id")
    ]


def _ollama_probe_once(
    http_client: httpx.Client, base: str, *, timeout: float, headers: dict[str, str]
) -> dict[str, Any]:
    """One endpoint reachability pass, factored out so ``ollama_reachable`` can call it twice for the
    edge-auth enforcement check (once with the configured bearer, once
    deliberately tokenless) without duplicating the /api/tags -> /v1/models
    fallback chain."""
    try:
        resp = http_client.get(f"{base}/api/tags", headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        authorization = headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization else None
        return {
            "reachable": False,
            "status": None,
            "models": [],
            "detail": redact_text(
                str(exc) or "not running",
                secret_values=(token,),
                max_chars=200,
            ),
            "protocol": "unknown",
            "auth_status": "unknown",
        }
    if resp.status_code in (401, 403):
        return {
            "reachable": True,
            "status": resp.status_code,
            "models": [],
            "detail": f"server requires authentication (HTTP {resp.status_code})",
            "protocol": "unknown",
            "auth_status": "unauthorized",
        }
    if resp.status_code == 200:
        models = _parse_tags_models(resp)
        if models is not None:
            return {
                "reachable": True,
                "status": resp.status_code,
                "models": models,
                "detail": None,
                "protocol": "ollama_native",
                "auth_status": "ok",
            }
    # /api/tags unsupported or not the native shape — try the
    # OpenAI-compat listing before giving up on a model list.
    try:
        compat = http_client.get(f"{base}/v1/models", headers=headers, timeout=timeout)
    except httpx.HTTPError:
        # the /api/tags response already proved the host is up
        return {
            "reachable": True,
            "status": resp.status_code,
            "models": [],
            "detail": None,
            "protocol": "unknown",
            "auth_status": "unknown",
        }
    if compat.status_code in (401, 403):
        return {
            "reachable": True,
            "status": compat.status_code,
            "models": [],
            "detail": f"server requires authentication (HTTP {compat.status_code})",
            "protocol": "unknown",
            "auth_status": "unauthorized",
        }
    if compat.status_code == 200:
        models = _parse_compat_models(compat)
        if models is not None:
            return {
                "reachable": True,
                "status": compat.status_code,
                "models": models,
                "detail": None,
                "protocol": "openai_compatible",
                "auth_status": "ok",
            }
    return {
        "reachable": True,
        "status": resp.status_code,
        "models": [],
        "detail": None,
        "protocol": "unknown",
        "auth_status": "unknown",
    }


def ollama_reachable(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 2.0,
    token: str | None = None,
    edge_auth: bool = False,
) -> dict[str, Any]:
    """Probe a local OpenAI-compatible server, reporting capability FACTS
    separately, never inferred from "which
    endpoint happened to supply models":

    - ``reachable``: connection-level only — any HTTP response counts.
    - ``protocol``: ``ollama_native`` (``/api/tags`` answered 200 with the
      schema-valid ``{"models": [...]}`` shape, EMPTY LIST INCLUDED — a fresh
      install is still native), ``openai_compatible`` (``GET /v1/models``
      answered with a valid listing — LM Studio, llama.cpp server, vLLM;
      local-openai-compat-server-v1), or ``unknown``.
    - ``auth_status``: ``ok`` | ``unauthorized`` (HTTP 401/403 — its own
      state with its own copy, NOT "reachable with no models") | ``unenforced``
      (a token is configured, ``edge_auth`` says a front door should check it,
      AND a deliberately-tokenless request ALSO succeeded: the door isn't
      actually enforcing anything, you are probably talking straight to the
      daemon) | ``unknown``.
    - ``models``: the honest list; empty means empty, never "didn't look".
    - ``token_configured``: whether a bearer was sent at all (catalog fact).

    ``token`` (when given) rides every request as ``Authorization: Bearer
    <token>`` — tokenless callers get byte-identical behavior to before
    (no header sent at all). ``edge_auth=True`` with a token additionally
    fires the tokenless deliberate-reject check ONLY when the tokened
    request itself came back ``auth_status: "ok"`` — an already-failing
    tokened call has nothing to compare against.
    """
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    own = client is None
    http_client = client or httpx.Client()
    try:
        result = _ollama_probe_once(http_client, base, timeout=timeout, headers=headers)
        result["token_configured"] = bool(token)
        if token and edge_auth and result.get("auth_status") == "ok":
            tokenless = _ollama_probe_once(
                http_client, base, timeout=timeout, headers={}
            )
            if tokenless.get("auth_status") == "ok":
                result["auth_status"] = "unenforced"
                result["detail"] = (
                    "a local-model token is configured but the endpoint "
                    "accepted a request with NO token — the front door "
                    "isn't enforcing auth; you may be talking directly to "
                    "the daemon"
                )
        return result
    finally:
        if own:
            http_client.close()


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def local_endpoint_catalog_entry(
    root: str | Path,
    endpoint: LocalModelEndpointConfig,
    env: dict[str, str],
    *,
    authority: Literal["instance", "organization"],
) -> dict[str, Any]:
    probe = ollama_reachable(
        endpoint.origin,
        token=endpoint.inference_token,
        edge_auth=endpoint.edge_auth,
    )
    entry: dict[str, Any] = {
        "endpoint_id": endpoint.endpoint_id,
        "label": endpoint.display_name,
        "kind": "local_http",
        "read_only": endpoint.source != "local_file",
        "models": [],
        "reachable": bool(probe.get("reachable")),
        "origin": endpoint.origin,
        "authority": authority,
        "source": "environment" if endpoint.source == "env" else "stored",
        "detail": probe.get("detail"),
        "protocol": probe.get("protocol", "unknown"),
        "auth_status": probe.get("auth_status", "unknown"),
        "token_configured": bool(endpoint.inference_token),
        "provisioning_token_configured": bool(endpoint.provisioning_token),
        "edge_auth": endpoint.edge_auth,
    }
    entry["pull_enabled"] = endpoint.pull_enabled
    installed = probe.get("models") or []
    authoritative = entry["protocol"] in ("ollama_native", "openai_compatible")
    if authoritative or installed:
        entry["installed_models"] = installed
        entry["models"] = [
            {
                "id": format_local_model_id(endpoint.endpoint_id, name),
                "label": name,
                "price": _price_for(format_local_model_id(endpoint.endpoint_id, name)),
                "local": True,
            }
            for name in installed
        ]
    return entry


def build_provider_catalog(
    root: str | Path,
    env: dict[str, str] | None = None,
    *,
    network_off: bool = False,
) -> dict[str, Any]:
    """The Braintrust-style picker's source of truth: providers grouped with
    their models (priced from the live table, unknown = None), configured
    status (env/file), and a real Ollama reachability badge.

    ``network_off`` (the project's effective network policy, threaded from
    the route's ``project_id`` param): remote providers (``platform_api``)
    are omitted so the picker only offers what dispatch would accept — a UX
    filter; the authoritative gate is at validate/dispatch time."""
    env = os.environ if env is None else env
    status = {row["id"]: row for row in provider_key_status(root, env)}
    providers: list[dict[str, Any]] = []
    for provider in PROVIDER_ORDER:
        if network_off and PROVIDER_KIND.get(provider) != "local_http":
            continue
        if provider == "ollama":
            local_endpoints, _endpoint_notes = resolve_local_endpoints(root, env)
            if local_endpoints:
                # Each endpoint probe has its own timeout and HTTP client.
                # Bound parallelism keeps one offline server from serially
                # multiplying picker/settings latency across the collection.
                with ThreadPoolExecutor(
                    max_workers=min(4, len(local_endpoints))
                ) as pool:
                    providers.extend(
                        pool.map(
                            lambda endpoint: local_endpoint_catalog_entry(
                                root,
                                endpoint,
                                env,
                                authority="instance",
                            ),
                            local_endpoints,
                        )
                    )
            continue
        models = [
            {
                "id": f"{provider}/{model['id']}",
                "label": model["label"],
                "price": _price_for(f"{provider}/{model['id']}"),
                "local": provider == "ollama",
            }
            for model in MODEL_ENTRIES.get(provider, [])
        ]
        entry: dict[str, Any] = {
            "id": provider,
            "label": PROVIDER_LABELS[provider],
            "kind": PROVIDER_KIND[provider],
            "models": models,
        }
        row = status.get(provider, {})
        entry["configured"] = bool(row.get("configured"))
        entry["source"] = row.get("source")
        entry["hint"] = row.get("hint")
        providers.append(entry)
    payload: dict[str, Any] = {
        "schemaVersion": "frisket.providers.v1",
        "tier": "local",
        "providers": providers,
    }
    if network_off:
        # Honest marker for the picker's copy: filtered by the project's
        # network setting, not by missing keys.
        payload["network"] = "off"
    return payload
