"""Refresh existing derived sheets through the host's atomic transition."""

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import ActionParams, RefreshedSheet, SheetRefresher


class RefreshParams(ActionParams):
    sheet_id: int = Field(
        gt=0, strict=True, description="Derived sheet to refresh in place."
    )


def refresh(params: RefreshParams, sheets: SheetRefresher) -> RefreshedSheet:
    return sheets.refresh(params.sheet_id)


REFRESH = action(
    name="refresh",
    title="Refresh derived sheet",
    description=(
        "Rebuild a supported list or join sheet against current parent data, "
        "preserving sheet and column identities. Refresh replaces all child rows. "
        "Join fan-out and historical model estimates may require confirmation; "
        "supported refreshes do not call a model."
    ),
    category=ActionCategory.CONVERT,
    run=refresh,
    form="sheet_refresh",
    examples=(RefreshParams(sheet_id=1),),
)
