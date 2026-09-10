"""An installed Action whose output stamps the plugin's own identity.

The legacy version of this fixture ran in a trusted-local SUBPROCESS and had
the host hand it a handler `ctx`, so its stamp could quote `ctx.plugin_id`,
`ctx.handler_key`, the dispatch row id, and a `safe:` probe asserting the ctx
carried no project/db/filesystem handle. None of that survives: an installed
Action is an ordinary Action running in process on the same native hosts as a
builtin, and a `map_rows` handler receives exactly its typed Params and the
Row. Identity is therefore declared here, once, and reused by both the stamp
and the `Plugin` below, so the stamp still names the plugin and the handler it
came from without a host-injected ctx to read them from.
"""

from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin

PLUGIN_ID = "demo.receipt_stamp"
ACTION_NAME = "stamp"
HANDLER_KEY = f"{PLUGIN_ID}:{ACTION_NAME}"


class StampParams(ActionParams):
    name: ColumnRef[str]


class StampOutput(BaseModel):
    receipt_stamp: str


def stamp(params: StampParams, row: Row) -> RowResult[StampOutput]:
    value = params.name.read(row)
    raw = "" if value is None else str(value)
    return RowResult(
        output=StampOutput(receipt_stamp=f"{raw}|{PLUGIN_ID}|{HANDLER_KEY}")
    )


STAMP = action(
    name=ACTION_NAME,
    title="Stamp receipt proof",
    description="Writes a deterministic installed-Action stamp.",
    category=ActionCategory.TEXT,
    run=map_rows(stamp),
)

plugin = Plugin(
    id=PLUGIN_ID,
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(STAMP,),
)
