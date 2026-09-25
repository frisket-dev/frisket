"""Shared default model selection for persisted Project Ask turns."""

from __future__ import annotations

from frisket.ai.llm import ModelRouter


PROJECT_ASK_MODEL = "anthropic/claude-sonnet-5"

_PROJECT_ASK_MODEL_BY_PROVIDER: tuple[tuple[str, str], ...] = (
    ("anthropic", PROJECT_ASK_MODEL),
    ("openai", "openai/gpt-5.6-terra"),
    ("gemini", "gemini/gemini-3.6-flash"),
)


def default_project_ask_model(router: ModelRouter) -> str:
    """Choose the configured provider's Project Ask default model."""

    configured = router.configured_keys()
    for provider, model in _PROJECT_ASK_MODEL_BY_PROVIDER:
        if provider in configured:
            return model
    return PROJECT_ASK_MODEL
