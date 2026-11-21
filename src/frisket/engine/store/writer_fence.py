"""Frozen writer-fence value object for fenced store and checkpoint mutations."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class WriterFence:
    """Explicit writer and fact-ownership authority for one fenced mutation.

    ``writer_attempt_id`` is the live attempt performing the write (holding
    ``claim_token`` or claimless direct-effect authority).
    ``authorized_attempt_id`` is the immutable attempt that originally
    authorized the provider effect: on a resumed or replayed run, attempt B
    writes under its own ``writer_attempt_id``/``claim_token`` while returned
    facts and spend remain owned by attempt A (``authorized_attempt_id``).
    """

    run_id: int | None = None
    writer_attempt_id: str | None = None
    claim_token: str | None = None
    claimless_direct_effect: bool = False
    authorized_attempt_id: str | None = None

    def __post_init__(self) -> None:
        if self.claimless_direct_effect and self.claim_token is not None:
            raise ValueError("claimless_direct_effect cannot also carry a claim_token")

    @property
    def fact_owner_attempt_id(self) -> str | None:
        """The immutable attempt owning facts and spend for this write."""
        return self.authorized_attempt_id or self.writer_attempt_id

    def for_fact_owner(self, authorized_attempt_id: str | None) -> WriterFence:
        """Return a copy attributing fact ownership to ``authorized_attempt_id``."""
        return replace(self, authorized_attempt_id=authorized_attempt_id)

    def with_returned_owner(self, owner_attempt_id: str | None) -> WriterFence:
        """Return a copy where both writer and fact owner name ``owner_attempt_id``.

        Publication-independent returned-fact writers require the actual
        authorized attempt as both writer and immutable fact owner.
        """
        return replace(
            self,
            writer_attempt_id=owner_attempt_id,
            authorized_attempt_id=owner_attempt_id,
        )

    @classmethod
    def claimless(cls, *, run_id: int | None, attempt_id: str) -> WriterFence:
        """Construct a claimless direct-effect fence owned by ``attempt_id``."""
        return cls(
            run_id=run_id,
            writer_attempt_id=attempt_id,
            claim_token=None,
            claimless_direct_effect=True,
            authorized_attempt_id=attempt_id,
        )

    @classmethod
    def claimed(
        cls,
        *,
        run_id: int | None,
        writer_attempt_id: str,
        claim_token: str,
        authorized_attempt_id: str | None = None,
    ) -> WriterFence:
        """Construct an output-claim-backed fence owned by ``writer_attempt_id``."""
        return cls(
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=False,
            authorized_attempt_id=authorized_attempt_id,
        )


__all__ = ["WriterFence"]
