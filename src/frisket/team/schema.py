from __future__ import annotations

import os
from datetime import UTC, datetime

import sqlalchemy as sa

# This metadata owns identity and control-plane state only: identities,
# membership and access, project registration, org credentials, and
# audit/diagnostic records. Funding, quota, settlement, and admission policy
# are separate deployment concerns, attached by an outer composition through
# their own metadata keyed on these tables' stable identity IDs — never
# fused into these rows. See the table definitions and
# tests/team/test_split_bootstrap_ownership.py, which is normative for the
# table/column split.
metadata = sa.MetaData()

# Personal access tokens: the wire format is PAT_PREFIX + token_urlsafe;
# only its sha256 is persisted.
PAT_PREFIX = "frisket_pat_"

# Operator tokens and password-reset links follow the same shape: prefix +
# token_urlsafe on the wire, sha256 hex digest at rest.
OPERATOR_TOKEN_PREFIX = "frisket_operator_"
PASSWORD_RESET_PREFIX = "frisket_reset_"

DEFAULT_CLIENT_ERROR_RETENTION_DAYS = 30
DEFAULT_CLIENT_ERROR_RETENTION_MAX_ROWS = 5_000
DEFAULT_DIAGNOSTIC_REPORT_RETENTION_DAYS = 90
DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS = 1_000


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1:
        return default
    return value


users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("email", sa.String, unique=True, nullable=False),
    sa.Column("name", sa.String),
    # Null means the user has not yet answered the first-login spend prompt.
    sa.Column("cost_preapproval_usd", sa.Numeric(18, 6)),
    # Null for identities authenticated only by an optional external channel.
    # Fresh server setup writes an Argon2id hash for its local owner.
    sa.Column("password_hash", sa.String),
    sa.Column("default_org_id", sa.Integer, sa.ForeignKey("orgs.id")),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
)

# Stable external identity binding. Email is mutable provider metadata and is
# never used to re-bind an existing account after first explicit creation.
oidc_identities = sa.Table(
    "oidc_identities",
    metadata,
    sa.Column("issuer", sa.String, primary_key=True),
    sa.Column("subject", sa.String, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("email_at_link", sa.String, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.UniqueConstraint("issuer", "user_id"),
)

# Identity/branding only. Credits, grace, and quota policy are commerce state
# and live outside this tree, in a deployment's own accounting.
orgs = sa.Table(
    "orgs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    # Instance identity for third-party/self-hosted deployments. All
    # nullable — unset means "use the generic frisket default", so our own
    # hosted org is not special-cased; it is just the first configured
    # instance.
    sa.Column("display_name", sa.String),
    sa.Column("welcome_message", sa.Text),
    sa.Column("support_contact", sa.String),
    # Org-wide network default for projects whose network mode is "inherit":
    # "on" | "off"; NULL means "on". Materialized into each project bundle
    # (Project.set_network_policy org_default) the same way `sensitive`
    # syncs, because the gate reads bundle-side.
    sa.Column("network_default", sa.String),
)

memberships = sa.Table(
    "memberships",
    metadata,
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), primary_key=True),
    sa.Column("role", sa.String, nullable=False, default="owner"),
)

# Project identity. Which funding account pays for a project is commerce state
# and lives outside this tree, in a deployment's own accounting.
projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("storage_org_id", sa.Integer, sa.ForeignKey("orgs.id")),
    sa.Column("slug", sa.String, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("description", sa.String),
    sa.Column("sensitive", sa.Boolean, nullable=False, default=False),
    # Per-project network mode: "inherit" | "on" | "off". Control-plane
    # copy of the bundle's network_policy mode.
    sa.Column("network", sa.String, nullable=False, default="inherit"),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.UniqueConstraint("org_id", "slug"),
)

sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("token", sa.String, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

auth_challenges = sa.Table(
    "auth_challenges",
    metadata,
    # One-time browser secrets are high-entropy random values. Persist only
    # their SHA-256 digest; the raw value is returned once at issuance.
    sa.Column("secret_hash", sa.String(64), primary_key=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("subject_email", sa.String),
    sa.Column("subject_user_id", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column("provider", sa.String),
    # OIDC needs its independently generated nonce after state consumption so
    # the provider response can be verified. It is not itself redeemable.
    sa.Column("nonce", sa.String),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("consumed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "(kind = 'magic_link' AND subject_email IS NOT NULL "
        "AND subject_user_id IS NULL AND provider IS NULL AND nonce IS NULL) OR "
        "(kind = 'oidc_sign_in' AND subject_email IS NULL "
        "AND subject_user_id IS NULL AND provider IS NOT NULL AND nonce IS NOT NULL) OR "
        "(kind = 'connected_account_oauth' AND subject_email IS NULL "
        "AND subject_user_id IS NOT NULL AND provider IS NOT NULL AND nonce IS NULL)",
        name="auth_challenge_shape",
    ),
)

auth_attempt_buckets = sa.Table(
    "auth_attempt_buckets",
    metadata,
    sa.Column("scope", sa.String, primary_key=True),
    sa.Column("key_hash", sa.String(64), primary_key=True),
    sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("attempt_count", sa.Integer, nullable=False),
)
sa.Index(
    "idx_auth_attempt_buckets_window_started_at",
    auth_attempt_buckets.c.window_started_at,
)

pending_invites = sa.Table(
    "pending_invites",
    metadata,
    sa.Column("email", sa.String, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    # Org role granted on acceptance: "member" (default) or "owner".
    sa.Column(
        "role",
        sa.String,
        nullable=False,
        default="member",
        server_default="member",
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

project_invites = sa.Table(
    "project_invites",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("slug", sa.String, nullable=False),
    sa.Column("email", sa.String, nullable=False),
    sa.Column("role", sa.String, nullable=False),
    # Display/compatibility marker only. Never store raw invite tokens here;
    # `token_hash` is what an incoming token is matched against.
    sa.Column("token", sa.String, unique=True, nullable=False),
    sa.Column("token_hash", sa.String, unique=True, nullable=False),
    sa.Column("invited_by_user_id", sa.Integer, sa.ForeignKey("users.id")),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
)

# Per-project RBAC: editor / reviewer / viewer scoped to a single project,
# distinct from the org-level owner/member membership. A row
# grants `user_id` the role `role` on (org_id, slug). Absence of a row means
# the user falls back to their org default (owner => owner, otherwise no project
# access). The project roles form a strict capability ladder:
#   viewer   - read only (GET)
#   reviewer - read + accept/reject review actions, but cannot run ops or mutate
#   editor   - full control, incl. running ops, editing data, managing members
#   owner    - editor powers plus project ownership/funding management
project_roles = sa.Table(
    "project_roles",
    metadata,
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), primary_key=True),
    sa.Column("slug", sa.String, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("role", sa.String, nullable=False),
)

# Recoverable project-create protocol. The intent commits before filesystem
# creation; startup completes any surviving intent rather than leaving an
# unregistered bundle or silently exposing it.
project_creation_intents = sa.Table(
    "project_creation_intents",
    metadata,
    sa.Column("slug", sa.String, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("creator_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column(
        "sensitive",
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
)

# API tokens / PATs: org-scoped bearer credentials for programmatic access.
# The plaintext token (`frisket_pat_<rand>`) is returned exactly once at
# creation; only its sha256 hex digest is stored. `prefix` is a display
# handle (scheme + first chars of the random part) - enough to recognize a
# token in a list, never enough to authenticate. A token acts AS its
# creating user within its org, so the existing per-project RBAC ladder
# applies unchanged. Revocation is a tombstone (revoked_at) so the audit
# trail keeps the row.
api_tokens = sa.Table(
    "api_tokens",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("token_hash", sa.String, unique=True, nullable=False),
    sa.Column("prefix", sa.String, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("last_used_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
)

# Operator tokens: on-box-minted bearer credentials for remote server
# administration (`frisket-control token mint` -> `frisket remote link`).
# The plaintext (`frisket_operator_<rand>`) is printed exactly once at mint
# time; only its sha256 hex digest is stored. An operator token carries
# org-owner power over the admin API, authenticated entirely apart from
# sessions/PATs. Rotation and revocation are tombstones (revoked_at) so the
# audit trail keeps every credential ever issued.
operator_tokens = sa.Table(
    "operator_tokens",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("label", sa.String),
    sa.Column("token_hash", sa.String, unique=True, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("last_used_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
)

# Single-use password-reset links, following the project-invite hash-at-rest
# pattern: the raw token (`frisket_reset_<rand>`) leaves the server exactly
# once (in the reset link) and only its sha256 digest is persisted.
password_resets = sa.Table(
    "password_resets",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("token_hash", sa.String, unique=True, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
)

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer),
    sa.Column("org_id", sa.Integer),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("detail", sa.String),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
)

# Browser/client error reports and diagnostic issue bundles. These are
# control-plane records, not project content: they must be readable by
# operators even when a project bundle or worker is unhealthy.
client_errors = sa.Table(
    "client_errors",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer),
    sa.Column("user_id", sa.Integer),
    sa.Column("source", sa.String, nullable=False, default="browser"),
    sa.Column("severity", sa.String, nullable=False, default="error"),
    sa.Column("name", sa.String),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("stack", sa.Text),
    sa.Column("route", sa.String),
    sa.Column("project_id", sa.String),
    sa.Column("sheet_id", sa.String),
    sa.Column("run_id", sa.String),
    sa.Column("job_id", sa.String),
    sa.Column("trace_id", sa.String),
    sa.Column("context_json", sa.Text, nullable=False, default="{}"),
    sa.Column("bundle_json", sa.Text),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
)

# BYO API keys: org-supplied provider keys, envelope-encrypted at rest. The
# plaintext is write-only - stored encrypted, never read back over the API.
# Metered spend caps against a key are commerce policy and live outside this
# tree, in a deployment's own accounting.
org_keys = sa.Table(
    "org_keys",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("encrypted", sa.Text, nullable=False),
    sa.Column("hint", sa.String, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.UniqueConstraint("org_id", "provider"),
)

# Custom org environment variables: write-only encrypted metadata separate
# from provider API keys. These are NOT injected into sandboxed Python by
# default; future trusted integrations can opt in explicitly.
org_env_vars = sa.Table(
    "org_env_vars",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("encrypted", sa.Text, nullable=False),
    sa.Column("hint", sa.String, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.UniqueConstraint("org_id", "name"),
)

oauth_connections = sa.Table(
    "oauth_connections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("connection_id", sa.String, nullable=False),
    sa.Column("external_subject", sa.String, nullable=False),
    sa.Column("external_email", sa.String),
    sa.Column("scopes_json", sa.Text, nullable=False, default="[]"),
    sa.Column("encrypted_refresh_token", sa.Text, nullable=False),
    sa.Column("refresh_token_hint", sa.String, nullable=False),
    sa.Column("token_type", sa.String, nullable=False, default="Bearer"),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    ),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("org_id", "provider", "connection_id"),
    sa.UniqueConstraint("org_id", "provider", "external_subject"),
)
