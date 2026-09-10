"""Bounded per-row web research, independent of action publication and envelopes."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from frisket.ai.llm.structured import FrisketRouterModel, _content_to_pai
from frisket.ai.llm.types import LLMResponse
from frisket.ai.models.metadata import ModelCallMeta, PROVIDER_KIND
from frisket.local_model_ids import bare_model_name
from frisket.ops.base import render_input_block

MAX_STEPS = 5
# Per turn, not per ordinary completion: this agent can make MAX_STEPS model
# calls per pass; one optional verification pass has the same bound.
MAX_STEP_OUTPUT_TOKENS = 2_048
FETCH_CHARS = 8000
# What one `fetch` tool call may read and how long it may take, end to end.
# The byte budget is the one the agent path has always had; it is now a
# refusal rather than a silent clip (see `fetch_page`). The timeout is a total
# wall-clock bound on the guarded call including DNS and every redirect hop.
FETCH_MAX_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0
# "tools" raised to MAX_STEPS (pydantic-ai's Agent default is 1) so a
# repeated unknown/bogus tool call doesn't hit pydantic-ai's own
# UnexpectedModelBehavior("exceeded max retries") before our request_limit
# does -- an unknown-tool call already produces a ModelRetry observation
# (pydantic-ai's tool_manager: "Unknown tool name: ... Available tools:
# ...") fed back to the model; it just needs enough budget to survive
# until UsageLimits.request_limit=MAX_STEPS is the thing that actually stops
# it (caught as UsageLimitExceeded below). One output retry lets the registered
# validator correct a tool call emitted in the text channel. The same single
# output budget also bounds pydantic-ai's native empty-output retry.
RETRIES = {"tools": MAX_STEPS, "output": 1}


def _answer_is_tool_shaped(answer: str) -> bool:
    """A final answer that is really a tool call the model emitted as PLAIN TEXT
    (a dialect misfire: the provider serialized a tool call into the assistant's
    text channel instead of the tool channel, so pydantic-ai handed it back as
    the ``str`` output). The loop has two fixed tools, so detection uses their
    names and exact argument sets. The whole answer must parse as a JSON object
    that is either

      * frisket's ToolCallPart serialization / a provider tool-call encoding --
        a ``tool``/``name``/``tool_name``/``function`` key whose value is a
        ``search`` or ``fetch`` (this is the exact ``{"tool": ..., "args": {...}}``
        shape ``structured.py`` writes, and the shape seen leaking in prod), or
      * a bare ``{"query": ...}`` or ``{"url": ...}`` arguments object.

    Ordinary prose (even prose that mentions or contains JSON) never parses as a
    whole-string JSON object, so it is never a false positive."""
    text = (answer or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    for name_key in ("tool", "name", "tool_name", "function"):
        if parsed.get(name_key) in {"search", "fetch"}:
            return True
    return set(parsed) in ({"query"}, {"url"})


async def run_research(
    row_values: dict[str, Any],
    *,
    goal: str,
    model_id: str,
    router: Any,
    recipe_version: str,
    search: Callable[[str], Awaitable[tuple[str, list[str]]]],
    fetch: Callable[[str], Awaitable[str]],
) -> tuple[dict, dict]:
    """Return logical answer/source values and actual model-call accounting.

    The caller supplies admitted tools and owns consent, cancellation, durable
    checkpoints, and publication. No project state is written by this loop.
    """
    # The author supplies the actual rendered goal. Do not template it again:
    # a substituted source value may itself contain literal {{braces}}.
    system = (
        "You are a research agent for an investigative journalist, "
        "working on ONE row of a dataset. Use search/fetch to gather "
        "evidence, then finish with a precise, sourced answer. GROUND every "
        "factual claim in a source you found with the search or fetch tools "
        "before finalizing -- do not answer from memory alone. Reply with "
        "prose only -- never a raw tool call or a JSON object as your "
        f"answer. You have at most {MAX_STEPS} steps. Never fabricate."
    )
    sources: list[str] = []

    search_source, fetch_source = search, fetch

    async def search(query: str) -> str:
        obs, hits = await search_source(query)
        # the snippets it reads ARE its sources — cite them (deduped)
        for u in hits:
            if u and u not in sources:
                sources.append(u)
        return obs

    async def fetch(url: str) -> str:
        obs = await fetch_source(url)
        if url and url not in sources:
            sources.append(url)
        return obs[:FETCH_CHARS]

    model = FrisketRouterModel(
        router,
        model_id,
        recipe_version=recipe_version,
        max_tokens=MAX_STEP_OUTPUT_TOKENS,
    )
    agent = Agent(model, output_type=str, instructions=system, retries=RETRIES)
    for tool in (search, fetch):
        agent.tool_plain(tool, name=tool.__name__)

    malformed_answer_count = 0

    @agent.output_validator
    def prose_answer(answer: str) -> str:
        nonlocal malformed_answer_count
        if _answer_is_tool_shaped(answer):
            malformed_answer_count += 1
            raise ModelRetry(
                "Return the final answer as plain prose, not a tool call or "
                "JSON object."
            )
        return answer

    user_content = _content_to_pai(
        [
            *render_input_block(row_values),
            {"type": "text", "text": f"\nGoal: {goal}"},
        ]
    )
    try:
        run_result = await agent.run(
            user_content, usage_limits=UsageLimits(request_limit=MAX_STEPS)
        )
        answer = run_result.output or ""
    except UsageLimitExceeded:
        return _row_error(
            "research_incomplete_step_budget",
            "research incomplete -- the agent exhausted its step "
            "budget before producing a cited answer",
            model_id,
            model,
        )
    except UnexpectedModelBehavior:
        if malformed_answer_count:
            return _row_error(
                "research_answer_malformed",
                "research answer malformed -- the model emitted a tool call "
                "instead of a prose answer",
                model_id,
                model,
            )
        # A model turn with no text and no tool call
        # (nothing to branch on) finishes with answer="" rather than
        # erroring the row -- empty-finish parity.
        answer = ""
    answer_is_grounded = bool(sources)

    # Grounding guard: an answer produced without consulting ANY source is
    # ungrounded parametric memory (the loop never searched). ONE
    # verification nudge re-runs the loop demanding a search first; if it
    # then grounds the claim (sources appear) we adopt that grounded answer,
    # otherwise we KEEP the answer but mark it `unverified_memory` so the UI
    # can flag it -- "mark, don't destroy" (never a hard failure here).
    if answer.strip() and not answer_is_grounded:
        verify_content = _content_to_pai(
            [
                *render_input_block(row_values),
                {"type": "text", "text": f"\nGoal: {goal}"},
                {
                    "type": "text",
                    "text": (
                        "\nYou answered without consulting any source. Before "
                        "finalizing you MUST use the search tool to verify your "
                        "claims against public sources. Do not answer from "
                        "memory alone."
                    ),
                },
            ]
        )
        malformed_before_verification = malformed_answer_count
        try:
            verify_result = await agent.run(
                verify_content, usage_limits=UsageLimits(request_limit=MAX_STEPS)
            )
            verified = verify_result.output or ""
        except UsageLimitExceeded:
            verified = ""
        except UnexpectedModelBehavior:
            if malformed_answer_count > malformed_before_verification:
                return _row_error(
                    "research_answer_malformed",
                    "research answer malformed -- the model emitted a tool "
                    "call instead of a prose answer",
                    model_id,
                    model,
                )
            verified = ""
        # The same output validator owns this run too, so only clean prose
        # reaches this branch.
        if sources and verified.strip():
            answer = verified
            answer_is_grounded = True

    data: dict[str, Any] = {"answer": answer, "sources": sources}
    if answer.strip() and not answer_is_grounded:
        data["outcome"] = "unverified_memory"
    return (data, model_call_accounting(model_id, model.wire_calls))


def _row_error(
    code: str,
    message: str,
    model_id: str,
    model: FrisketRouterModel,
) -> tuple[dict, dict]:
    """Fail this ONE row with a typed cell error (model_error outcome,
    backfill-eligible) rather than persisting debris. The runner
    (row_execution.py) spreads ``error``/``error_code`` across every output
    field and derives the failed-row accounting from the outcome."""
    return (
        {
            "answer": None,
            "error": message,
            "error_code": code,
            "outcome": "model_error",
        },
        model_call_accounting(model_id, model.wire_calls),
    )


def model_call_accounting(engine: str, wire_calls: list[LLMResponse]) -> dict[str, Any]:
    """Every wire attempt this run made (``FrisketRouterModel.wire_calls``)
    becomes the same ``model_calls``/tokens/cost receipt shape the old
    per-step loop built inline, now built once after the run completes."""
    model_id = bare_model_name(engine)
    tokens = {"in": 0, "out": 0, "cost": 0.0}
    model_calls: list[dict[str, Any]] = []
    for resp in wire_calls:
        units = {"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out}
        if resp.cached:
            model_call = ModelCallMeta.cache_hit(
                capability="llm.complete",
                engine=engine,
                provider=resp.provider,
                provider_kind=PROVIDER_KIND.get(resp.provider, "platform_api"),
                model_ids=[model_id or engine],
                units=units,
                warnings=[],
            ).as_dict()
        else:
            model_call = ModelCallMeta.provider_call(
                capability="llm.complete",
                engine=engine,
                provider=resp.provider,
                provider_kind=PROVIDER_KIND.get(resp.provider, "platform_api"),
                model_ids=[model_id or engine],
                credential_source=resp.credential_source,
                provider_reported_cost_usd=resp.cost,
                provider_cost_usd=resp.cost,
                units=units,
                cost_source=resp.cost_source,
                warnings=[],
                # The router already measured this live wire call; carry
                # it by value rather than reporting an honest-looking but
                # false NULL.
                duration_ms=resp.duration_ms,
            ).as_dict()
        model_calls.append(model_call)
        tokens["in"] += resp.tokens_in
        tokens["out"] += resp.tokens_out
        # resp.cost None = unpriced model; one unknown call makes the
        # row's total unknown (None), never silently $0
        if resp.cached:
            pass  # cache hits bill nothing
        elif resp.cost is None:
            tokens["cost"] = None
        elif tokens["cost"] is not None:
            tokens["cost"] += resp.cost
    return {
        "tokens_in": tokens["in"],
        "tokens_out": tokens["out"],
        "cost": tokens["cost"],
        "model_calls": model_calls,
    }


async def search_web(query: str) -> tuple[str, list[str]]:
    """Returns (observation_text, result_urls)."""
    if not query.strip():
        return "empty query", []
    from ddgs import DDGS

    try:
        results = await asyncio.to_thread(lambda: DDGS().text(query, max_results=6))
    except Exception as e:  # noqa: BLE001
        return f"search failed: {e}", []
    results = results or []
    text = "\n".join(
        f"- {r.get('title')} | {r.get('href')}\n  {r.get('body', '')[:200]}"
        for r in results
    )
    urls = [r.get("href") for r in results if r.get("href")][:3]
    return text, urls


async def fetch_page(url: str, http: Any) -> str:
    """Read one page for the agent. The MODEL chooses this URL, so the
    guard has to be authoritative, not advisory: ``safe_request`` resolves
    once, dials the vetted literal, and re-vets + re-pins every redirect
    hop, closing the rebinding window a name-based fetch leaves open.

    Cross-origin redirects stay allowed (a credential-free GET has nothing
    to leak, and refusing them would break ordinary reading — an
    ``http://`` → ``https://`` upgrade is already cross-origin). An
    oversized page is a refusal the model sees as a failed tool call, not a
    silent clip: a page quietly truncated mid-way reads to the model as the
    whole page and becomes a confidently wrong, "sourced" answer.
    """
    from frisket.ops.netguard import safe_request

    if not url.startswith(("http://", "https://")):
        return "invalid url"
    try:
        resp = await safe_request(
            http,
            "GET",
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            max_bytes=FETCH_MAX_BYTES,
            cross_origin_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        # Every refusal (blocked URL, blocked/looping redirect, oversized or
        # encoded body, timeout) reaches the model as an observation it can
        # act on by choosing another source.
        return f"fetch failed: {e}"
    text = resp.content.decode("utf-8", errors="replace")
    # crude readability: strip tags, collapse whitespace
    text = re.sub(
        r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()
