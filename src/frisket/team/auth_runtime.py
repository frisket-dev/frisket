"""Production outbound auth transports for the open team server."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import httpx
import jwt

from frisket.team.config import TeamConfig


def smtp_sender(config: TeamConfig):
    async def send(email: str, link: str) -> bool:
        def _send() -> None:
            message = EmailMessage()
            message["From"] = config.smtp_from
            message["To"] = email
            message["Subject"] = "Your frisket sign-in link"
            message.set_content(f"Sign in to frisket:\n\n{link}\n")
            context = ssl.create_default_context()
            smtp_type = smtplib.SMTP_SSL if config.smtp_ssl else smtplib.SMTP
            kwargs = {"context": context} if config.smtp_ssl else {}
            with smtp_type(
                str(config.smtp_host), config.smtp_port, timeout=15, **kwargs
            ) as smtp:
                if config.smtp_starttls:
                    smtp.starttls(context=context)
                if config.smtp_username:
                    smtp.login(config.smtp_username, config.smtp_password or "")
                smtp.send_message(message)

        await asyncio.to_thread(_send)
        return True

    return send


def production_oidc_exchange(config: TeamConfig):
    async def exchange(
        provider: str, code: str, redirect_uri: str, expected_nonce: str
    ) -> dict[str, Any]:
        definition = config.oidc_providers[provider]
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                definition.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": definition.client_id,
                    "client_secret": definition.client_secret,
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            id_token = token_payload.get("id_token")
            if not id_token:
                raise ValueError("OIDC token response omitted id_token")
            jwks_response = await client.get(definition.jwks_uri)
            jwks_response.raise_for_status()
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") not in definition.algorithms:
            raise ValueError("OIDC id_token uses an unconfigured signing algorithm")
        keys = jwt.PyJWKSet.from_dict(jwks_response.json()).keys
        key = next(
            (candidate for candidate in keys if candidate.key_id == header.get("kid")),
            None,
        )
        if key is None:
            raise ValueError("OIDC signing key is not present in configured JWKS")
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=list(definition.algorithms),
            audience=definition.client_id,
            issuer=definition.issuer,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "nonce",
                    "sub",
                    "email",
                    "email_verified",
                ]
            },
        )
        if not expected_nonce or claims.get("nonce") != expected_nonce:
            raise ValueError("OIDC nonce mismatch")
        return {
            "email": claims["email"],
            "email_verified": claims.get("email_verified") is True,
            "subject": claims["sub"],
            "nonce": claims["nonce"],
            # Carried through when the provider returns it under the requested
            # `profile` scope; consumers treat it as optional (Google omits it
            # for some workspace identities).
            "name": claims.get("name"),
        }

    return exchange


async def validate_provider_credential(provider: str, secret: str) -> bool:
    requests = {
        "openai": (
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {secret}"},
            None,
        ),
        "anthropic": (
            "https://api.anthropic.com/v1/models",
            {"x-api-key": secret, "anthropic-version": "2023-06-01"},
            None,
        ),
        "openrouter": (
            "https://openrouter.ai/api/v1/auth/key",
            {"Authorization": f"Bearer {secret}"},
            None,
        ),
        "gemini": (
            "https://generativelanguage.googleapis.com/v1beta/models",
            {},
            {"key": secret},
        ),
    }
    if provider not in requests:
        return False
    url, headers, params = requests[provider]
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=headers, params=params)
    return response.status_code < 400


__all__ = ["production_oidc_exchange", "smtp_sender", "validate_provider_credential"]
