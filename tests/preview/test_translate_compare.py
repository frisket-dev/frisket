"""The translation contract (Route A) — the translate-compare scratch bake-off backend.

Runs a text sample through several engines side by side; NO project writes, NO
live provider calls (every request replays httpx.MockTransport — tests never
touch the live network by default). Engine dispatch reuses TranslationEngine, so this drives exactly as the
durable map.translate action."""

from __future__ import annotations

import json

import httpx
import pytest

import frisket.preview.translate as translate_compare
from frisket.preview.common import BillablePreviewDispatch
from frisket.preview.translate import (
    TranslateComparePreviewError,
    compare_translate_scratch,
)


COMPARE_SECRET = "sk-compare-preview-secret-1234"
CODE_SECRET = "frisket_pat_abcdefghijklmno"


def _router_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if "deepl" in host:
        return httpx.Response(
            200,
            json={
                "translations": [
                    {
                        "text": "Hola",
                        "detected_source_language": "EN",
                        "billed_characters": 5,
                    }
                ]
            },
        )
    if "googleapis" in host:
        return httpx.Response(
            200,
            json={
                "data": {
                    "translations": [
                        {"translatedText": "Hola G", "detectedSourceLanguage": "en"}
                    ]
                }
            },
        )
    raise AssertionError(f"unexpected host {host}")


@pytest.mark.asyncio
async def test_compare_runs_selected_engines_side_by_side(monkeypatch) -> None:
    # The FREE local engines run side by side. Stubbed at
    # ``_run_engine_with_project`` because opus_mt/hy_mt2 runtimes are not
    # provisioned in the test env; the billable-engine fence lives one level
    # lower (see test_billable_engine_at_dispatch_raises_loudly), so this stub
    # does not hide it.
    async def fake_run(engine, req, *, http, project):
        del req, http, project
        return {"translation": f"Hola from {engine}", "detected_language": "en"}

    monkeypatch.setattr(translate_compare, "_run_engine_with_project", fake_run)
    out = await compare_translate_scratch(
        None,
        {
            "engines": ["opus_mt", "hy_mt2"],
            "text": "Hello",
            "target_language": "Spanish",
        },
    )
    assert out["schema_version"] == "frisket.translate_compare_preview.v1"
    assert out["source"] == {"scratch": True, "text_length": 5}
    by_engine = {r["engine"]: r for r in out["results"]}
    assert by_engine["opus_mt"]["translation"] == "Hola from opus_mt"
    assert by_engine["hy_mt2"]["translation"] == "Hola from hy_mt2"
    assert out["errors"] == []
    # Compare is the free surface: no cost block on the wire at all.
    assert "cost" not in out


@pytest.mark.asyncio
async def test_compare_isolates_a_failing_engine(monkeypatch) -> None:
    async def fake_run(engine, req, *, http, project):
        del req, http, project
        if engine == "hy_mt2":
            error = RuntimeError("model weights missing")
            error.code = "auth"
            raise error
        return {"translation": "Hola", "detected_language": "en"}

    monkeypatch.setattr(translate_compare, "_run_engine_with_project", fake_run)
    out = await compare_translate_scratch(
        None,
        {
            "engines": ["opus_mt", "hy_mt2"],
            "text": "Hello",
            "target_language": "Spanish",
        },
    )
    by_engine = {r["engine"]: r for r in out["results"]}
    # opus_mt still translated; hy_mt2 failed but did not abort the run.
    assert by_engine["opus_mt"]["translation"] == "Hola"
    assert by_engine["hy_mt2"]["translation"] == ""
    assert by_engine["hy_mt2"]["errors"]
    assert any(e["engine"] == "hy_mt2" and e["code"] == "auth" for e in out["errors"])


@pytest.mark.asyncio
async def test_compare_redacts_failures_and_canonicalizes_untrusted_codes(
    monkeypatch,
) -> None:
    async def fake_run(
        engine,
        req,
        *,
        http,
        project,
    ):
        del req, http, project
        if engine == "hy_mt2":
            error = RuntimeError(f"provider quota exhausted; api_key={COMPARE_SECRET}")
            # This credential is also syntactically valid lower_snake_case.
            error.code = CODE_SECRET
            raise error
        return {
            "translation": f"{engine} translated",
            "detected_language": "en",
        }

    monkeypatch.setattr(translate_compare, "_run_engine_with_project", fake_run)
    out = await compare_translate_scratch(
        None,
        {
            "engines": ["opus_mt", "hy_mt2", "hy_mt2_alias"],
            "text": "Hello",
            "target_language": "Spanish",
        },
    )

    assert [result["engine"] for result in out["results"]] == [
        "opus_mt",
        "hy_mt2",
        "hy_mt2_alias",
    ]
    assert COMPARE_SECRET not in json.dumps(out)
    assert CODE_SECRET not in json.dumps(out)
    assert out["errors"] == [
        {
            "code": "translate_preview_engine_failed",
            "engine": "hy_mt2",
            "message": "provider quota exhausted; api_key=[REDACTED]",
        }
    ]
    assert out["results"][1]["errors"] == [out["errors"][0]["message"]]
    assert out["results"][2]["translation"] == "hy_mt2_alias translated"


@pytest.mark.parametrize("engine", ["deepl", "google_translate", "llm"])
@pytest.mark.asyncio
async def test_billable_engines_are_refused_and_never_dispatch(
    engine, monkeypatch
) -> None:
    # Billable -> run. Every engine that bills real money is refused while the
    # request is coerced, so no provider call can be made and there is no
    # allow_remote to flip. The refusal names the cost-gated action.
    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"translations": [{"text": "x"}]})

    async with _router_client(handler) as http:
        with pytest.raises(TranslateComparePreviewError) as exc:
            await compare_translate_scratch(
                None,
                {
                    "engines": [engine, "opus_mt"],
                    "text": "Hello",
                    "target_language": "Spanish",
                    # There is no allow_remote knob any more.
                    "allow_remote": True,
                },
                http=http,
            )
    assert exc.value.code == "billable_engine_requires_run"
    assert exc.value.details == {"engine": engine, "action": "map.translate"}
    assert "map.translate" in exc.value.message
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_local_engines_need_no_consent(monkeypatch) -> None:
    # The other half of the rule: not billable -> preview, with no dialog, no
    # run and no ledger entry. Nothing about the free path changed.
    async def fake_run(engine, req, *, http, project):
        del req, http, project
        return {"translation": f"{engine} ok", "detected_language": "en"}

    monkeypatch.setattr(translate_compare, "_run_engine_with_project", fake_run)
    out = await compare_translate_scratch(
        None, {"engines": ["opus_mt"], "text": "Hello", "target_language": "Spanish"}
    )
    assert out["results"][0]["translation"] == "opus_mt ok"
    assert out["errors"] == []


@pytest.mark.asyncio
async def test_billable_engine_at_dispatch_raises_loudly() -> None:
    # The effect-site fence, proven load-bearing by calling the dispatch
    # helper directly (i.e. as if the coercion refusal above had a hole).
    # It must NOT degrade into a per-engine error inside a 200.
    req = translate_compare.TranslateCompareScratchRequest(
        engines=["deepl"], text="Hello", target_language="Spanish"
    )
    async with _router_client(_handler) as http:
        with pytest.raises(BillablePreviewDispatch):
            await translate_compare._run_engine_with_project(
                "deepl",
                req,
                http=http,
                project=None,
            )


@pytest.mark.asyncio
async def test_validation_errors() -> None:
    with pytest.raises(TranslateComparePreviewError) as exc:
        await compare_translate_scratch(None, {"engines": [], "text": "hi"})
    assert exc.value.field == "engines"

    with pytest.raises(TranslateComparePreviewError) as exc:
        await compare_translate_scratch(None, {"engines": ["deepl"], "text": "   "})
    assert exc.value.field == "text"
