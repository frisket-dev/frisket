"""Typed project change-history actions."""

from __future__ import annotations

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    ChangeRedoer,
    ChangeTransition,
    ChangeUndoer,
)


class ChangeStepParams(ActionParams):
    expected_change_id: int | None = Field(default=None, gt=0, strict=True)


def undo_change(params: ChangeStepParams, changes: ChangeUndoer) -> ChangeTransition:
    return changes.undo(expected_change_id=params.expected_change_id)


def redo_change(params: ChangeStepParams, changes: ChangeRedoer) -> ChangeTransition:
    return changes.redo(expected_change_id=params.expected_change_id)


UNDO = action(
    examples=(ChangeStepParams(expected_change_id=1),),
    name="undo",
    title="Undo change",
    description="Undo the most recent applied project change.",
    category=ActionCategory.CLEANUP,
    run=undo_change,
    form="change_step",
)

REDO = action(
    examples=(ChangeStepParams(expected_change_id=1),),
    name="redo",
    title="Redo change",
    description="Redo the next undone project change.",
    category=ActionCategory.CLEANUP,
    run=redo_change,
    form="change_step",
)


class ChangeTargetParams(ActionParams):
    change_id: int = Field(gt=0, strict=True)


def revert_change(
    params: ChangeTargetParams, changes: ChangeUndoer
) -> ChangeTransition:
    return changes.revert(change_id=params.change_id)


def restore_change(
    params: ChangeTargetParams, changes: ChangeRedoer
) -> ChangeTransition:
    return changes.restore(change_id=params.change_id)


REVERT = action(
    examples=(ChangeTargetParams(change_id=1),),
    name="revert",
    title="Revert change",
    description="Revert one selected change without moving Undo/Redo navigation.",
    category=ActionCategory.CLEANUP,
    run=revert_change,
    form="change_step",
)

RESTORE = action(
    examples=(ChangeTargetParams(change_id=1),),
    name="restore",
    title="Restore change",
    description="Restore one reverted change without resuming its execution.",
    category=ActionCategory.CLEANUP,
    run=restore_change,
    form="change_step",
)
