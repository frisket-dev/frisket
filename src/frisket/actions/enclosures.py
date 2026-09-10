"""Download RSS enclosures into their existing source rows."""

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.enclosure_types import (
    EnclosureMaterializer,
    MaterializedEnclosures,
)
from frisket.actions.types import ActionParams


class MaterializeParams(ActionParams):
    force: bool = Field(default=False, strict=True)


def materialize(
    params: MaterializeParams, enclosures: EnclosureMaterializer
) -> MaterializedEnclosures:
    return enclosures.materialize(force=params.force)


MATERIALIZE = action(
    name="enclosure_materialize",
    title="Materialize RSS enclosures",
    description="Download selected RSS enclosures into their existing media cells.",
    category=ActionCategory.CONVERT,
    run=materialize,
    examples=(MaterializeParams(),),
)
