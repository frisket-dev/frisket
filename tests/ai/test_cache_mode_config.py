"""onboard-replay-banner-v1: the active router cache-mode/config posture is
exposed via an API the frontend can read (both the local server and the
hosted app, which construct routers differently), the replay default remains
cache-first, and a
replay-mode cache MISS surfaces as a distinct not-success run result instead
of crashing the run or silently producing an empty cell.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import (
    ModelRouter,
    ResponseCache,
    live_calls_possible,
    resolve_env_cache_mode,
)
from typed_model_fixtures import model_plan
from frisket.engine.recipe_fence_posture import recipe_fence_posture
from frisket.engine.runner import MapRunner
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.server.app import create_app
from frisket.engine.store import Project
from typed_model_fixtures import run_with_output_claim

MODEL = "anthropic/claude-haiku-4-5"


# ---------------------------------------------------------------------------
# live_calls_possible / dev-default posture


class TestLiveCallsPossible:
    def test_replay_strict_is_the_only_no_live_call_mode(self):
        """replay_strict is the ONLY posture that can never make a live call
        (a strict-replay miss raises CacheMiss instead of calling out)."""
        assert live_calls_possible("replay_strict") is False

    def test_replay_can_make_live_calls_on_a_miss(self):
        """replay is cache-FIRST, not cache-ONLY: a miss falls through to a
        live wire attempt (router._complete_transport)."""
        assert live_calls_possible("replay") is True

    def test_fresh_and_off_can_make_live_calls(self):
        assert live_calls_possible("fresh") is True
        assert live_calls_possible("off") is True

    def test_router_default_cache_mode_is_still_replay(self):
        """The default mode stays replay: cache-first with a live call on a
        miss, so live_calls_possible is True."""
        router = ModelRouter()
        assert router.cache_mode == "replay"
        assert live_calls_possible(router.cache_mode) is True


class TestResolveEnvCacheMode:
    def test_absent_env_var_defaults_to_replay(self, monkeypatch):
        monkeypatch.delenv("FRISKET_CACHE_MODE", raising=False)
        assert resolve_env_cache_mode() == "replay"

    def test_empty_env_var_defaults_to_replay(self, monkeypatch):
        monkeypatch.setenv("FRISKET_CACHE_MODE", "")
        assert resolve_env_cache_mode() == "replay"

    def test_each_valid_mode_is_honoured(self):
        for mode in ("replay", "fresh", "replay_strict", "off"):
            assert resolve_env_cache_mode({"FRISKET_CACHE_MODE": mode}) == mode

    def test_unknown_value_fails_loudly(self):
        with pytest.raises(ValueError, match="not a valid cache mode"):
            resolve_env_cache_mode({"FRISKET_CACHE_MODE": "cached"})

    def test_workspace_honours_the_env_knob(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRISKET_CACHE_MODE", "off")
        client = TestClient(create_app(tmp_path / "workspace"))
        body = client.get("/api/config").json()
        assert body["cache_mode"] == "off"
        assert body["live_calls_possible"] is True

    def test_workspace_rejects_a_bad_env_knob_at_startup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRISKET_CACHE_MODE", "nope")
        with pytest.raises(ValueError, match="not a valid cache mode"):
            create_app(tmp_path / "workspace")


# ---------------------------------------------------------------------------
# Local server: GET /api/config


class TestLocalServerRuntimeConfig:
    def test_default_workspace_reports_replay_with_live_calls_possible(self, tmp_path):
        client = TestClient(create_app(tmp_path / "workspace"))
        resp = client.get("/api/config")
        assert resp.status_code == 200
        # The default posture stays replay, now honestly reported: a replay
        # cache miss falls through to a live call, so live_calls_possible True.
        # onboard2-signin-sender-copy-v1: the local tier sends no email, so
        # the sender-identity fields are an honest null (full sender-identity
        # contract pinned in tests/test_runtime_config_sender.py).
        # recipe_fence_posture: what confines a map.python recipe on this
        # host, per platform (contract pinned in
        # tests/server/test_runtime_config_recipe_fence.py) -- asserted through
        # the mint so this stays an exact-payload test on every platform.
        assert resp.json() == {
            "cache_mode": "replay",
            "live_calls_possible": True,
            "cache_mode_editable": True,
            "cost_preapproval_usd": "2",
            "cost_preapproval_editable": True,
            "in_container": False,
            "recipe_fence_posture": recipe_fence_posture(),
            "email_from_address": None,
            "email_from_name": None,
            "auth_methods": {
                "password": False,
                "magic_link": False,
                "oidc": [],
            },
            "plugins_available": True,
            "plugin_management_available": True,
            "product_telemetry_available": False,
        }

    def test_reflects_an_injected_router_in_a_live_call_capable_mode(self, tmp_path):
        cache = ResponseCache(tmp_path / "c.db")
        router = ModelRouter(cache=cache, cache_mode="fresh")
        client = TestClient(create_app(tmp_path / "workspace", router=router))
        resp = client.get("/api/config")
        assert resp.json() == {
            "cache_mode": "fresh",
            "live_calls_possible": True,
            "cache_mode_editable": False,
            "cost_preapproval_usd": "2",
            "cost_preapproval_editable": False,
            "in_container": False,
            "recipe_fence_posture": recipe_fence_posture(),
            "email_from_address": None,
            "email_from_name": None,
            "auth_methods": {
                "password": False,
                "magic_link": False,
                "oidc": [],
            },
            "plugins_available": True,
            "plugin_management_available": True,
            "product_telemetry_available": False,
        }

    def test_config_route_precedes_health_route_naming_and_is_getonly(self, tmp_path):
        app = create_app(tmp_path / "workspace")
        route = next(r for r in app.routes if getattr(r, "path", "") == "/api/config")
        assert sorted(m for m in route.methods if m != "HEAD") == ["GET"]


# ---------------------------------------------------------------------------
# Hosted app: HostedConfig.cache_mode + GET /api/config


def _hosted_env(monkeypatch, tmp_path, **extra: str) -> None:
    monkeypatch.setenv("FRISKET_DATABASE_URL", f"sqlite:///{tmp_path}/control.db")
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRISKET_ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("FRISKET_BASE_URL", "http://test")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# A replay_strict cache MISS is a distinct not-success run result, not a
# crash and not a silent empty cell (previously CacheMiss was UNCAUGHT in
# MapRunner._execute_row's except tuple).


CLASSIFY_SPEC_FIELDS = [
    {"name": "relevance", "type": "score", "description": "0-10 relevance"}
]


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def _seed_sheet(p: Project, texts: list[str]) -> tuple[int, dict]:
    sheet = p.add_sheet("data")
    cols = {"text": p.add_column(sheet, "text")}
    p.add_rows(sheet, [{"text": t} for t in texts], cols)
    return sheet, cols


def _classify_spec(sheet_id: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": CLASSIFY_SPEC_FIELDS,
    }


class TestReplayStrictCacheMissIsDistinctFromSuccess:
    def test_miss_fails_the_row_instead_of_crashing_the_run(self, project, tmp_path):
        sheet, _ = _seed_sheet(project, ["uncached row"])
        spec = _classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")  # never primed: guaranteed miss
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))

        progress = asyncio.run(
            run_with_output_claim(runner, spec)
        )  # must not raise CacheMiss

        # `completed` is rows PROCESSED (success or failure), `failed` the
        # subset of those that failed — mirrors test_runner.py's
        # TestPartialFailure convention.
        assert progress.done is True
        assert progress.completed == 1
        assert progress.failed == 1

    def test_miss_outcome_is_not_success_and_not_a_silent_empty_result(
        self, project, tmp_path
    ):
        sheet, _ = _seed_sheet(project, ["uncached row"])
        spec = _classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        progress = asyncio.run(run_with_output_claim(runner, spec))

        # all-rows-failed-no-columns-v1: a zero-success run hides its created
        # column, so it must be looked up with include_hidden=True here.
        col = next(
            c
            for c in project.columns(sheet, include_hidden=True)
            if c["name"] == "relevance"
        )
        row = project.db.execute(
            "SELECT outcome, error, value FROM results WHERE run_id=? AND column_id=?",
            (progress.run_id, col["id"]),
        ).fetchone()
        assert row is not None
        # not the "empty" default and not a success value
        assert row["outcome"] in ("model_error", "invalid_output")
        assert row["outcome"] != "ok"
        assert row["value"] is None
        # a helpful, distinguishing message — not a generic/blank error.
        # the regression guard (commit c6a9613b) deliberately
        # replaced CacheMiss's developer-facing text (which told real users
        # to "run pytest with FRISKET_CACHE_REFRESH") with polished
        # user-facing replay copy — see
        # frisket.llm.remediation.classify_llm_error and its dedicated pin
        # tests/test_cachemiss_user_copy.py. This assertion is retargeted to
        # the new intended copy rather than the retired dev-message wording.
        assert row["error"]
        lowered_error = row["error"].lower()
        assert "replay mode" in lowered_error or "no cached result" in lowered_error

    def test_partial_run_one_cached_one_miss_only_the_miss_fails(
        self, project, tmp_path
    ):
        """A replay_strict miss fails only its own row; cached rows in the
        same run still succeed (partial-failure tolerance)."""
        texts = ["cached row", "uncached row"]
        sheet, _ = _seed_sheet(project, texts)
        spec = _classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        recipe = model_plan(spec).program
        call = recipe.render({"text": "cached row"}, spec)
        from frisket.ai.llm import LLMRequest, LLMResponse, request_key

        req = LLMRequest(
            model=MODEL,
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        cache.put(
            request_key(req, recipe.version),
            LLMResponse(
                content=None,
                data={"relevance": 5},
                tokens_in=10,
                tokens_out=5,
                cost=0.0001,
                model=MODEL,
            ),
        )
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        progress = asyncio.run(run_with_output_claim(runner, spec))

        assert progress.done is True
        assert progress.completed == 2
        assert progress.failed == 1
