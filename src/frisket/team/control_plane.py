"""Open identity control-plane access for the worker.

Org BYOK provider keys are CONTROL-PLANE rows, not project rows: they live in
`frisket.team.schema.org_keys` (open identity), envelope-encrypted with the open
AEAD module `frisket.security.secrets`. The open team worker genuinely needs
them — a queued run for org N must resolve that org's Anthropic/OpenAI key
before it can call a model, and the per-project seam
(`frisket.store.project.Project.provider_model_keys`) is NOT a substitute: it
only knows keys a project carries in its own bundle.

That is why this module exists in the OPEN tree: before the split the
worker reached org keys through an external edition's bootstrap +
`HostedSecretAccessService`, which made the open worker unable to run without
that external package.

What is NOT here: spend caps. A key's cap and accrued spend are commerce policy
(an external edition's own accounting), so that edition wraps this
resolution in its own credential port rather than extending it here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import sqlalchemy as sa

from frisket.team.security.secrets import decrypt_secret
from frisket.team.schema import org_keys

# The providers a run's model router can be keyed for. Mirrors the provider
# names written by the org-BYOK routes.
MODEL_KEY_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini", "openrouter")

# Engines are cached per URL: a worker resolves keys once per claimed job and
# must not open a fresh pool every time.
_ENGINES: dict[str, sa.Engine] = {}


def control_plane_engine(database_url: str) -> sa.Engine:
    """A cached engine over the identity control plane.

    Read-only from the worker's point of view. It deliberately does NOT create
    or migrate schema: the control plane is initialized by whoever owns the
    deployment (`frisket.team.bootstrap.init_identity_schema` for the open team,
    an external composition for a managed deployment), so a worker never
    silently conjures tables into a database it merely reads.
    """
    cached = _ENGINES.get(database_url)
    if cached is not None:
        return cached
    engine = sa.create_engine(database_url, future=True)
    _ENGINES[database_url] = engine
    return engine


def org_provider_key_plaintext(
    engine: sa.Engine,
    *,
    org_id: int,
    provider: str,
    decryptor: Callable[[str], str] = decrypt_secret,
) -> str | None:
    """Decrypt one org's stored key for `provider`, or None if it has none."""
    provider = provider.strip().lower()
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(org_keys.c.encrypted).where(
                org_keys.c.org_id == org_id,
                org_keys.c.provider == provider,
            )
        ).first()
    if row is None:
        return None
    return decryptor(row.encrypted)


def org_provider_keys(
    engine: sa.Engine,
    *,
    org_id: int,
    providers: tuple[str, ...] = MODEL_KEY_PROVIDERS,
    decryptor: Callable[[str], str] = decrypt_secret,
) -> dict[str, str]:
    """Every provider key this org has stored, decrypted for runtime use."""
    keys: dict[str, str] = {}
    for provider in providers:
        key = org_provider_key_plaintext(
            engine, org_id=org_id, provider=provider, decryptor=decryptor
        )
        if key:
            keys[provider] = key
    return keys


class TeamOrgKeyCredentialPort:
    """Open worker credential composition using the team's per-instance AEAD key."""

    def __init__(self, decryptor: Callable[[str], str]):
        self._decryptor = decryptor

    def provider_keys(
        self, *, org_id: int, control_database_url: str | None
    ) -> Mapping[str, str]:
        if not control_database_url:
            return {}
        return org_provider_keys(
            control_plane_engine(control_database_url),
            org_id=org_id,
            decryptor=self._decryptor,
        )
