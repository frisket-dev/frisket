"""Unit tests for the frozen WriterFence value object."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from frisket.engine.store.writer_fence import WriterFence


def test_writer_fence_rejects_claimless_with_claim_token() -> None:
    with pytest.raises(
        ValueError,
        match="claimless_direct_effect cannot also carry a claim_token",
    ):
        WriterFence(
            run_id=7,
            writer_attempt_id="attempt-a",
            claim_token="claim:tok",
            claimless_direct_effect=True,
        )


def test_writer_fence_defaults_and_immutability() -> None:
    fence = WriterFence()
    assert fence.run_id is None
    assert fence.writer_attempt_id is None
    assert fence.claim_token is None
    assert fence.claimless_direct_effect is False
    assert fence.authorized_attempt_id is None
    assert fence.fact_owner_attempt_id is None

    with pytest.raises(FrozenInstanceError):
        fence.claim_token = "claim:mutated"  # type: ignore[misc]


def test_writer_fence_claimless_constructor() -> None:
    fence = WriterFence.claimless(run_id=42, attempt_id="attempt-direct")
    assert fence == WriterFence(
        run_id=42,
        writer_attempt_id="attempt-direct",
        claim_token=None,
        claimless_direct_effect=True,
        authorized_attempt_id="attempt-direct",
    )
    assert fence.fact_owner_attempt_id == "attempt-direct"


def test_writer_fence_claimed_constructor_and_distinct_fact_owner() -> None:
    writer_fence = WriterFence.claimed(
        run_id=9,
        writer_attempt_id="attempt-b",
        claim_token="claim:live",
    )
    assert writer_fence.claimless_direct_effect is False
    assert writer_fence.authorized_attempt_id is None
    assert writer_fence.fact_owner_attempt_id == "attempt-b"

    replayed = writer_fence.for_fact_owner("attempt-a")
    assert replayed.writer_attempt_id == "attempt-b"
    assert replayed.claim_token == "claim:live"
    assert replayed.authorized_attempt_id == "attempt-a"
    assert replayed.fact_owner_attempt_id == "attempt-a"
    # Original fence remains untouched.
    assert writer_fence.authorized_attempt_id is None
    assert writer_fence.fact_owner_attempt_id == "attempt-b"

    explicit = WriterFence.claimed(
        run_id=9,
        writer_attempt_id="attempt-b",
        claim_token="claim:live",
        authorized_attempt_id="attempt-a",
    )
    assert explicit == replayed


def test_writer_fence_with_returned_owner_preserves_none_explicitly() -> None:
    fence = WriterFence.claimed(
        run_id=12,
        writer_attempt_id="attempt-b",
        claim_token="claim:live",
        authorized_attempt_id="attempt-a",
    )
    returned = fence.with_returned_owner("attempt-a")
    assert returned.writer_attempt_id == "attempt-a"
    assert returned.authorized_attempt_id == "attempt-a"
    assert returned.claim_token == "claim:live"
    assert returned.run_id == 12

    cleared = fence.with_returned_owner(None)
    assert cleared.writer_attempt_id is None
    assert cleared.authorized_attempt_id is None
    assert cleared.fact_owner_attempt_id is None
