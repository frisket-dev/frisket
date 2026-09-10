from frisket.engine.executor.action_support import _routed_call_provider_use


def call(kind, *, units=None):
    return {
        "capability": "transcribe",
        "provider": "fixture",
        "engine": "whisper",
        "provider_kind": kind,
        "units": units or {},
        "provider_cost_usd": 0.0,
    }


def test_audio_duration_units_do_not_erase_actual_call_counts_or_claim_egress():
    result = _routed_call_provider_use(
        [call("local_process", units={"audio_seconds": 5}), call("local_process")],
        capability="transcribe",
    )
    assert len(result) == 1
    assert result[0]["external_api"] is False
    assert result[0]["request_count"] == 0
    assert result[0]["model_call_count"] == 2


def test_request_units_and_actual_provider_locality_are_preserved():
    result = _routed_call_provider_use(
        [
            call("platform_api", units={"requests": 3}),
            call("local_http", units={"requests": 1}),
        ],
        capability="transcribe",
    )
    assert [item["external_api"] for item in result] == [True, False]
    assert [item["request_count"] for item in result] == [3, 1]
    assert [item["model_call_count"] for item in result] == [1, 1]


def test_no_calls_does_not_invent_external_provider_usage():
    assert _routed_call_provider_use([], capability="transcribe") == []


def test_credential_sources_remain_distinct_durable_provider_groups():
    facts = [
        {**call("platform_api"), "credential_source": source}
        for source in ("project_key", "org_byok", "project_key", None)
    ]
    groups = _routed_call_provider_use(facts, capability="transcribe")
    assert [group.get("credential_source") for group in groups] == [
        "project_key",
        "org_byok",
        None,
    ]
    assert [group["model_call_count"] for group in groups] == [2, 1, 1]
    assert "credential_source" not in groups[-1]
