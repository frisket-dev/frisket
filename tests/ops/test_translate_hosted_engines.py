"""The hosted-translation rollout — hosted translation engines (DeepL, Google Cloud Translation)
and their shared helpers. Every provider call is replayed through
``httpx.MockTransport``; NO live network (tests never touch the live network
by default). The DeepL happy-
path bodies come from a committed recording (tests/fixtures/deepl/*.json,
recorded once against the free tier); Google's are hand-authored from the
documented v2 schema (its key is unverified — see the module note in
integrations/google_translate.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from frisket.ops.integrations.deepl import deepl_base_url, deepl_translate
from frisket.ops.integrations.google_translate import google_translate
from frisket.ops.integrations.translate_common import (
    TranslateEngineError,
    normalize_detected_bcp47,
    normalize_language,
)
from frisket.ops.base import OpContext
from frisket.ops.integrations.translation_engine import TranslationEngine
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.credential_use import CredentialUseContext


@pytest.fixture(autouse=True)
def _pin_zero_standing_preapproval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted translation is a paid provider call, and the cases below that
    assert ``needs_confirmation`` need the gate to fire. The product default
    of $2 already covers these small translations, so they would dispatch
    instead. Pinning zero standing preapproval keeps the paid-path challenge
    reachable without changing any assertion.
    """

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "deepl" / name).read_text(encoding="utf-8"))


def _load_google(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "google" / name).read_text(encoding="utf-8"))


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _op_context(project: Any, http: httpx.AsyncClient) -> OpContext:
    return OpContext(
        project=project,
        http=http,
        credential_use_context=CredentialUseContext.open(),
    )


async def _run_with_confirmation(runner, spec):  # noqa: ANN001, ANN201
    """Exercise the real 402-style gate, then echo its exact scope/quote hash."""
    import asyncio
    from types import SimpleNamespace
    from frisket.engine.executor import run_action_spec

    output = spec.get("output_name", "translation")
    detected = bool(spec.get("save_detected_language"))
    body = {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": spec["sheet_id"]},
        "params": {
            "source": spec["input_columns"],
            "engine": spec["engine"],
            "target_language": spec["target_language"],
            "save_detected_language": detected,
        },
        "output_names": {
            "translation": output,
            **(
                {"detected_language": output + "_detected_language"} if detected else {}
            ),
        },
        "replace_existing": bool(
            runner.project.columns(spec["sheet_id"])
            and any(
                c["name"] == output for c in runner.project.columns(spec["sheet_id"])
            )
        ),
        "idempotency_key": f"translate-{detected}",
    }

    def run():
        challenge = run_action_spec(
            runner.project, body, project_id="translate-test", router=runner.router
        )
        assert challenge.status == "needs_confirmation", challenge
        body["confirmation"] = challenge.errors[0].details["promise_set_hash"]
        result = run_action_spec(
            runner.project, body, project_id="translate-test", router=runner.router
        )
        assert result.status in {"completed", "partial", "succeeded"}, result
        failed = runner.project.db.execute(
            "SELECT count(DISTINCT row_id) FROM results WHERE error_code IS NOT NULL"
        ).fetchone()[0]
        return SimpleNamespace(failed=failed)

    return await asyncio.to_thread(run)


# --- detected-language BCP-47 (stored value) -------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("EN", "en"),  # DeepL upper
        ("PT-BR", "pt-BR"),  # DeepL region -> region UPPER
        ("zh-CN", "zh-CN"),  # Google region
        ("zh-hans", "zh-Hans"),  # script subtag Titlecase
        ("de", "de"),
        ("en_US", "en-US"),  # underscore separator
        ("English", "en"),  # LLM prose maps via the name table
        ("Portuguese", "pt"),
        ("notalanguage", None),  # unknown prose -> None, never stored raw
        ("", None),
        (None, None),
    ],
)
def test_normalize_detected_bcp47(raw, expected) -> None:
    assert normalize_detected_bcp47(raw) == expected


# --- request-time language normalization is role-aware and region-safe


def test_llm_language_passes_through() -> None:
    assert normalize_language("llm", "Japanese", "target") == "Japanese"


@pytest.mark.parametrize(
    "target,expected",
    [
        ("Spanish", "ES"),
        ("japanese", "JA"),
        ("english", "EN"),  # bare stays bare (EN is a valid DeepL target, verified)
        ("pt", "PT"),  # NOT PT-BR — no invented default
        ("Chinese", "ZH"),
        ("en-GB", "EN-GB"),  # explicit region PRESERVED, not EN-US
        ("pt-PT", "PT-PT"),
        ("zh-Hans", "ZH-HANS"),
    ],
)
def test_deepl_target_normalization(target, expected) -> None:
    assert normalize_language("deepl", target, "target") == expected


@pytest.mark.parametrize(
    "source,expected",
    [("en-GB", "EN"), ("pt-PT", "PT"), ("English", "EN"), ("zh-Hans", "ZH")],
)
def test_deepl_source_drops_region(source, expected) -> None:
    # DeepL source_lang rejects a region (verified live 2026-07-17): the bare
    # language is the provider's documented rule, dropped here not silently.
    assert normalize_language("deepl", source, "source") == expected


@pytest.mark.parametrize(
    "target,expected",
    [
        ("Spanish", "es"),
        ("Japanese", "ja"),
        ("Chinese", "zh"),  # bare, no invented zh-CN default
        ("zh-TW", "zh-TW"),  # explicit region PRESERVED
        ("de", "de"),
    ],
)
def test_google_target_normalization(target, expected) -> None:
    assert normalize_language("google_translate", target, "target") == expected


def test_google_source_preserves_region() -> None:
    assert normalize_language("google_translate", "zh-TW", "source") == "zh-TW"


def test_unknown_target_fails_once_with_invalid_target() -> None:
    with pytest.raises(TranslateEngineError) as exc:
        normalize_language("deepl", "Klingon", "target")
    assert exc.value.code == "invalid_target"


def test_unknown_source_fails_with_invalid_source() -> None:
    with pytest.raises(TranslateEngineError) as exc:
        normalize_language("deepl", "Klingon", "source")
    assert exc.value.code == "invalid_source"


# --- DeepL base-url selection ----------------------------------------------


def test_deepl_free_vs_pro_endpoint() -> None:
    assert deepl_base_url("abc:fx") == "https://api-free.deepl.com"
    assert deepl_base_url("abc-pro-key") == "https://api.deepl.com"


# --- DeepL client (recorded fixtures) --------------------------------------


@pytest.mark.asyncio
async def test_deepl_translate_basic_replays_recorded_body() -> None:
    fixture = _load("translate_basic.json")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=fixture)

    async with _mock(handler) as http:
        out = await deepl_translate(http, "test-key:fx", ["Hello world"], "ES")

    # request went to the free endpoint with the DeepL auth scheme
    assert captured["url"] == "https://api-free.deepl.com/v2/translate"
    assert captured["auth"] == "DeepL-Auth-Key test-key:fx"
    assert captured["body"]["target_lang"] == "ES"
    # response mapped to the normalized {text, detected_source_language} shape
    assert out[0]["text"] == fixture["translations"][0]["text"]
    assert (
        out[0]["detected_source_language"]
        == fixture["translations"][0]["detected_source_language"]
    )


@pytest.mark.asyncio
async def test_deepl_auth_failure_is_typed() -> None:
    # Replays the recorded 403 body (bogus-key call, tests/fixtures/deepl).
    recorded = _load("auth_failure.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(recorded["status"], json=recorded["body"])

    async with _mock(handler) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await deepl_translate(http, "bogus:fx", ["Hi"], "ES")
    assert exc.value.code == "auth"


@pytest.mark.asyncio
async def test_deepl_invalid_target_body_classified_from_message() -> None:
    # Replays the recorded 400 body (target_lang=XX). The structured
    # message ("target_lang ... not supported") classifies this as
    # invalid_target, not a generic bad_request.
    recorded = _load("invalid_target.json")
    assert "target_lang" in recorded["body"]["message"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(recorded["status"], json=recorded["body"])

    async with _mock(handler) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await deepl_translate(http, "k:fx", ["Hi"], "XX")
    assert exc.value.code == "invalid_target"


@pytest.mark.asyncio
async def test_deepl_source_lang_400_is_invalid_source() -> None:
    body = {"message": "Bad request. Reason: Value for 'source_lang' not supported."}

    async with _mock(lambda r: httpx.Response(400, json=body)) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await deepl_translate(http, "k:fx", ["Hi"], "ES", "EN-GB")
    assert exc.value.code == "invalid_source"


@pytest.mark.asyncio
async def test_deepl_quota_is_retryable_only_on_429() -> None:
    for status, retryable in ((429, True), (456, False)):

        def handler(request: httpx.Request, _s=status) -> httpx.Response:
            return httpx.Response(_s, json={})

        async with _mock(handler) as http:
            with pytest.raises(TranslateEngineError) as exc:
                await deepl_translate(http, "k:fx", ["Hi"], "ES")
        assert exc.value.code == "quota"
        assert exc.value.retryable is retryable


# --- Google client (hand-authored fixtures) --------------------------------


@pytest.mark.asyncio
async def test_google_translate_maps_v2_schema() -> None:
    # Replays a recorded live body (tests/fixtures/google/translate_basic.json).
    recorded = _load_google("translate_basic.json")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.url.params.get("key")
        captured["body"] = json.loads(request.read())
        return httpx.Response(recorded["status"], json=recorded["body"])

    async with _mock(handler) as http:
        out = await google_translate(http, "g-key", ["Hello, world."], "es")

    assert captured["key"] == "g-key"
    assert captured["body"]["target"] == "es"
    assert out[0]["text"] == "Hola Mundo."
    assert out[0]["detected_source_language"] == "en"


@pytest.mark.asyncio
async def test_google_translate_unescapes_html_entities() -> None:
    body = {
        "data": {
            "translations": [
                {"translatedText": "Tom &amp; Jerry", "detectedSourceLanguage": "en"}
            ]
        }
    }

    async with _mock(lambda r: httpx.Response(200, json=body)) as http:
        out = await google_translate(http, "g-key", ["Tom & Jerry"], "es")
    assert out[0]["text"] == "Tom & Jerry"


@pytest.mark.asyncio
async def test_google_auth_failure_is_typed() -> None:
    body = {"error": {"code": 403, "message": "API key not valid"}}

    async with _mock(lambda r: httpx.Response(403, json=body)) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await google_translate(http, "bad", ["Hi"], "es")
    assert exc.value.code == "auth"
    assert "API key not valid" in exc.value.message


@pytest.mark.asyncio
async def test_google_permission_vs_quota_from_reason() -> None:
    # A 403 is permission or quota depending on the structured
    # reason/status, not the status code alone.
    perm = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "errors": [{"reason": "accessNotConfigured"}],
            "message": "Cloud Translation API has not been used",
        }
    }
    async with _mock(lambda r: httpx.Response(403, json=perm)) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await google_translate(http, "k", ["Hi"], "es")
    assert exc.value.code == "permission"

    quota = {
        "error": {
            "code": 403,
            "errors": [{"reason": "rateLimitExceeded"}],
            "message": "Rate Limit Exceeded",
        }
    }
    async with _mock(lambda r: httpx.Response(403, json=quota)) as http:
        with pytest.raises(TranslateEngineError) as exc:
            await google_translate(http, "k", ["Hi"], "es")
    assert exc.value.code == "quota"
    assert exc.value.retryable is True


# --- recipe execute() dispatch (end to end through the engine axis) ---------


class _StubProject:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def secret_plaintext(self, name: str):
        return self._secrets.get(name)


def _data(out):
    """execute() returns (data, meta) for hosted engines; unwrap the data."""
    return out[0] if isinstance(out, tuple) else out


def _meta(out):
    return out[1] if isinstance(out, tuple) else {}


@pytest.mark.asyncio
async def test_recipe_execute_google_explicit_source_fallback() -> None:
    # Google omits detectedSourceLanguage when source is explicit —
    # fall back to the (normalized) explicit source instead of null. Opt-in
    # detected column emitted under the derived name (item 1).
    body = {"data": {"translations": [{"translatedText": "Hola"}]}}

    async with _mock(lambda r: httpx.Response(200, json=body)) as http:
        ctx = _op_context(_StubProject({"GOOGLE_TRANSLATE_API_KEY": "g"}), http)
        out = await TranslationEngine().execute(
            {"statement": "Hello"},
            {
                "engine": "google_translate",
                "target_language": "Spanish",
                "language": ["en-GB"],
                "output_name": "es",
                "save_detected_language": True,
            },
            ctx,
        )
    assert _data(out) == {"translation": "Hola", "detected_language": "en-GB"}


@pytest.mark.asyncio
async def test_recipe_execute_deepl_returns_translation_and_bcp47(monkeypatch) -> None:
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    fixture = _load("translate_basic.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "DeepL-Auth-Key secret:fx"
        return httpx.Response(200, json=fixture)

    async with _mock(handler) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "secret:fx"}), http)
        out = await TranslationEngine().execute(
            {"statement": "Hello, world."},
            {
                "engine": "deepl",
                "target_language": "Spanish",
                "output_name": "es",
                "save_detected_language": True,
            },
            ctx,
        )
    # detected_source_language "EN" -> canonical BCP-47 "en"; billed_characters
    # 13 * the pinned DeepL rate -> a real (non-zero) recorded cost (item 2).
    assert _data(out) == {"translation": "Hola, mundo.", "detected_language": "en"}
    assert _meta(out)["cost"] > 0


@pytest.mark.asyncio
async def test_recipe_execute_records_unknown_cost_when_rate_missing(
    monkeypatch,
) -> None:
    # F6: a hosted engine whose per-character rate is unconfigured records the
    # spend as UNKNOWN (cost=None + cost_source), never a fake 0 that reads as
    # free. hosted_translate_cost resolves the rate through external_unit_price_usd.
    import frisket.ai.external_pricing as ep

    monkeypatch.setattr(ep, "external_unit_price_usd", lambda key: None)
    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")
    fixture = _load("translate_basic.json")

    async with _mock(lambda r: httpx.Response(200, json=fixture)) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "k:fx"}), http)
        out = await TranslationEngine().execute(
            {"statement": "Hello, world."},
            {"engine": "deepl", "target_language": "Spanish", "output_name": "es"},
            ctx,
        )
    assert isinstance(out, tuple)  # billable engine still returns a cost channel
    assert _meta(out)["cost"] is None  # unknown, NOT 0.0
    assert _meta(out).get("cost_source") == "unknown"


@pytest.mark.asyncio
async def test_recipe_execute_default_omits_detected_column(monkeypatch) -> None:
    # item 1: default (no save_detected_language) emits ONLY the translation.
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    fixture = _load("translate_basic.json")

    async with _mock(lambda r: httpx.Response(200, json=fixture)) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "secret:fx"}), http)
        out = await TranslationEngine().execute(
            {"statement": "Hello, world."},
            {"engine": "deepl", "target_language": "Spanish", "output_name": "es"},
            ctx,
        )
    assert _data(out) == {"translation": "Hola, mundo."}


@pytest.mark.asyncio
async def test_recipe_execute_empty_provider_result_is_typed_row_failure(monkeypatch):
    # F1 (eval failure-modes): a provider that returns an EMPTY `text` over
    # NON-EMPTY source text is a per-row failure, NOT a terminal-ok empty cell.
    # Previously `first.get("text", "")` wrote "" as a green `ok` cell backfill
    # never revisited. Now it raises a typed error (empty_output) that MapRunner
    # records as a TERMINAL failure (empty-output terminal-row contract):
    # retryable=False because automation never retries it — only a deliberate
    # user retry (run.backfill with explicit row_ids) may re-run the row.
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    empty_body = {"translations": [{"text": "", "detected_source_language": "EN"}]}

    async with _mock(lambda r: httpx.Response(200, json=empty_body)) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "secret:fx"}), http)
        with pytest.raises(TranslateEngineError) as exc:
            await TranslationEngine().execute(
                {"statement": "Hello, world."},
                {"engine": "deepl", "target_language": "Spanish", "output_name": "es"},
                ctx,
            )
    assert exc.value.code == "empty_output"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_recipe_execute_empty_source_stays_a_legitimate_empty(monkeypatch):
    # The other side of F1: a genuinely EMPTY source (nothing to translate) is
    # NOT a failure — it returns cleanly without ever reaching the provider, so
    # legitimately-empty inputs are undisturbed.
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("provider must not be called for empty source")

    async with _mock(handler) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "secret:fx"}), http)
        out = await TranslationEngine().execute(
            {"statement": ""},
            {"engine": "deepl", "target_language": "Spanish", "output_name": "es"},
            ctx,
        )
    assert _data(out) == {"translation": ""}


@pytest.mark.asyncio
async def test_recipe_execute_missing_key_is_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    async with _mock(lambda r: httpx.Response(200, json={})) as http:
        ctx = _op_context(_StubProject({}), http)
        with pytest.raises(TranslateEngineError) as exc:
            await TranslationEngine().execute(
                {"statement": "Hi"},
                {"engine": "deepl", "target_language": "Spanish"},
                ctx,
            )
    assert exc.value.code == "auth"


@pytest.mark.asyncio
async def test_recipe_execute_blank_source_skips_the_provider() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={})

    async with _mock(handler) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "s:fx"}), http)
        out = await TranslationEngine().execute(
            {"statement": "   "},
            {"engine": "deepl", "target_language": "Spanish", "output_name": "es"},
            ctx,
        )
    assert _data(out) == {"translation": ""}
    assert _meta(out)["cost"] == 0.0  # no chars -> no spend
    assert calls["n"] == 0  # no wasted paid call on an empty row


@pytest.mark.asyncio
async def test_recipe_execute_invalid_target_fails_before_calling(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={})

    async with _mock(handler) as http:
        ctx = _op_context(_StubProject({"DEEPL_API_KEY": "s:fx"}), http)
        with pytest.raises(TranslateEngineError) as exc:
            await TranslationEngine().execute(
                {"statement": "Hi"},
                {"engine": "deepl", "target_language": "Klingon", "output_name": "es"},
                ctx,
            )
    assert exc.value.code == "invalid_target"
    assert calls["n"] == 0


# --- per-row error isolation through the real MapRunner ---------------------


def test_hosted_translate_row_error_stays_per_row(tmp_path, monkeypatch) -> None:
    """A 2-row DeepL run where row 1 hits a quota error must still translate
    row 2 — the TranslateEngineError must NOT escape MapRunner's per-row
    boundary and abort the run, and the taxonomy code lands in error_code."""
    import asyncio

    from frisket.ai.llm import ModelRouter
    from frisket.engine.runner import MapRunner
    from frisket.engine.store import Project

    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")
    project = Project.create(tmp_path / "t.frisket", name="t")
    sheet_id = project.add_sheet("s")
    col = {"statement": project.add_column(sheet_id, "statement", type="text")}
    row_ids = project.add_rows(
        sheet_id,
        [{"statement": "quota-row"}, {"statement": "ok-row"}],
        col,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.read())["text"][0]
        if "quota" in text:
            return httpx.Response(429, json={})
        return httpx.Response(
            200,
            json={
                "translations": [{"text": "OK-ES", "detected_source_language": "EN"}]
            },
        )

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001

    progress = asyncio.run(
        _run_with_confirmation(
            MapRunner(project, router, authority=UnroutedOnlyAuthority(project)),
            {
                "action_kind": "map.translate",
                "sheet_id": sheet_id,
                "input_columns": ["statement"],
                "engine": "deepl",
                "target_language": "Spanish",
                "output_name": "es",
            },
        )
    )

    # The run finished (did not abort): exactly one row failed, one completed.
    assert progress.failed == 1
    cols = {c["name"]: c for c in project.columns(sheet_id)}
    values = project.get_values(sheet_id, cols["es"]["id"])
    assert values[row_ids[1]] == "OK-ES"  # row 2 still translated
    assert values[row_ids[0]] is None  # row 1 failed
    codes = project.db.execute(
        "SELECT error_code FROM results WHERE row_id=? AND error_code IS NOT NULL",
        (row_ids[0],),
    ).fetchall()
    assert any(row[0] == "quota" for row in codes)  # taxonomy preserved
    project.close()


# --- item 1: opt-in flip retires the prior detected-language column ---------


def test_hosted_translate_preserves_prior_detected_values_when_flag_turns_off(
    tmp_path, monkeypatch
) -> None:
    """Replacing only translation preserves independently named prior outputs."""
    import asyncio

    from frisket.ai.llm import ModelRouter
    from frisket.engine.runner import MapRunner
    from frisket.engine.store import Project

    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")
    project = Project.create(tmp_path / "t.frisket", name="t")
    sheet_id = project.add_sheet("s")
    col = {"statement": project.add_column(sheet_id, "statement", type="text")}
    project.add_rows(sheet_id, [{"statement": "Hello"}], col)

    def handler(request: httpx.Request) -> httpx.Response:
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

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001

    base = {
        "action_kind": "map.translate",
        "sheet_id": sheet_id,
        "input_columns": ["statement"],
        "engine": "deepl",
        "target_language": "Spanish",
        "output_name": "es",
    }
    # F2: a hosted run egresses source text -> confirm before egress.
    asyncio.run(
        _run_with_confirmation(
            MapRunner(project, router, authority=UnroutedOnlyAuthority(project)),
            {**base, "save_detected_language": True},
        )
    )
    cols = {c["name"]: c for c in project.columns(sheet_id, include_hidden=True)}
    assert "es_detected_language" in cols
    assert cols["es_detected_language"]["default_hidden"] == 0
    prior_detected = project.get_values(sheet_id, cols["es_detected_language"]["id"])

    # The new invocation owns only translation. Previous detection remains
    # available with its original values and provenance.
    asyncio.run(
        _run_with_confirmation(
            MapRunner(project, router, authority=UnroutedOnlyAuthority(project)),
            base,
        )
    )
    cols = {c["name"]: c for c in project.columns(sheet_id, include_hidden=True)}
    assert (
        project.get_values(sheet_id, cols["es_detected_language"]["id"])
        == prior_detected
    )
    assert cols["es"]["default_hidden"] == 0
    project.close()
