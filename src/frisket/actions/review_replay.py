"""Typed review decisions and regenerated-value replay mutations."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

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
)


class ReviewDecisionParams(ActionParams):
    run_id: int = Field(ge=1, strict=True)
    row_id: int = Field(ge=1, strict=True)
    column_id: int = Field(ge=1, strict=True)
    decision: Literal["accept", "reject", "reject_clear", "edit"]
    value: Any | None = None
    note: str | None = None

    @field_validator("value")
    @classmethod
    def _json_value(cls, value: Any | None) -> Any | None:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be JSON-serializable") from exc
        return value

    @model_validator(mode="after")
    def _value_belongs_only_to_edit(self) -> ReviewDecisionParams:
        if self.decision != "edit" and "value" in self.model_fields_set:
            raise ValueError("value is only valid for an edit decision")
        return self


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
        row_id=params.row_id,
        column_id=params.column_id,
        decision=params.decision,
        value=params.value,
        value_supplied="value" in params.model_fields_set,
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
            run_id=1, row_id=1, column_id=1, decision="edit", value=None
        ),
    ),
    name="decision",
    title="Review decision",
    description=(
        "Record an accept, reject, or edit decision for the current generated "
        "result cell, with an optional note and pinned receipt evidence."
    ),
    category=ActionCategory.CLEANUP,
    run=decide_review,
    form="review_decision",
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
    title="Accept regenerated value",
    description=(
        "Accept one exact current regenerated value as an attributed, undoable "
        "edit after checking its run and value identity."
    ),
    category=ActionCategory.CLEANUP,
    run=accept_replay,
    form="replay_accept",
)

REPLAY_ACCEPT_COLUMN = action(
    examples=(ReplayAcceptColumnParams(sheet_id=1, column_id=1),),
    name="accept_column",
    title="Accept regenerated column values",
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
        "Keep a human edit against one exact regenerated run and value identity "
        "so an identical value remains dismissed."
    ),
    category=ActionCategory.CLEANUP,
    run=dismiss_replay,
    form="replay_dismiss",
)
