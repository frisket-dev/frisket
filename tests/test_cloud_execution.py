from __future__ import annotations

import io

import pytest

from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudMediaUpload,
    CloudPayerSubject,
)


def _payer() -> CloudPayerSubject:
    return CloudPayerSubject(
        user_id=7,
        account_id=41,
        lane="frisket_credits",
        provider="anthropic",
        engine="anthropic/claude-haiku-4-5",
        route_revision="route-1",
    )


def test_cloud_attempt_carries_durable_id_and_exact_payer_without_a_secret():
    attempt = CloudExecutionAttempt(
        request_id="cloud-request-1",
        payer=_payer(),
        may_confirm_quote=True,
    )

    assert attempt.request_id == "cloud-request-1"
    assert attempt.payer.account_id == 41
    assert "token" not in repr(attempt).lower()


@pytest.mark.parametrize("name", ["../secret.pdf", "folder/audio.wav", "", ".."])
def test_cloud_upload_refuses_paths(name):
    with pytest.raises(ValueError, match="plain file name"):
        CloudMediaUpload(name, "application/octet-stream", io.BytesIO(b"data"))


def test_cloud_upload_retains_only_a_safe_name_type_and_stream():
    upload = CloudMediaUpload(
        "source.pdf", "application/pdf", io.BytesIO(b"%PDF-test")
    )

    assert upload.file_name == "source.pdf"
    assert upload.stream.read() == b"%PDF-test"
