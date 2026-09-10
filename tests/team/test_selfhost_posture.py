from __future__ import annotations

from pathlib import Path

import pytest

from frisket.team.security.secrets import using_default_master_key


ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"


class TestEnvExample:
    def test_env_example_flags_shipped_postgres_creds_must_change(self) -> None:
        text = ENV_EXAMPLE.read_text().lower()
        assert "must-change" in text or "must change" in text


class TestSecretsPosture:
    def test_using_default_master_key_true_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FRISKET_SECRETS_MASTER_KEY", raising=False)
        assert using_default_master_key() is True

    def test_using_default_master_key_false_when_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FRISKET_SECRETS_MASTER_KEY", "a-real-operator-key")
        assert using_default_master_key() is False
