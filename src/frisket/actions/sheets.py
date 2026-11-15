"""Refresh existing derived sheets through the host's atomic transition."""

from typing import Literal

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import ActionParams, RefreshedSheet, SheetRefresher


class RefreshParams(ActionParams):
    sheet_id: int = Field(
        gt=0, strict=True, description="Derived sheet to refresh in place."
    )

    update_policy: Literal["keep_changes", "incoming_wins"] = "keep_changes"


def refresh(params: RefreshParams, sheets: SheetRefresher) -> RefreshedSheet:
    return sheets.refresh(params.sheet_id)


REFRESH = action(
    name="refresh",
    title="Refresh derived sheet",
    description=(
        "Rebuild a supported list, join, or grouped-summary sheet against current parent data, "
        "preserving sheet and column identities. Join rows retain edits by default; lists replace generated rows without guessing item identity. "
        "Join fan-out and grouped-summary model estimates may require confirmation."
    ),
    category=ActionCategory.CONVERT,
    run=refresh,
    form="sheet_refresh",
    examples=(RefreshParams(sheet_id=1),),
)
