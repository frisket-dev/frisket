"""Typed review decisions and regenerated-value replay mutations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    AcceptedReplayColumn,
    AcceptedReplayValue,
    ActionParams,
    DismissedReplayValue,
    ReplayColumnAcceptor,
    ReplayValueAcceptor,
    ReplayValueDismissor,
    ReviewDecider,
    ReviewDecision,
    ReviewNote,
    ReviewNoter,
)


class ReviewTargetParams(ActionParams):
    run_id: int | None = Field(default=None, ge=1, strict=True)
    copied_op_id: int | None = Field(default=None, ge=1, strict=True)
    row_id: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _one_result_owner(self):
        if (self.run_id is None) == (self.copied_op_id is None):
            raise ValueError("select exactly one run_id or copied_op_id")
        return self


class ReviewDecisionParams(ReviewTargetParams):
    column_id: int = Field(ge=1, strict=True)
    decision: Literal["accept", "reject", "reject_clear", "clear"]
    note: str | None = None


class ReviewNoteParams(ReviewTargetParams):
    note: str | None = None


class ReplayCellParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    row_id: int = Field(ge=1, strict=True)
    column_id: int = Field(ge=1, strict=True)
    run_id: int = Field(ge=1, strict=True)
    generated_value_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReplayAcceptParams(ReplayCellParams):
    pass


class ReplayAcceptColumnParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    column_id: int = Field(ge=1, strict=True)


class ReplayDismissParams(ReplayCellParams):
    pass


def decide_review(
    params: ReviewDecisionParams, reviews: ReviewDecider
) -> ReviewDecision:
    return reviews.decide(
        run_id=params.run_id,
        copied_op_id=params.copied_op_id,
        row_id=params.row_id,
        column_id=params.column_id,
        decision=params.decision,
        note=params.note,
        note_supplied="note" in params.model_fields_set,
    )


def note_review(params: ReviewNoteParams, reviews: ReviewNoter) -> ReviewNote:
    return reviews.note(
        run_id=params.run_id,
        copied_op_id=params.copied_op_id,
        row_id=params.row_id,
        note=params.note,
    )


def accept_replay(
    params: ReplayAcceptParams, replay: ReplayValueAcceptor
) -> AcceptedReplayValue:
    return replay.accept(
        sheet_id=params.sheet_id,
        row_id=params.row_id,
        column_id=params.column_id,
        run_id=params.run_id,
        generated_value_hash=params.generated_value_hash,
    )


def accept_replay_column(
    params: ReplayAcceptColumnParams, replay: ReplayColumnAcceptor
) -> AcceptedReplayColumn:
    return replay.accept_column(
        sheet_id=params.sheet_id,
        column_id=params.column_id,
    )


def dismiss_replay(
    params: ReplayDismissParams, replay: ReplayValueDismissor
) -> DismissedReplayValue:
    return replay.dismiss(
        sheet_id=params.sheet_id,
        row_id=params.row_id,
        column_id=params.column_id,
        run_id=params.run_id,
        generated_value_hash=params.generated_value_hash,
    )


REVIEW_DECISION = action(
    examples=(
        ReviewDecisionParams(run_id=1, row_id=1, column_id=1, decision="accept"),
        ReviewDecisionParams(
            copied_op_id=1, row_id=1, column_id=1, decision="reject_clear"
        ),
    ),
    name="decision",
    title="Review decision",
    description=(
        "Record or clear an accept or reject decision for the current "
        "generated result cell, with an optional note and a pinned receipt. "
        "Clearing keeps the current visible cell value and its review note."
    ),
    category=ActionCategory.CLEANUP,
    run=decide_review,
    form="review_decision",
)


REVIEW_NOTE = action(
    examples=(
        ReviewNoteParams(run_id=1, row_id=1, note="Checked against the filing."),
        ReviewNoteParams(run_id=1, row_id=1, note=None),
    ),
    name="note",
    title="Review row note",
    description=(
        "Record one optional review note across the current generated result fields "
        "for a run row, without changing their decisions or visible values."
    ),
    category=ActionCategory.CLEANUP,
    run=note_review,
    form="review_note",
)

REPLAY_ACCEPT = action(
    examples=(
        ReplayAcceptParams(
            sheet_id=1,
            row_id=1,
            column_id=1,
            run_id=1,
            generated_value_hash="sha256:" + "0" * 64,
        ),
    ),
    name="accept",
    title="Accept newer value",
    description=(
        "Accept one exact current newer value as an attributed, undoable "
        "edit after checking its run and value identity."
    ),
    category=ActionCategory.CLEANUP,
    run=accept_replay,
    form="replay_accept",
)

REPLAY_ACCEPT_COLUMN = action(
    examples=(ReplayAcceptColumnParams(sheet_id=1, column_id=1),),
    name="accept_column",
    title="Accept newer column values",
    description=(
        "Accept every pending value in a single-origin generated column as one "
        "undoable edit operation."
    ),
    category=ActionCategory.CLEANUP,
    run=accept_replay_column,
    form="replay_accept_column",
)

REPLAY_DISMISS = action(
    examples=(
        ReplayDismissParams(
            sheet_id=1,
            row_id=1,
            column_id=1,
            run_id=1,
            generated_value_hash="sha256:" + "0" * 64,
        ),
    ),
    name="dismiss",
    title="Keep edit",
    description=(
        "Keep a human edit against one exact newer run and value identity "
        "so an identical value remains dismissed."
    ),
    category=ActionCategory.CLEANUP,
    run=dismiss_replay,
    form="replay_dismiss",
)
