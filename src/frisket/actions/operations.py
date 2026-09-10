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
