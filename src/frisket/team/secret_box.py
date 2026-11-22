"""Per-deployment secret-key provider for the open team control plane."""

from __future__ import annotations

import hashlib
import hmac

from frisket.security.secrets import (
    decrypt_secret,
    encrypt_secret,
    load_or_create_secret_key,
)
from frisket.team.config import TeamConfig


class TeamSecretBox:
    def __init__(self, config: TeamConfig):
        if config.secrets_master_key:
            self._key = config.secrets_master_key.encode("utf-8")
        else:
            assert config.secrets_key_file is not None
            self._key = load_or_create_secret_key(config.secrets_key_file)

    def encrypt(self, value: str) -> str:
        return encrypt_secret(value, master_key=self._key)

    def decrypt(self, value: str) -> str:
        return decrypt_secret(value, master_key=self._key)

    @property
    def validation_signing_secret(self) -> bytes:
        return hmac.new(
            self._key,
            b"frisket.team.provider-validation-signing.v1",
            hashlib.sha256,
        ).digest()


__all__ = ["TeamSecretBox"]
