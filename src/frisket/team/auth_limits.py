"""Small persistent budgets for anonymous browser-auth entry points."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable

import sqlalchemy as sa
from fastapi import Request

from frisket.team.schema import auth_attempt_buckets


MAX_AUTH_WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True)
class AuthBudget:
    scope: str
    key_hash: str
    limit: int
    window_seconds: int


def auth_budget(
    scope: str, subject: str, *, limit: int, window_seconds: int
) -> AuthBudget:
    digest = hashlib.sha256(f"{scope}\0{subject}".encode("utf-8")).hexdigest()
    return AuthBudget(
        scope=scope,
        key_hash=digest,
        limit=limit,
        window_seconds=window_seconds,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def reserve_many(
    cx: sa.Connection, budgets: tuple[AuthBudget, ...], *, now: datetime
) -> bool:
    if not budgets:
        return True
    if any(
        budget.limit < 1
        or budget.window_seconds < 1
        or budget.window_seconds > MAX_AUTH_WINDOW_SECONDS
        for budget in budgets
    ):
        raise ValueError("invalid browser-auth budget")

    cx.execute(
        auth_attempt_buckets.delete().where(
            auth_attempt_buckets.c.window_started_at
            <= now - timedelta(seconds=MAX_AUTH_WINDOW_SECONDS)
        )
    )
    current: list[tuple[AuthBudget, int, datetime]] = []
    for budget in budgets:
        row = cx.execute(
            sa.select(auth_attempt_buckets).where(
                auth_attempt_buckets.c.scope == budget.scope,
                auth_attempt_buckets.c.key_hash == budget.key_hash,
            )
        ).first()
        started_at = now
        attempts = 0
        if row is not None:
            started_at = _aware(row.window_started_at)
            if started_at + timedelta(seconds=budget.window_seconds) > now:
                attempts = int(row.attempt_count)
        if attempts >= budget.limit:
            return False
        current.append((budget, attempts, started_at))

    for budget, attempts, started_at in current:
        values = {
            "window_started_at": started_at if attempts else now,
            "attempt_count": attempts + 1,
        }
        updated = cx.execute(
            auth_attempt_buckets.update()
            .where(
                auth_attempt_buckets.c.scope == budget.scope,
                auth_attempt_buckets.c.key_hash == budget.key_hash,
            )
            .values(**values)
        )
        if updated.rowcount == 0:
            cx.execute(
                auth_attempt_buckets.insert().values(
                    scope=budget.scope,
                    key_hash=budget.key_hash,
                    **values,
                )
            )
    return True


def clear(cx: sa.Connection, budget: AuthBudget) -> None:
    cx.execute(
        auth_attempt_buckets.delete().where(
            auth_attempt_buckets.c.scope == budget.scope,
            auth_attempt_buckets.c.key_hash == budget.key_hash,
        )
    )


def trusted_proxy_networks(
    cidrs: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = str(raw).strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid trusted proxy CIDR: {value!r}") from exc
    return tuple(networks)


def trusted_client_ip(
    request: Request,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str:
    peer_text = request.client.host if request.client is not None else ""
    try:
        peer = ipaddress.ip_address(peer_text)
    except ValueError:
        return "unknown"
    if not any(peer in network for network in networks):
        return str(peer)

    forwarded_values = request.headers.getlist("x-forwarded-for")
    if len(forwarded_values) != 1:
        return str(peer)
    parts = forwarded_values[0].split(",")
    if not parts or any(not part.strip() for part in parts):
        return str(peer)

    forwarded: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in parts:
        value = raw.strip()
        try:
            forwarded.append(ipaddress.ip_address(value))
        except ValueError:
            return str(peer)
    for candidate in reversed(forwarded):
        if not any(candidate in network for network in networks):
            return str(candidate)
    return str(peer)


__all__ = [
    "AuthBudget",
    "auth_budget",
    "clear",
    "reserve_many",
    "trusted_client_ip",
    "trusted_proxy_networks",
]
