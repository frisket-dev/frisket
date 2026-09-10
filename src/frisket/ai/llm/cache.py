"""Response cache for replaying a prior model response instead of re-running it.

Key = sha256(model + recipe_version + canonical(messages) + canonical(schema)
+ canonical(params)). Rendered row inputs are inside messages, so row 2 can
never answer for row 1, and any prompt/schema/model/param change busts the key.

Entries store response bodies only — request headers (auth) are never written.
Lives in a sidecar db (project.cache.db) or any standalone path (test cache).

Modes:
  replay        — hit returns cached; miss does live call then stores
  fresh         — always live, stores result (the "Re-run fresh" button)
  replay_strict — miss raises (CI: fail loudly with the refresh command)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .types import LLMRequest, LLMResponse

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class CacheMiss(Exception):
    def __init__(self, key: str):
        super().__init__(
            f"cache miss in replay_strict mode (key={key[:16]}…). "
            "Refresh with: FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay "
            "uv run pytest <the test> — this performs live calls and updates "
            "the committed cache fixture."
        )
        self.key = key


# Bump when adapter request-shaping changes (same LLMRequest → different API
# body would otherwise replay stale responses).
# v2→v3: the structured-output `mechanism` now joins the key. Once
# the completer's resolved mechanism can vary the wire body (a strict json_schema
# body vs a loose json_object+prompt body vs an Anthropic tool body) for the SAME
# LLMRequest, two strategies would otherwise collide on one key and replay each
# other's stale responses. The bump invalidates every v2 cassette in one move so
# a v2 key can never leak into v3.
CACHE_KEY_VERSION = 3


def request_key(req: LLMRequest, recipe_version: str = "1") -> str:
    canonical_dict: dict = {
        "v": CACHE_KEY_VERSION,
        "model": req.model,
        "recipe_version": recipe_version,
        "messages": req.messages,
        "schema": req.schema,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "params": req.params,
        "mechanism": req.mechanism,
    }
    # This mirrors the `mechanism` field's v2->v3 bump reasoning above: `tools` is a
    # NEW request-shaping field that changes the wire body (multi-tool defs +
    # tool_choice=auto), so two different tool sets for an otherwise-identical
    # LLMRequest must NOT collide on one cache key. But json.dumps bakes in every
    # key present in the dict regardless of its value -- unconditionally adding
    # `"tools": req.tools` would flip EVERY existing (tools=None) request's hash
    # the moment this key ships, invalidating the entire committed golden cache
    # for zero behavior change (exactly what CACHE_KEY_VERSION 2->3 was minted to
    # avoid doing casually). No caller sets `tools` before this stage, so there is
    # no existing cassette to protect for the tools=None case: only include the
    # key when a request actually carries tools, keeping every unchanged mode's
    # canonical JSON -- and therefore its hash -- byte-identical to before.
    if req.tools is not None:
        canonical_dict["tools"] = req.tools
    if req.reasoning_policy is not None:
        canonical_dict["reasoning_policy"] = req.reasoning_policy
    canonical = json.dumps(canonical_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(CACHE_SCHEMA)

    def get(self, key: str) -> LLMResponse | None:
        row = self.db.execute(
            "SELECT response FROM llm_cache WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        resp = LLMResponse(**payload)
        resp.cached = True
        return resp

    def put(self, key: str, resp: LLMResponse) -> None:
        payload = asdict(resp)
        payload["cached"] = False
        self.db.execute(
            "INSERT INTO llm_cache (key, model, response) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET response=excluded.response",
            (key, resp.model, json.dumps(payload)),
        )
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]

    def prune_lru(self, max_entries: int) -> int:
        """Cache LRU: keep at most ``max_entries`` most-recent rows, evict the
        oldest. The response cache is rebuildable (a sidecar), so dropping cold
        entries only forces a re-call on next access — it never loses
        irreplaceable data. Returns the number of entries evicted.

        Without this, the sidecar grows append-forever as prompts and models
        churn.
        """
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")
        total = self.count()
        if total <= max_entries:
            return 0
        cur = self.db.execute(
            "DELETE FROM llm_cache WHERE key IN ("
            "  SELECT key FROM llm_cache ORDER BY created_at DESC, rowid DESC "
            "  LIMIT -1 OFFSET ?)",
            (max_entries,),
        )
        evicted = cur.rowcount
        self.db.commit()
        return evicted

    def close(self) -> None:
        self.db.close()
