"""Store-owned provider-key / project-secret accessors (the settings store seam).

RED-first for triagefix-project-provider-key-store-seam-v1. The 2026-07-01
settings revamp (60d6e2ed) reached the per-project `project_provider_keys` and
`project_secrets` sqlite tables from OUTSIDE the store layer in three places:
the settings service (raw project.db SQL), the run worker
(jobs/runs.py:_project_model_keys, inline decrypt_secret), and the workspace
router (workspace.py:_project_provider_keys, an identical inline decrypt). That
broke two architectural-boundary pins that share one root cause.

The store (frisket.store.Project) owns project.db and the `encrypted` column, so
the accessors live here: a raw-row reader + upsert/delete for the service's
catalog/CRUD, and a single decrypted provider_model_keys()/secret_plaintext()
seam for every reader. This test pins the RUN path: a broken refactor would
silently drop provider keys and make runs fail or use the wrong key.
"""

from __future__ import annotations

import pytest

from frisket.team.security.secrets import decrypt_secret, encrypt_secret, key_hint
from frisket.engine.store import Project
from frisket.engine.store.credentials import ProviderKeyDecryptError


def _new_project(tmp_path) -> Project:
    return Project.create(tmp_path / "seam.frisket", name="Seam")


def test_provider_key_set_get_delete_round_trips_through_store(tmp_path) -> None:
    project = _new_project(tmp_path)
    assert project.provider_key_catalog_rows() == {}
    assert project.provider_model_keys() == {}

    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=7_000_000,
    )

    rows = project.provider_key_catalog_rows()
    assert set(rows) == {"openai"}
    assert rows["openai"]["hint"] == "...enai"
    assert rows["openai"]["spend_cap_micro"] == 7_000_000
    assert rows["openai"]["spent_micro"] == 0
    assert rows["openai"]["updated_at"]

    assert project.provider_model_keys() == {"openai": "sk-project-openai"}

    assert project.delete_provider_key("openai") is True
    assert project.delete_provider_key("openai") is False
    assert project.provider_key_catalog_rows() == {}
    assert project.provider_model_keys() == {}


def test_no_executor_path_composes_a_router_without_the_project_key_layer() -> None:
    """Closure guard, not an enumeration promise (CLAUDE.md: agents undercount
    call sites, so make a new one go RED rather than asking anyone to sweep).

    Every executor entry point that dispatches model work for a project must
    compose its router through `jobs/runs.py:project_scoped_router`, which
    layers `project.provider_model_keys()` on. Four sites spelled it `router
    or ModelRouter()` instead — the two map-runner factories, the preview
    factory, and the reduce/join child-sheet body — and a bare `ModelRouter`
    reads the process env and nothing else. A project whose key was set in
    Settings > AI Providers had it bypassed on every router-less entry point
    (`frisket action run`, the plan runner, previews), spending the
    deployment's env key outside the per-key spend cap.

    `operability/diagnostics.py` is deliberately out of scope: it holds no
    project and probes provider PRESENCE rather than spending against a key.
    """
    import ast
    import inspect

    from frisket.engine.executor import action_lifecycle, actions

    offenders: list[str] = []
    for module in (actions, action_lifecycle):
        tree = ast.parse(inspect.getsource(module))  # rule19: import + inspect,
        # not a repository path read -- the guard is about THIS package's
        # composition, so the imported module is the honest subject.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ModelRouter"
            ):
                offenders.append(f"{module.__name__}:{node.lineno}")
    assert offenders == [], (
        "these executor sites construct a ModelRouter directly instead of "
        f"going through project_scoped_router: {offenders}"
    )


def test_undecryptable_provider_key_raises_naming_the_provider(tmp_path) -> None:
    """A stored key that will not decrypt used to be skipped with a silent
    `except Exception: continue`, degrading the project to "no project keys".

    That is not a harmless downgrade. Both key brokers layer project keys ON
    TOP of the workspace file / org BYOK / process env
    (`jobs/runs.py:_router_for`, `server/workspace.py:router_for`), so the
    run silently continued on a DIFFERENT credential — spending against a key
    with no per-key spend cap, and stamping a `credential_source` on every
    durable fact that did not describe what happened. A rotated or corrupt
    AEAD key is a configuration fault to name, not a fallback to take.
    """
    project = _new_project(tmp_path)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-real"),
        hint=key_hint("sk-real"),
        spend_cap_micro=None,
    )
    # Corrupt the ciphertext in place, exactly as a rotated bundle key looks.
    project.db.execute(
        "UPDATE project_provider_keys SET encrypted=? WHERE provider='openai'",
        ("not-a-valid-ciphertext",),
    )
    project.db.commit()

    with pytest.raises(ProviderKeyDecryptError) as excinfo:
        project.provider_model_keys()
    message = str(excinfo.value)
    assert excinfo.value.provider == "openai"
    assert "openai" in message
    # Fail closed and name the knob -- and never the material itself.
    assert "AI Providers" in message
    assert "not-a-valid-ciphertext" not in message
    assert "sk-real" not in message


def test_store_decrypt_matches_legacy_inline_path(tmp_path) -> None:
    """provider_model_keys() must yield the SAME plaintext the old inline
    `decrypt_secret(row['encrypted'])` path produced from the same table."""
    project = _new_project(tmp_path)
    plaintext = "sk-anthropic-abcd1234"
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret(plaintext),
        hint=key_hint(plaintext),
        spend_cap_micro=None,
    )

    row = project.db.execute(
        "SELECT encrypted FROM project_provider_keys WHERE provider='anthropic'"
    ).fetchone()
    legacy = decrypt_secret(str(row["encrypted"]))

    assert legacy == plaintext
    assert project.provider_model_keys()["anthropic"] == legacy
    assert project.provider_key_catalog_rows()["anthropic"]["spend_cap_micro"] is None


def test_spent_micro_resets_and_hint_updates_on_provider_key_update(tmp_path) -> None:
    project = _new_project(tmp_path)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("k1"),
        hint=key_hint("k1"),
        spend_cap_micro=5_000_000,
    )
    project.db.execute(
        "UPDATE project_provider_keys SET spent_micro=123456 WHERE provider='openai'"
    )
    project.db.commit()
    assert project.provider_key_catalog_rows()["openai"]["spent_micro"] == 123456

    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("k2-longer"),
        hint=key_hint("k2-longer"),
        spend_cap_micro=9_000_000,
    )
    row = project.provider_key_catalog_rows()["openai"]
    assert row["spent_micro"] == 0
    assert row["spend_cap_micro"] == 9_000_000
    assert row["hint"] == key_hint("k2-longer")
    assert project.provider_model_keys()["openai"] == "k2-longer"


def test_secret_store_seam_round_trips_and_tracks_declared_consumers(tmp_path) -> None:
    project = _new_project(tmp_path)
    project.set_secret(
        name="MY_TOKEN",
        encrypted=encrypt_secret("s3cr3t"),
        hint=key_hint("s3cr3t"),
    )
    assert {row["name"] for row in project.secret_rows()} == {"MY_TOKEN"}

    assert (
        project.secret_consumer_is_declared(
            kind="plugin", consumer_id="p1", name="MY_TOKEN"
        )
        is False
    )
    project.add_secret_consumer(kind="plugin", consumer_id="p1", name="MY_TOKEN")
    assert (
        project.secret_consumer_is_declared(
            kind="plugin", consumer_id="p1", name="MY_TOKEN"
        )
        is True
    )

    assert project.secret_plaintext("MY_TOKEN") == "s3cr3t"
    assert project.secret_plaintext("MISSING") is None

    assert project.delete_secret("MY_TOKEN") is True
    assert project.delete_secret("MY_TOKEN") is False
    assert project.secret_plaintext("MY_TOKEN") is None
