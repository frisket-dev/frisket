"""Seeded fault-injection middleware.

Deterministic: same seed + same call sequence → same fault sequence, so
failure tests are reproducible in CI. Faults are injected at the router
seam — the only path model traffic takes.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

from .types import LLMError, LLMRequest, LLMResponse, SchemaViolation


@dataclass
class ChaosConfig:
    seed: int = 0
    enabled: bool = False
    fail_rate: float = 0.0  # probability of a 500
    rate_limit_rate: float = 0.0  # probability of a 429
    auth_fail_rate: float = 0.0  # probability of a 401 (non-retryable)
    malformed_json_rate: float = 0.0  # structured output returns garbage
    truncate_rate: float = 0.0  # output cut mid-stream
    latency_ms: tuple[int, int] = (0, 0)
    rate_limit_storm_after: int | None = (
        None  # every call 429s for `storm_length` after N calls
    )
    storm_length: int = 5


@dataclass
class ChaosMiddleware:
    config: ChaosConfig
    calls: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.config.seed)

    async def intercept(self, req: LLMRequest) -> LLMResponse | None:
        """Returns a fabricated faulty response, raises a fabricated error,
        or returns None to let the real call proceed."""
        c = self.config
        if not c.enabled:
            return None
        self.calls += 1
        if c.latency_ms[1] > 0:
            await asyncio.sleep(self._rng.uniform(*c.latency_ms) / 1000)
        if (
            c.rate_limit_storm_after is not None
            and c.rate_limit_storm_after
            < self.calls
            <= c.rate_limit_storm_after + c.storm_length
        ):
            raise LLMError("chaos: 429 storm", status=429, retryable=True)
        roll = self._rng.random()
        if roll < c.auth_fail_rate:
            raise LLMError("chaos: 401 unauthorized", status=401, retryable=False)
        roll = self._rng.random()
        if roll < c.fail_rate:
            raise LLMError("chaos: 500 internal error", status=500, retryable=True)
        roll = self._rng.random()
        if roll < c.rate_limit_rate:
            raise LLMError("chaos: 429 rate limited", status=429, retryable=True)
        roll = self._rng.random()
        if roll < c.malformed_json_rate and req.schema is not None:
            raise SchemaViolation("chaos: malformed JSON", raw_text='{"oops": tru')
        roll = self._rng.random()
        if roll < c.truncate_rate:
            text = json.dumps({"truncated": True})[:8]
            raise SchemaViolation("chaos: truncated output", raw_text=text)
        return None
