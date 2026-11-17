"""Typed project operation-history actions."""

from __future__ import annotations

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    OperationRedoer,
    OperationTransition,
    OperationUndoer,
)


class OperationStepParams(ActionParams):
    expected_op_id: int | None = Field(default=None, gt=0, strict=True)


def undo_operation(
    params: OperationStepParams, operations: OperationUndoer
) -> OperationTransition:
    return operations.undo(expected_op_id=params.expected_op_id)


def redo_operation(
    params: OperationStepParams, operations: OperationRedoer
) -> OperationTransition:
    return operations.redo(expected_op_id=params.expected_op_id)


UNDO = action(
    examples=(OperationStepParams(expected_op_id=1),),
    name="undo",
    title="Operation undo",
    description="Undo the most recent applied project operation.",
    category=ActionCategory.CLEANUP,
    run=undo_operation,
    form="operation_step",
)

REDO = action(
    examples=(OperationStepParams(expected_op_id=1),),
    name="redo",
    title="Operation redo",
    description="Redo the next undone project operation.",
    category=ActionCategory.CLEANUP,
    run=redo_operation,
    form="operation_step",
)


class OperationTargetParams(ActionParams):
    op_id: int = Field(gt=0, strict=True)


def revert_operation(
    params: OperationTargetParams, operations: OperationUndoer
) -> OperationTransition:
    return operations.revert(op_id=params.op_id)


def restore_operation(
    params: OperationTargetParams, operations: OperationRedoer
) -> OperationTransition:
    return operations.restore(op_id=params.op_id)


REVERT = action(
    examples=(OperationTargetParams(op_id=1),),
    name="revert",
    title="Revert change",
    description="Revert one selected change without moving Undo/Redo navigation.",
    category=ActionCategory.CLEANUP,
    run=revert_operation,
    form="operation_step",
)

RESTORE = action(
    examples=(OperationTargetParams(op_id=1),),
    name="restore",
    title="Restore change",
    description="Restore one reverted change without resuming its execution.",
    category=ActionCategory.CLEANUP,
    run=restore_operation,
    form="operation_step",
)
