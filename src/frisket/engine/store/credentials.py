"""Credential store for a project bundle: encrypted per-project provider API
keys and named project secrets, with their consumer declarations and
migration-conflict rows. The bundle owns project.db and the ``encrypted``
column, so every read/write of the project_provider_keys / project_secrets
tables goes through here; the settings service keeps validation and
orchestration (provider normalization, spend-cap conversion, encrypt-on-write,
consumer-declaration policy) and never touches project.db directly.
Decryption of a stored key or secret happens ONLY here
(``provider_model_keys`` / ``secret_plaintext``) — the run worker and the
workspace router read through this single seam instead of re-implementing an
inline decrypt. Free functions over the facade's per-thread SQLite
connection; ``project`` stays duck-typed (``Any``) so this leaf never
re-imports the facade module."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from frisket.team.security.secrets import decrypt_secret


class ProviderKeyDecryptError(RuntimeError):
    """A stored project provider key could not be decrypted.

    Loud on purpose. The silent ``continue`` this replaces degraded an
    undecryptable row to "no project keys", and "no project keys" is not an
    absence of consequence: the router's key layers fall THROUGH to the
    workspace file / org BYOK / process env (``jobs/runs.py:_router_for``,
    ``server/workspace.py:router_for``), so a rotated or corrupt AEAD key
    silently re-routed the run onto a different credential — spending money
    on a key with no per-key cap and stamping a different
    ``credential_source`` onto every durable fact. A wrong key is a
    configuration fault to be named and fixed, never a downgrade to make
    quietly.

    Names the provider and nothing else: no ciphertext, no hint, no
    plaintext.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(
            f"the stored {provider} provider key for this project could not be "
            "decrypted (the bundle's encryption key may have rotated, or the "
            "row may be corrupt). Re-enter the key in Settings > AI Providers; "
            "refusing to fall back to another credential, which would spend "
            "against a key you did not choose and bypass this key's spend cap."
        )


@dataclass(frozen=True)
class ProviderSpendDelta:
    """One batch's worth of provider spend attributable to ONE project key.

    The two fields only ever occur together and must never be collapsed:
    ``micro`` is spend we can price, ``unmetered_calls`` is spend we cannot.
    Folding an unpriceable call into ``micro`` as 0 is exactly the
    ``cost_actual REAL NOT NULL DEFAULT 0`` defect (a real BYOK call
    rendering identically to a free local one), so the union is kept whole
    all the way to the UI.
    """

    micro: int
    unmetered_calls: int

    def __post_init__(self) -> None:
        if self.micro < 0 or self.unmetered_calls < 0:
            raise ValueError("provider spend deltas are non-negative")

    @property
    def is_empty(self) -> bool:
        return self.micro == 0 and self.unmetered_calls == 0


@dataclass(frozen=True)
class ProviderSpendState:
    """What is known about one project provider key's spend against its cap.

    ``cap_micro is None`` means the journalist set no cap — nothing to check.
    ``spent_micro`` is the sum of calls we could price; when
    ``unmetered_calls > 0`` it is a LOWER BOUND on true spend, not the whole
    truth, which is why the two travel together everywhere they are shown.

    The two questions a cap can answer are separate, and both are decided
    here: is the bound EXCEEDED (``over_cap``), and can the bound be
    ENFORCED at all (``cap_enforceable``).
    """

    provider: str
    cap_micro: int | None
    spent_micro: int
    unmetered_calls: int

    @property
    def over_cap(self) -> bool:
        return self.cap_micro is not None and self.spent_micro >= self.cap_micro

    @property
    def cap_enforceable(self) -> bool:
        """Whether ``spent_micro`` is the WHOLE spend on this key.

        A capped key that has made calls of unknown price has spend of
        unknown size: ``spent_micro`` is a lower bound, so "under the cap" is
        a guess, not an answer. A real billed vision-OCR
        call rated as unpriceable and the cap silently went on reporting
        $0.00 spent as if it were the truth. A cap is a promise; a promise we
        cannot keep must say so rather than pass.

        An UNCAPPED key is unaffected — the journalist bounded nothing, so
        there is nothing to fail to enforce, and unpriced calls stay a
        recorded fact.
        """
        return self.cap_micro is None or self.unmetered_calls == 0


def provider_key_catalog_rows(project: Any) -> dict[str, dict[str, Any]]:
    """Raw project_provider_keys rows keyed by provider (never the
    ciphertext). The settings service maps these onto its provider catalog
    projection; nothing plaintext is read here."""
    return {
        str(row["provider"]): {
            "provider": str(row["provider"]),
            "hint": row["hint"],
            "spend_cap_micro": row["spend_cap_micro"],
            "spent_micro": row["spent_micro"],
            "unmetered_calls": row["unmetered_calls"],
            "updated_at": row["updated_at"],
        }
        for row in project.db.execute(
            "SELECT provider, hint, spend_cap_micro, spent_micro, "
            "unmetered_calls, updated_at "
            "FROM project_provider_keys ORDER BY provider"
        )
    }


def provider_spend_state(project: Any, provider: str) -> ProviderSpendState | None:
    """This key's cap and accrued spend, or None when the project configures
    no key for ``provider`` (no key, no cap, nothing to enforce — the
    missing-key guard owns that case)."""
    row = project.db.execute(
        "SELECT provider, spend_cap_micro, spent_micro, unmetered_calls "
        "FROM project_provider_keys WHERE provider=?",
        (provider,),
    ).fetchone()
    if row is None:
        return None
    return ProviderSpendState(
        provider=str(row["provider"]),
        cap_micro=(
            None if row["spend_cap_micro"] is None else int(row["spend_cap_micro"])
        ),
        spent_micro=int(row["spent_micro"]),
        unmetered_calls=int(row["unmetered_calls"]),
    )


def accrue_provider_spend(
    project: Any, deltas: Mapping[str, ProviderSpendDelta]
) -> None:
    """Add one batch's provider spend onto the matching project keys.

    The ONLY writer of ``spent_micro`` / ``unmetered_calls`` outside the key
    upsert. Deliberately does NOT commit: its single caller
    (``RunResultStore.write_model_calls``) runs inside the results-write
    transaction, so accrual lands atomically with the model_call facts it was
    derived from — a fact that exists always has its money counted, and a
    rolled-back batch accrues nothing.

    A provider with no configured project key matches no row and accrues
    nothing; that spend belongs to some other credential (org BYOK, platform,
    env) whose cap, if any, is not ours to enforce. ``updated_at`` is NOT
    stamped — it means "key last set", not "key last used".
    """
    for provider, delta in deltas.items():
        if delta.is_empty:
            continue
        project.db.execute(
            "UPDATE project_provider_keys SET "
            "spent_micro = spent_micro + ?, "
            "unmetered_calls = unmetered_calls + ? "
            "WHERE provider = ?",
            (delta.micro, delta.unmetered_calls, provider),
        )


def _provider_key_value_changed(project: Any, provider: str, encrypted: str) -> bool:
    """Whether this save replaces the stored key VALUE with a different one.

    Compared on PLAINTEXT, in the one module allowed to decrypt: the
    ciphertext differs on every save even when the key does not. A row that
    cannot be decrypted (rotated encryption material, corrupt bundle) counts
    as UNCHANGED — the accrual is money already spent, and a doubt about
    which key spent it is not a reason to forgive it.
    """
    row = project.db.execute(
        "SELECT encrypted FROM project_provider_keys WHERE provider=?",
        (provider,),
    ).fetchone()
    if row is None:
        return False  # no prior row; the INSERT branch starts at zero anyway
    try:
        return decrypt_secret(str(row["encrypted"])) != decrypt_secret(encrypted)
    except Exception:
        return False


def set_provider_key(
    project: Any,
    *,
    provider: str,
    encrypted: str,
    hint: str,
    spend_cap_micro: int | None,
) -> None:
    """Upsert an already-encrypted provider key, PRESERVING accrued spend
    unless the key value itself changes.

    This used to zero ``spent_micro``/``unmetered_calls`` on every save, so
    the settings form that edits a cap also forgave every dollar accrued
    against it — lowering the cap made
    the key read $0.00 spent on a key that had just billed, and the lowered
    cap then had nothing to refuse. A cap that any edit resets is not a cap,
    and the reset was reachable by the ONE gesture (change the cap) most
    likely to be made by someone who thinks they are tightening it.

    A NEW KEY VALUE is a new budget. Rotating the credential means the old
    accrual was spent on a different key at the provider, and the
    provider-side bill starts over with it, so the counters do too. That is
    also the recovery path both cap refusals name: rotate the key, or clear
    the cap. Editing the cap, the hint, or re-saving the same key keeps
    every dollar.
    """
    starts_fresh = _provider_key_value_changed(project, provider, encrypted)
    try:
        project.db.execute(
            "INSERT INTO project_provider_keys "
            "(provider, encrypted, hint, spend_cap_micro, spent_micro, "
            "unmetered_calls, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, datetime('now')) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "encrypted=excluded.encrypted, hint=excluded.hint, "
            "spend_cap_micro=excluded.spend_cap_micro, "
            "spent_micro=CASE WHEN ? THEN 0 ELSE spent_micro END, "
            "unmetered_calls=CASE WHEN ? THEN 0 ELSE unmetered_calls END, "
            "updated_at=excluded.updated_at",
            (
                provider,
                encrypted,
                hint,
                spend_cap_micro,
                1 if starts_fresh else 0,
                1 if starts_fresh else 0,
            ),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def delete_provider_key(project: Any, provider: str) -> bool:
    """Delete a provider key; returns True iff a row was removed."""
    try:
        cursor = project.db.execute(
            "DELETE FROM project_provider_keys WHERE provider=?", (provider,)
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return cursor.rowcount > 0


def provider_model_keys(project: Any) -> dict[str, str]:
    """Decrypted per-project provider API keys (the RUN path key layer).

    This is the ONLY decrypt of the project_provider_keys `encrypted`
    column: the run worker (jobs/runs.py) and the workspace router
    (server/workspace.py) both read through here rather than re-implementing
    an inline decrypt.

    A missing table still degrades to "no project keys" — a partially
    migrated bundle genuinely HAS no keys. An un-decryptable ROW does not:
    that bundle has a key, we just cannot read it, and skipping it silently
    hands the run to whatever credential the next layer offers. That
    downgrade routes spend around this key's per-key cap and falsifies the
    ``credential_source`` on every fact the run persists, so it raises
    :class:`ProviderKeyDecryptError` naming the provider instead."""
    try:
        rows = project.db.execute(
            "SELECT provider, encrypted FROM project_provider_keys"
        ).fetchall()
    except Exception:
        return {}
    keys: dict[str, str] = {}
    for row in rows:
        provider = str(row["provider"])
        try:
            keys[provider] = decrypt_secret(str(row["encrypted"]))
        except Exception as exc:
            raise ProviderKeyDecryptError(provider) from exc
    return keys


def secret_rows(project: Any) -> list[sqlite3.Row]:
    """Raw project_secrets rows (name/hint/updated_at, never ciphertext)."""
    return list(
        project.db.execute(
            "SELECT name, hint, updated_at FROM project_secrets ORDER BY name"
        )
    )


def secret_migration_conflict_rows(project: Any) -> list[sqlite3.Row]:
    return list(
        project.db.execute(
            "SELECT plugin_id, name, hint, status, created_at "
            "FROM project_secret_migration_conflicts ORDER BY name, plugin_id"
        )
    )


def secret_consumer_rows(project: Any) -> list[sqlite3.Row]:
    return list(
        project.db.execute(
            "SELECT kind, consumer_id, name FROM project_secret_consumers "
            "ORDER BY kind, consumer_id"
        )
    )


def set_secret(project: Any, *, name: str, encrypted: str, hint: str) -> None:
    """Upsert an already-encrypted project secret and mark any pending
    migration conflict for that name resolved (the settings revamp contract:
    supplying the value clears the workbench_plugin_env_vars conflict)."""
    try:
        project.db.execute(
            "INSERT INTO project_secrets (name, encrypted, hint, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET encrypted=excluded.encrypted, "
            "hint=excluded.hint, updated_at=excluded.updated_at",
            (name, encrypted, hint),
        )
        project.db.execute(
            "UPDATE project_secret_migration_conflicts "
            "SET status='resolved' WHERE name=?",
            (name,),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def add_secret_consumer(
    project: Any, *, kind: str, consumer_id: str, name: str
) -> None:
    try:
        project.db.execute(
            "INSERT OR IGNORE INTO project_secret_consumers "
            "(kind, consumer_id, name) VALUES (?, ?, ?)",
            (kind, consumer_id, name),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def replace_secret_consumers(
    project: Any, *, kind: str, consumer_id: str, names: set[str], commit: bool = True
) -> None:
    """Replace one consumer's declared project-secret references atomically.

    Removing a configuration consumer must never delete the shared project
    secret itself.  Replacing the join rows in one transaction prevents stale
    grants when a server is edited from one secret reference to another.
    """
    try:
        project.db.execute(
            "DELETE FROM project_secret_consumers WHERE kind=? AND consumer_id=?",
            (kind, consumer_id),
        )
        project.db.executemany(
            "INSERT INTO project_secret_consumers (kind, consumer_id, name) "
            "VALUES (?, ?, ?)",
            [(kind, consumer_id, name) for name in sorted(names)],
        )
        if commit:
            project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def secret_consumer_is_declared(
    project: Any, *, kind: str, consumer_id: str, name: str
) -> bool:
    return (
        project.db.execute(
            "SELECT 1 FROM project_secret_consumers "
            "WHERE kind=? AND consumer_id=? AND name=?",
            (kind, consumer_id, name),
        ).fetchone()
        is not None
    )


def secret_plaintext(project: Any, name: str) -> str | None:
    """Decrypt one project secret, or None when no row exists. The ONLY
    decrypt of the project_secrets `encrypted` column; the settings service
    applies its own consumer-declaration policy and workspace fallback around
    this call (stored secret values are never empty, so None unambiguously
    means "no such secret")."""
    row = project.db.execute(
        "SELECT encrypted FROM project_secrets WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        return None
    return decrypt_secret(str(row["encrypted"]))


def delete_secret(project: Any, name: str) -> bool:
    """Delete a project secret; returns True iff a row was removed."""
    try:
        cursor = project.db.execute("DELETE FROM project_secrets WHERE name=?", (name,))
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return cursor.rowcount > 0
