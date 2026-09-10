"""Validated operator configuration for the open team server."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

SAFE_OIDC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(hostname: str | None) -> bool:
    if hostname in {"localhost", "testserver"}:
        return True
    try:
        return bool(hostname and ipaddress.ip_address(hostname).is_loopback)
    except ValueError:
        return False


def _https_endpoint(value: str, *, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{label} must be an absolute credential-free https URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise ValueError(f"{label} must not target a private or loopback address")
    if parsed.fragment:
        raise ValueError(f"{label} must not contain a fragment")
    if parsed.query:
        raise ValueError(f"{label} must not contain a query string")
    return value


@dataclass(frozen=True)
class OIDCProviderConfig:
    issuer: str
    client_id: str
    client_secret: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    algorithms: tuple[str, ...] = ("RS256", "ES256")

    @classmethod
    def from_value(
        cls, value: "OIDCProviderConfig | dict[str, Any]"
    ) -> "OIDCProviderConfig":
        if isinstance(value, cls):
            return value
        raw_scopes = value.get("scopes", ("openid", "email", "profile"))
        if isinstance(raw_scopes, str):
            raise ValueError("OIDC scopes must be a sequence, not a string")
        return cls(
            issuer=str(value["issuer"]),
            client_id=str(value["client_id"]),
            client_secret=str(value["client_secret"]),
            authorization_endpoint=str(value["authorization_endpoint"]),
            token_endpoint=str(value["token_endpoint"]),
            jwks_uri=str(value["jwks_uri"]),
            scopes=tuple(raw_scopes),
            algorithms=tuple(value.get("algorithms", ("RS256", "ES256"))),
        )

    def validate(self, provider: str) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError(f"OIDC provider {provider!r} requires client credentials")
        for label, value in (
            ("issuer", self.issuer),
            ("authorization_endpoint", self.authorization_endpoint),
            ("token_endpoint", self.token_endpoint),
            ("jwks_uri", self.jwks_uri),
        ):
            _https_endpoint(value, label=f"OIDC provider {provider!r} {label}")
        if not self.algorithms or not set(self.algorithms) <= SAFE_OIDC_ALGORITHMS:
            raise ValueError(
                f"OIDC provider {provider!r} has an unsafe signing algorithm"
            )
        if (
            "openid" not in self.scopes
            or not self.scopes
            or any(
                not isinstance(scope, str) or not scope.strip() for scope in self.scopes
            )
        ):
            raise ValueError(
                f"OIDC provider {provider!r} scopes must be a sequence containing openid"
            )


@dataclass(frozen=True)
class TeamConfig:
    database_url: str
    data_dir: Path
    base_url: str
    organization_name: str
    # The compose worker (docker-compose.yml's `worker` service, and
    # `frisket worker`/`frisket hosted-worker` via cli.py's `_run_worker`)
    # resolves its run-queue Postgres locator from
    # `FRISKET_RUN_QUEUE_DATABASE_URL`, never from `database_url` above (that
    # field is the CONTROL-PLANE schema locator, `FRISKET_TEAM_DATABASE_URL`).
    # This field carries the SAME locator the worker CLI reads, resolved the
    # SAME way (see `team_config_from_env`). Every queued job kind (not just
    # model pulls) enqueues through this locator, so `None` is rejected
    # unconditionally at construction time -- there is no supported
    # deployment where this field is legitimately absent.
    run_queue_database_url: str | None = None
    admin_emails: set[str] = field(default_factory=set)
    oidc_providers: dict[str, OIDCProviderConfig | dict[str, Any]] = field(
        default_factory=dict
    )
    trusted_proxy_cidrs: tuple[str, ...] = ()
    magic_link_enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    secrets_key_file: Path | None = None
    secrets_master_key: str | None = None
    # Connected-account OAuth is deliberately separate from OIDC sign-in.
    # These credentials authorize a user's Google Sheets connection; they do
    # not replace the team's browser sign-in channels.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_connection_redirect_uri: str | None = None
    static_dir: Path | None = None
    client_error_capture: bool = False
    map_tile_url_template: str = ""
    map_api_key: str = ""
    map_attribution: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "team base_url must be an absolute credential-free http(s) origin"
            )
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            # The canonical text lives in
            # frisket.team.diagnostics.BOOT_FAILURE_CATALOG
            # (PLAIN_HTTP_BASE_URL_REJECTED) so this raise and any future
            # translated-error surface (e.g. a startup-health endpoint) say
            # the exact same thing.
            from frisket.team.diagnostics import (
                PLAIN_HTTP_BASE_URL_REJECTED,
                BOOT_FAILURE_CATALOG,
            )

            entry = BOOT_FAILURE_CATALOG[PLAIN_HTTP_BASE_URL_REJECTED]
            raise ValueError(f"{entry['what']} {entry['todo']}")
        canonical = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                "",
                "",
                "",
                "",
            )
        )
        object.__setattr__(self, "base_url", canonical)
        redirect_uri = self.google_connection_redirect_uri or (
            f"{canonical}/api/org/oauth/google/callback"
        )
        redirect = urlparse(redirect_uri)
        if (
            redirect.scheme not in {"http", "https"}
            or not redirect.netloc
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError(
                "google_connection_redirect_uri must be an absolute credential-free http(s) URL"
            )
        if redirect.scheme == "http" and not _is_loopback(redirect.hostname):
            raise ValueError(
                "plain-http google_connection_redirect_uri is allowed only for loopback development"
            )
        object.__setattr__(self, "google_connection_redirect_uri", redirect_uri)
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.static_dir is not None:
            object.__setattr__(self, "static_dir", Path(self.static_dir).resolve())
        for field_name in (
            "map_tile_url_template",
            "map_api_key",
            "map_attribution",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name)).strip())
        object.__setattr__(
            self,
            "admin_emails",
            {email.lower().strip() for email in self.admin_emails if email.strip()},
        )
        object.__setattr__(
            self,
            "oidc_providers",
            {
                key: OIDCProviderConfig.from_value(value)
                for key, value in self.oidc_providers.items()
            },
        )
        from frisket.team.auth_limits import trusted_proxy_networks

        object.__setattr__(
            self,
            "trusted_proxy_cidrs",
            tuple(
                str(network)
                for network in trusted_proxy_networks(self.trusted_proxy_cidrs)
            ),
        )
        if self.secrets_key_file is None:
            object.__setattr__(
                self, "secrets_key_file", self.data_dir / "secrets" / "master.key"
            )

    @property
    def secure_cookies(self) -> bool:
        return urlparse(self.base_url).scheme == "https"

    def validate(
        self, *, magic_transport_injected: bool, oidc_exchange_injected: bool
    ) -> None:
        if not self.organization_name.strip():
            raise ValueError("organization_name is required")
        # Local-password setup is always available on an unclaimed server.
        # SMTP/OIDC are optional enhancements, never boot prerequisites.
        if self.smtp_username and not (self.smtp_starttls or self.smtp_ssl):
            raise ValueError("SMTP credentials require TLS")
        if (
            self.smtp_host
            and not _is_loopback(self.smtp_host)
            and not (self.smtp_starttls or self.smtp_ssl)
        ):
            raise ValueError("non-loopback SMTP requires verified STARTTLS or SSL")
        if self.smtp_starttls and self.smtp_ssl:
            raise ValueError("choose SMTP STARTTLS or implicit SSL, not both")
        if bool(self.google_client_id) != bool(self.google_client_secret):
            raise ValueError(
                "Google connected-account OAuth requires both GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET"
            )
        for provider, definition in self.oidc_providers.items():
            if not provider or any(
                ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in provider
            ):
                raise ValueError(f"invalid OIDC provider id: {provider!r}")
            definition.validate(provider)


def team_config_from_env(env: dict[str, str] | None = None) -> TeamConfig:
    values = os.environ if env is None else env
    providers = json.loads(values.get("FRISKET_OIDC_PROVIDERS_JSON", "{}"))
    if not isinstance(providers, dict):
        raise ValueError("FRISKET_OIDC_PROVIDERS_JSON must be an object")
    return TeamConfig(
        database_url=values.get(
            "FRISKET_TEAM_DATABASE_URL", "sqlite:///frisket-team.db"
        ),
        # Same env var, same precedence as cli.py's `_run_worker` (no
        # positional workspace there either -- the team edition always runs
        # database-backed). `create_team_app` now requires this
        # unconditionally; `None` when absent is deliberately still passed
        # through here (rather than defaulted) so the constructor's own
        # unconditional check is the single place that raises.
        run_queue_database_url=values.get("FRISKET_RUN_QUEUE_DATABASE_URL") or None,
        data_dir=Path(values.get("FRISKET_DATA_DIR", "./frisket-data")),
        base_url=values.get("FRISKET_BASE_URL", "http://127.0.0.1:8000"),
        organization_name=values.get("FRISKET_ORGANIZATION_NAME", "frisket"),
        admin_emails={
            email
            for email in values.get("FRISKET_ADMIN_EMAILS", "").split(",")
            if email.strip()
        },
        oidc_providers=providers,
        trusted_proxy_cidrs=tuple(
            value.strip()
            for value in values.get("FRISKET_TRUSTED_PROXY_CIDRS", "").split(",")
            if value.strip()
        ),
        # Local password auth is the zero-dependency server default.  External
        # channels are opt-in so a fresh image never pretends email works just
        # because the operator omitted SMTP configuration.
        magic_link_enabled=values.get("FRISKET_MAGIC_LINK_ENABLED", "false").lower()
        in {"1", "true", "yes"},
        smtp_host=values.get("FRISKET_SMTP_HOST"),
        smtp_port=int(values.get("FRISKET_SMTP_PORT", "587")),
        smtp_username=values.get("FRISKET_SMTP_USERNAME"),
        smtp_password=values.get("FRISKET_SMTP_PASSWORD"),
        smtp_from=values.get("FRISKET_SMTP_FROM"),
        smtp_starttls=values.get("FRISKET_SMTP_STARTTLS", "true").lower()
        in {"1", "true", "yes"},
        smtp_ssl=values.get("FRISKET_SMTP_SSL", "false").lower()
        in {"1", "true", "yes"},
        secrets_key_file=Path(values["FRISKET_SECRETS_KEY_FILE"])
        if values.get("FRISKET_SECRETS_KEY_FILE")
        else None,
        secrets_master_key=values.get("FRISKET_SECRETS_MASTER_KEY"),
        google_client_id=values.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        google_client_secret=values.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        google_connection_redirect_uri=values.get(
            "GOOGLE_OAUTH_CONNECTION_REDIRECT_URI"
        ),
        static_dir=(
            Path(values["FRISKET_STATIC_DIR"])
            if values.get("FRISKET_STATIC_DIR", "").strip()
            else None
        ),
        client_error_capture=_truthy(values.get("FRISKET_CLIENT_ERROR_CAPTURE")),
        map_tile_url_template=values.get("FRISKET_MAP_TILE_URL_TEMPLATE", ""),
        map_api_key=values.get("FRISKET_MAP_API_KEY", ""),
        map_attribution=values.get("FRISKET_MAP_ATTRIBUTION", ""),
    )


__all__ = [
    "OIDCProviderConfig",
    "SAFE_OIDC_ALGORITHMS",
    "TeamConfig",
    "team_config_from_env",
]
