import json

import pytest
from fastapi.testclient import TestClient

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner.validation import (
    confirmation_estimate,
    consented_confirmation_estimate,
)
from frisket.execution.pricing_policy import (
    PersistedRating,
    RatedQuote,
    Unpriceable,
    _reset_pricing_policy_for_tests,
    install_pricing_policy,
)
from frisket.ops.capture import url as capture_url
from frisket.ops.capture.url import BrowserUrlRenderResult
from frisket.server.app import create_app
from tests.engine.test_capture_screenshot_action import SCREENSHOT_BYTES
from http_test_helpers import drain_queue


class BrowserTariff:
    policy_id = "test.browser_tariff.v1"

    def __init__(self, billed):
        self.billed = billed

    def rate(self, facts):
        if self.billed is None:
            return Unpriceable(
                reason="browser tariff unavailable", policy_id=self.policy_id
            )
        return RatedQuote(
            billed_cost=self.billed,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.mark.parametrize("billed", [0, 1, None])
def test_screenshot_uses_rated_consent_and_persisted_rating(
    tmp_path, monkeypatch, billed
):
    _reset_pricing_policy_for_tests()
    policy = BrowserTariff(billed)
    install_pricing_policy(policy)
    calls = []

    def browser(url, **kwargs):
        calls.append(url)
        return BrowserUrlRenderResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            html="<title>Captured</title>",
            screenshot=SCREENSHOT_BYTES,
        )

    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    monkeypatch.setattr(capture_url, "render_playwright_url", browser)
    try:
        with TestClient(create_app(tmp_path / "workspace")) as client:
            pid = client.post("/api/projects", json={"name": "Browser tariff"}).json()[
                "id"
            ]
            project = client.app.state.workspace.get(pid)
            sheet = project.add_sheet("URLs")
            col = project.add_column(sheet, "url", type="link")
            project.add_rows(
                sheet, [{"url": "https://example.test/story"}], {"url": col}
            )
            body = {
                "action_id": "web.capture_screenshot",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "url"},
                "idempotency_key": "rated-browser",
            }
            plan = build_typed_map_rows_plan(project, typed_action_for_request(body))
            estimate = {"cost": 0.0, "cost_source": "free_local", "rows": 1}
            rated = confirmation_estimate(
                plan.program, plan.spec_dict(), estimate, policy=policy
            )
            restored = consented_confirmation_estimate(
                plan.program,
                plan.spec_dict(),
                estimate,
                rating=PersistedRating(billed_cost=billed, policy_id=policy.policy_id),
            )
            assert rated == restored
            assert rated["requires_confirmation"] is (billed != 0)
            assert rated["remote_capability"] == "external:browser_render"

            endpoint = f"/api/projects/{pid}/actions/v1/run"
            response = client.post(endpoint, json=body)
            assert calls == []
            if billed is None:
                assert response.status_code == 402, response.text
                details = response.json()["errors"][0]["details"]
                assert details["estimate"]["cost"] == 0
                assert details["estimate"]["billed_cost"] == billed
                wrong = client.post(endpoint, json={**body, "confirmation": "wrong"})
                assert wrong.status_code == 402, wrong.text
                assert calls == []
                body["confirmation"] = details["promise_set_hash"]
                response = client.post(endpoint, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "queued"
            assert calls == []
            if billed != 0:
                # A changed tariff cannot silently re-price the admitted job.
                policy.billed = 2_000_000
            drain_queue(client)
            assert calls == ["https://example.test/story"]
            stored = project.db.execute(
                "SELECT quote_json FROM consents WHERE subject_kind='run'"
            ).fetchall()
            if billed == 0:
                assert stored == []
            else:
                assert len(stored) == 1
                assert json.loads(stored[0]["quote_json"])["billed_cost"] == billed
    finally:
        _reset_pricing_policy_for_tests()
