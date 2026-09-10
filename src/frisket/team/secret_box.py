"""Per-deployment secret-key provider for the open team control plane."""

from __future__ import annotations

import hashlib
import hmac

from frisket.team.security.secrets import (
    decrypt_secret,
    encrypt_secret,
    load_or_create_secret_key,
)
from frisket.team.config import TeamConfig


class TeamSecretBox:
    def __init__(self, config: TeamConfig):
        self._key = (
            config.secrets_master_key.encode("utf-8")
            if config.secrets_master_key
            else load_or_create_secret_key(config.secrets_key_file)
        )

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
