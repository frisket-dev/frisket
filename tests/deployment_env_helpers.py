"""Shared classification of deployment environment names in tests."""

from typing import Any

# Deployment names carried by this repo's own `.env`, taken from that file
# rather than guessed. A leaked one does not merely add capability: it points
# the app somewhere real.
_DEPLOYMENT_ENV_NAMES: frozenset[str] = frozenset(
    {
        "FRISKET_BASE_URL",
        "FRISKET_BUILD_SHA",
        "FRISKET_DATABASE_ADMIN_URL",
        "FRISKET_EMAIL_FROM_ADDRESS",
        "FRISKET_EMAIL_FROM_NAME",
        "FRISKET_ASK_TRACE_CONTENT",
        "FRISKET_RUN_QUEUE_DATABASE_URL",
        "FRISKET_SECRETS_MASTER_KEY",
        "FRISKET_TEAM_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "SENTRY_DSN",
    }
)

_DEPLOYMENT_ENV_PREFIXES: tuple[str, ...] = (
    "FRISKET_MAP_",
    "GOOGLE_OAUTH_",
    "LITESTREAM_",
    "MODAL_TOKEN_",
    "OTEL_",
)

# Credentials by SHAPE rather than by vendor list: the `.env` carries ~25
# provider keys and the next one added must be covered without editing this
# file.
_CREDENTIAL_ENV_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_KEY_ID",
    "_SECRET_ACCESS_KEY",
    "_TOKEN",
)

# Test-owned selectors deliberately NOT stripped: they are how an operator opts
# INTO a heavier gate, and a stripped one would silently skip the gate it was
# set to run.
_TEST_SELECTOR_ENV_NAMES: frozenset[str] = frozenset(
    {
        "FRISKET_CHECKLOG_DISABLE",
        "FRISKET_CHECKLOG_PATH",
        "FRISKET_E2E_WEATHERAPI_KEY",
        "FRISKET_LIVE_GROUNDING_PROJECT",
        "FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS",
        "FRISKET_OCR_EXPECTED_SOURCE_SHA",
        "FRISKET_OLLAMA_LIVE",
        "FRISKET_PG_TEST_URL",
    }
)


def _is_deployment_env(name: str) -> bool:
    if name in _TEST_SELECTOR_ENV_NAMES:
        return False
    return (
        name in _DEPLOYMENT_ENV_NAMES
        or name.startswith(_DEPLOYMENT_ENV_PREFIXES)
        or name.endswith(_CREDENTIAL_ENV_SUFFIXES)
    )


def _deployment_env_names(env: Any) -> list[str]:
    return [name for name in list(env) if _is_deployment_env(name)]
