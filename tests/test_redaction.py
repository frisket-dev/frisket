from __future__ import annotations

import json
import time
import traceback
from collections.abc import Mapping

import pytest

from frisket.redaction import (
    CYCLE,
    REDACTED,
    TRUNCATED,
    SafeFrame,
    canonical_error_code,
    redact_stored_error,
    redact_text,
    redact_value,
    safe_error,
    safe_stack_frames,
)

SENTINEL = "sk-checkpoint1a-secret"


def test_canonical_error_code_accepts_only_bounded_lower_snake_case() -> None:
    assert canonical_error_code("provider_timeout") == "provider_timeout"
    assert canonical_error_code("Provider Timeout") == "internal_error"
    assert canonical_error_code("a" * 65, fallback="safe_fallback") == "safe_fallback"


def test_sensitive_keys_redact_without_hiding_observability_ids() -> None:
    value = {
        "api_key": SENTINEL,
        "AUTHORIZATION": f"Bearer {SENTINEL}",
        "auth_session_id": SENTINEL,
        "session_token": SENTINEL,
        "session_cookie": SENTINEL,
        "stripe_key": SENTINEL,
        "resend_key": SENTINEL,
        "session": "interactive-session",
        "session_id": "sess-123",
        "request_id": "req-123",
        "trace_id": "trace-123",
        "correlation_id": "corr-123",
        "provider_request_id": "provider-123",
        "token_count": 42,
        "password_policy": "strict",
    }

    redacted = redact_value(value)

    for key in (
        "api_key",
        "AUTHORIZATION",
        "auth_session_id",
        "session_token",
        "session_cookie",
        "stripe_key",
        "resend_key",
    ):
        assert redacted[key] == REDACTED
    for key in (
        "session",
        "session_id",
        "request_id",
        "trace_id",
        "correlation_id",
        "provider_request_id",
        "token_count",
        "password_policy",
    ):
        assert redacted[key] == value[key]


@pytest.mark.parametrize(
    "raw",
    [
        f"api_key={SENTINEL}",
        f'{{"api_key":"{SENTINEL}"}}',
        f"Authorization: Bearer {SENTINEL}",
        f"Cookie: session_token={SENTINEL}",
        f'api_key="prefix {SENTINEL} suffix"',
        "credential=frisket_pat_1234567890abcdef",
        "credential=ghp_1234567890abcdef",
        "credential=xoxb-1234567890-secret",
        "credential=AKIA1234567890ABCDEF",
        "credential=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "postgresql://user:password@example.test/db?request_id=req-1",
        "postgres://user:password@db.internal:5432/db",
        "mongodb+srv://user:password@cluster0.example.mongodb.net/db",
        "https://example.test/callback?api_key=opaquevalue&request_id=req-1",
        "https://hooks.slack.com/services/T00000000/B00000000/secretvalue",
        (
            'private_key="-----BEGIN PRIVATE KEY-----\n'
            "cHJpdmF0ZS1rZXktbWF0ZXJpYWw=\n"
            '-----END PRIVATE KEY-----"'
        ),
    ],
)
def test_text_credential_grammar_removes_secret_material(raw: str) -> None:
    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert safe != raw
    assert "secret" not in safe.lower() or REDACTED.lower() in safe.lower()
    assert redact_text(safe, max_chars=4_096, one_line=False) == safe


@pytest.mark.parametrize(
    "safe",
    [
        "token expired",
        "token_count=12",
        "password policy invalid",
        "sketch-v1",
        "550e8400-e29b-41d4-a716-446655440000",
        "https://example.test/items?limit=25&request_id=req-1",
        "session_id=sess-123",
        "hosted.tenant_dispatch.critical",
        "The bearer token expired",
        "The basic authentication policy failed",
    ],
)
def test_false_positive_matrix_remains_visible(safe: str) -> None:
    assert redact_text(safe, max_chars=4_096, one_line=False) == safe


def test_caller_known_values_are_bounded_and_literal() -> None:
    raw = "long=opaque-secret-value short=(abcd) embedded=xabcdx ignored=abc"
    safe = redact_text(
        raw,
        secret_values=("opaque-secret-value", "abcd", "abc"),
        max_chars=4_096,
        one_line=False,
    )
    assert "opaque-secret-value" not in safe
    assert "short=([REDACTED])" in safe
    assert "embedded=xabcdx" in safe
    assert "ignored=abc" in safe


def test_traceback_strings_are_replaced_as_a_unit() -> None:
    raw = (
        "before\n"
        "Traceback (most recent call last):\n"
        '  File "/private/work/app.py", line 7, in run\n'
        f"    raise RuntimeError('{SENTINEL}')\n"
        f"RuntimeError: {SENTINEL}\n"
        "\n"
        "after"
    )
    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert safe == "before\n[TRACEBACK REDACTED]\n\nafter"
    assert SENTINEL not in safe
    assert "/private/work" not in safe


def test_real_multiline_traceback_consumes_opaque_exception_continuation() -> None:
    sentinel = "opaque_second_line_value_Z9"
    try:
        raise RuntimeError(f"first line\n{sentinel}")
    except RuntimeError:
        raw = traceback.format_exc()

    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert safe == "[TRACEBACK REDACTED]\n"
    assert sentinel not in safe


def test_real_exception_group_traceback_is_replaced_as_one_block() -> None:
    sentinels = (
        "opaque_group_value_Z9",
        "opaque_child_one_Z9",
        "opaque_child_two_Z9",
    )
    try:
        raise ExceptionGroup(
            sentinels[0],
            [ValueError(sentinels[1]), RuntimeError(sentinels[2])],
        )
    except ExceptionGroup:
        raw = traceback.format_exc()

    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert safe == "[TRACEBACK REDACTED]\n"
    assert all(sentinel not in safe for sentinel in sentinels)


def test_traceback_header_without_a_frame_consumes_opaque_terminal_to_end() -> None:
    raw = (
        "before\n"
        "Traceback (most recent call last):\n"
        "RuntimeError: opaque-terminal\n"
        "after"
    )

    safe = redact_text(raw, max_chars=4_096, one_line=False)

    assert safe == "before\n[TRACEBACK REDACTED]"
    assert "opaque-terminal" not in safe


def test_standalone_traceback_frame_consumes_terminal_exception_rendering() -> None:
    sentinel = "sk-standalone-frame-secret-12345"
    raw = (
        f'  File "/private/x.py", line 7, in run\n'
        f'    source = "{sentinel}"\n'
        f"RuntimeError: {sentinel}"
    )

    safe = redact_text(raw, max_chars=4_096, one_line=False)

    assert safe == "[TRACEBACK REDACTED]"
    assert sentinel not in safe
    assert "RuntimeError" not in safe


def test_standalone_chain_separator_consumes_terminal_exception_rendering() -> None:
    sentinel = "sk-chain-separator-secret-12345"
    raw = (
        "The above exception was the direct cause of the following exception:\n"
        f"RuntimeError: {sentinel}"
    )

    safe = redact_text(raw, max_chars=4_096, one_line=False)

    assert safe == "[TRACEBACK REDACTED]"
    assert sentinel not in safe
    assert "RuntimeError" not in safe


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://user:opaque-password@[bad/db",
        "postgres://user:opaque-password@[bad/db",
        "mongodb+srv://user:opaque-password@[bad/db",
        "https://user:opaque-password@[bad/path",
        "https://user:opaque-password@[bad/path?token=opaque-query-secret",
    ],
)
def test_malformed_recognized_urls_fail_closed(raw: str) -> None:
    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert "opaque-password" not in safe
    assert "opaque-query-secret" not in safe
    assert f"{REDACTED}@" in safe


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://frisket:opaque-db-password@db.internal:5432/frisket",
        "postgresql://frisket:opaque-db-password@db.internal/frisket",
        "mongodb+srv://frisket:opaque-db-password@cluster0.example.mongodb.net/db",
    ],
)
def test_database_url_userinfo_passwords_redact_in_text_and_stacks(dsn: str) -> None:
    stack = (
        "OperationalError: connection failed\n"
        f"  connecting to {dsn}?connect_timeout=5&password=opaque-query-secret\n"
        "  at connect (db.py:41)"
    )

    safe = redact_text(stack, max_chars=8_192, one_line=False)

    assert "opaque-db-password" not in safe
    assert "opaque-query-secret" not in safe
    assert f"{REDACTED}@" in safe
    assert redact_text(safe, max_chars=8_192, one_line=False) == safe


def test_database_urls_redact_inside_nested_client_error_context() -> None:
    context = {
        "connection": "postgres://frisket:opaque-db-password@db.internal/frisket",
        "layers": [
            {"dsn": "mongodb+srv://u:opaque-db-password@cluster.example.net/db"},
        ],
        "trace_id": "trace-123",
    }

    redacted = redact_value(context)
    flattened = json.dumps(redacted)

    assert "opaque-db-password" not in flattened
    assert redacted["trace_id"] == "trace-123"
    assert f"{REDACTED}@" in redacted["connection"]


@pytest.mark.parametrize(
    "raw",
    [
        'api_key="opaque secret tail',
        '{"api_key":"opaque secret tail}',
        "prefix\napi_key='opaque secret tail\nafter",
    ],
)
def test_unclosed_sensitive_assignments_fail_closed_to_line_end(raw: str) -> None:
    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert "opaque secret tail" not in safe
    assert REDACTED in safe


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            r'{"api_key":"safe-prefix\"opaque-after"}',
            '{"api_key":"[REDACTED]"}',
        ),
        (
            r'api_key="safe-prefix\"opaque-after"',
            'api_key="[REDACTED]"',
        ),
        (
            r"api_key='safe-prefix\'opaque-after'",
            "api_key='[REDACTED]'",
        ),
        (
            r'api_key="safe-prefix\"opaque-after',
            'api_key="[REDACTED]"',
        ),
    ],
)
def test_sensitive_quoted_assignments_treat_escapes_as_value_atoms(
    raw: str,
    expected: str,
) -> None:
    safe = redact_text(raw, max_chars=4_096, one_line=False)
    assert safe == expected
    assert "safe-prefix" not in safe
    assert "opaque-after" not in safe


@pytest.mark.parametrize(
    "raw",
    [
        (
            "-----BEGIN PRIVATE KEY-----\n"
            "opaque-private-material\n"
            "-----END PRIVATE KEY-----"
        ),
        "-----BEGIN RSA PRIVATE KEY-----\nopaque-truncated-material",
    ],
)
def test_complete_and_truncated_private_key_blocks_fail_closed(raw: str) -> None:
    assert redact_text(raw, max_chars=4_096, one_line=False) == REDACTED


@pytest.mark.parametrize("raw", ["Bearer x", "Bearer abc", "Basic x", "Basic abc"])
def test_short_standalone_auth_schemes_remain_masked(raw: str) -> None:
    assert redact_text(raw, one_line=False) == f"{raw.split()[0]} {REDACTED}"


def test_opaque_non_json_three_part_jwt_is_redacted_without_dotted_prose_false_positive() -> (
    None
):
    token = "YWJjZGVm.Z2hpamts.bW5vcHFy"
    assert redact_text(token, one_line=False) == REDACTED
    assert redact_text("release.version.build", one_line=False) == (
        "release.version.build"
    )
    assert redact_text(
        "hosted.tenant_dispatch.critical.alert_event", one_line=False
    ) == ("hosted.tenant_dispatch.critical.alert_event")


def test_safe_error_has_bounded_detail_and_structured_recent_frames() -> None:
    def recurse(depth: int) -> None:
        local_secret = SENTINEL
        if depth:
            recurse(depth - 1)
        raise RuntimeError(f"api_key={local_secret}\nprovider exploded")

    try:
        recurse(20)
    except RuntimeError as exc:
        safe = safe_error("worker_exception", exc, include_frames=True, max_chars=120)

    assert safe.code == "worker_exception"
    assert safe.exception_type == "RuntimeError"
    assert len(safe.text) <= 120
    assert "\n" not in safe.text
    assert SENTINEL not in safe.text
    assert 1 <= len(safe.frames) <= 12
    assert all(isinstance(frame, SafeFrame) for frame in safe.frames)
    assert all(frame.line > 0 for frame in safe.frames)
    assert all(not frame.path.startswith("/") for frame in safe.frames)
    serialized = str(safe.frames)
    assert SENTINEL not in serialized
    assert "raise RuntimeError" not in serialized


def test_safe_stack_frames_handles_none_and_zero_limit() -> None:
    assert safe_stack_frames(None) == ()
    assert safe_stack_frames(None, max_frames=0) == ()


def test_safe_error_honors_total_limit_and_invalid_limits_do_not_raise() -> None:
    tiny = safe_error(
        "worker_exception",
        "a long provider failure that must be bounded",
        max_chars=8,
    )
    invalid = safe_error(
        "worker_exception",
        "provider failure",
        max_chars="invalid",  # type: ignore[arg-type]
    )
    assert len(tiny.text) <= 8
    for limit in (0, 1, 2):
        assert len(safe_error("worker_exception", "failure", max_chars=limit).text) <= (
            limit
        )
    assert len(safe_error("worker_exception", "failure", max_chars=3).text) == 3
    assert len(invalid.text) <= 1_000
    assert redact_text("visible", max_chars="invalid") == "visible"  # type: ignore[arg-type]
    assert redact_text("visible", max_chars=-1) == ""


def test_redact_stored_error_preserves_valid_code_and_is_idempotent() -> None:
    raw = f"provider_timeout: api_key={SENTINEL}\ntry again"
    safe = redact_stored_error(raw, fallback_code="legacy_job_error", max_chars=90)
    assert safe == "provider_timeout: api_key=[REDACTED] try again"
    assert redact_stored_error(safe, fallback_code="legacy_job_error") == safe
    assert redact_stored_error(None, fallback_code="legacy_job_error") is None
    assert redact_stored_error("plain failure", fallback_code="legacy_job_error") == (
        "legacy_job_error: plain failure"
    )


@pytest.mark.parametrize(
    "credential_code",
    ("frisket_pat_abcdefghijk", "github_pat_abcdefghijk"),
)
def test_redact_stored_error_rejects_credential_shaped_code(
    credential_code: str,
) -> None:
    raw = f"{credential_code}: failed"
    safe = redact_stored_error(raw, fallback_code="legacy_job_error")

    assert safe == "legacy_job_error: [REDACTED]: failed"
    assert credential_code not in safe
    assert redact_stored_error(safe, fallback_code="legacy_job_error") == safe


def test_recursive_redaction_is_cycle_safe_bounded_and_deterministic() -> None:
    cyclic: dict[str, object] = {"api_key": SENTINEL}
    cyclic["self"] = cyclic
    deep: object = "leaf"
    for _ in range(15):
        deep = [deep]
    value = {
        "cyclic": cyclic,
        "deep": deep,
        "bytes": b"secret bytes",
        "set": {"b", "a"},
    }

    first = redact_value(value)
    second = redact_value(value)

    assert first == second
    assert first["cyclic"] == {"api_key": REDACTED, "self": CYCLE}
    assert TRUNCATED in str(first["deep"])
    assert first["bytes"] == "[BYTES 12]"
    assert first["set"] == ["a", "b"]
    assert SENTINEL not in json.dumps(first)


def test_unprintable_unknown_object_never_falls_back_to_repr() -> None:
    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("do not stringify")

        def __repr__(self) -> str:
            return SENTINEL

    assert redact_value(Hostile()) == "[UNPRINTABLE Hostile]"


def test_infinite_mapping_iteration_stops_at_the_global_item_budget() -> None:
    class InfiniteMapping(Mapping[str, str]):
        def __init__(self) -> None:
            self.reads = 0

        def __getitem__(self, key: str) -> str:
            return key

        def __iter__(self):
            index = 0
            while True:
                self.reads += 1
                yield f"item_{index}"
                index += 1

        def __len__(self) -> int:
            return 2**31

    value = InfiniteMapping()
    safe = redact_value(value)

    assert isinstance(safe, dict)
    assert safe[TRUNCATED] == TRUNCATED
    assert len(safe) <= 257
    assert value.reads <= 257


def test_hostile_exception_string_and_one_shot_secrets_are_failure_safe() -> None:
    class HostileError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("do not stringify")

    class OneShotSecrets:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("secret iterable was consumed twice")
            yield "opaque-once-secret"

    secrets = OneShotSecrets()
    assert redact_value(
        {"message": "failure opaque-once-secret"},
        secret_values=secrets,
    ) == {"message": "failure [REDACTED]"}
    assert secrets.iterations == 1
    assert safe_error("worker_exception", HostileError()).detail == "operation failed"


def test_infinite_rejected_secret_iterable_is_read_a_bounded_number_of_times() -> None:
    class InfiniteRejectedSecrets:
        def __init__(self) -> None:
            self.reads = 0

        def __iter__(self):
            while True:
                self.reads += 1
                yield None

    secrets = InfiniteRejectedSecrets()
    assert redact_text("visible", secret_values=secrets) == "visible"
    assert secrets.reads == 64


def test_adversarial_input_is_bounded_before_pattern_matching() -> None:
    raw = ("authorization=" * 8_000) + SENTINEL
    started = time.perf_counter()
    safe = redact_text(raw, max_chars=1_000)
    elapsed = time.perf_counter() - started

    assert len(safe) <= 1_000
    assert elapsed < 1.0
