"""Code-owned API-key provider facts shared by settings and execution.

Storage scopes describe existing support, not permission to use a key: edition,
role, credential ownership and execution policy remain enforced by their callers.
Local model servers have a separate endpoint configuration and do not belong here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

KeyScope = Literal["project", "workspace", "organization"]
ProviderCategory = Literal["llm", "document", "search"]


@dataclass(frozen=True)
class CredentialProbe:
    url: str
    key_header: str = "Authorization"
    key_prefix: str = "Bearer "
    headers: tuple[tuple[str, str], ...] = ()
    validate_body: Callable[[object], bool] | None = None


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    label: str
    category: ProviderCategory
    env_var: str
    scopes: tuple[KeyScope, ...]
    probe: CredentialProbe
    spend_cap: bool = False


def _datalab_healthy(body: object) -> bool:
    return isinstance(body, dict) and body.get("status") == "ok"


_LLM_SCOPES: tuple[KeyScope, ...] = ("project", "workspace", "organization")
_SEARCH_SCOPES: tuple[KeyScope, ...] = ("workspace", "organization")

# Filtering this order preserves the project, workspace and organization UI order.
PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        "anthropic",
        "Anthropic",
        "llm",
        "ANTHROPIC_API_KEY",
        _LLM_SCOPES,
        CredentialProbe(
            "https://api.anthropic.com/v1/models",
            "x-api-key",
            "",
            (("anthropic-version", "2023-06-01"),),
        ),
        spend_cap=True,
    ),
    ProviderDefinition(
        "openai",
        "OpenAI",
        "llm",
        "OPENAI_API_KEY",
        _LLM_SCOPES,
        CredentialProbe("https://api.openai.com/v1/models"),
        spend_cap=True,
    ),
    ProviderDefinition(
        "gemini",
        "Gemini",
        "llm",
        "GEMINI_API_KEY",
        _LLM_SCOPES,
        CredentialProbe(
            "https://generativelanguage.googleapis.com/v1beta/openai/models"
        ),
        spend_cap=True,
    ),
    ProviderDefinition(
        "openrouter",
        "OpenRouter",
        "llm",
        "OPENROUTER_API_KEY",
        _LLM_SCOPES,
        CredentialProbe("https://openrouter.ai/api/v1/key"),
        spend_cap=True,
    ),
    ProviderDefinition(
        "opendocrouter",
        "OpenDocRouter",
        "document",
        "OPEN_DOC_ROUTER_API_KEY",
        ("project", "organization"),
        CredentialProbe("https://www.opendocrouter.ai/v1/credits"),
    ),
    ProviderDefinition(
        "datalab",
        "Datalab",
        "document",
        "DATALAB_API_KEY",
        ("project", "organization"),
        CredentialProbe(
            "https://www.datalab.to/api/v1/user_health",
            "X-API-Key",
            "",
            validate_body=_datalab_healthy,
        ),
    ),
    ProviderDefinition(
        "exa",
        "Exa",
        "search",
        "EXA_API_KEY",
        _SEARCH_SCOPES,
        CredentialProbe("https://api.exa.ai/v0/teams/me", "x-api-key", ""),
    ),
    ProviderDefinition(
        "tavily",
        "Tavily",
        "search",
        "TAVILY_API_KEY",
        _SEARCH_SCOPES,
        CredentialProbe("https://api.tavily.com/usage"),
    ),
)


def providers_for(
    *, scope: KeyScope | None = None, category: ProviderCategory | None = None
) -> tuple[ProviderDefinition, ...]:
    return tuple(
        p
        for p in PROVIDERS
        if (scope is None or scope in p.scopes)
        and (category is None or category == p.category)
    )


def provider_definition(provider_id: str) -> ProviderDefinition | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)
