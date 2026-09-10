"""Mixed-surface smoke fixture: one native Action plus the importer, operator
and projection contributions, which keep their own decorator families and
their own subprocess handler contexts.

Only the action moved to the native contract. The manifest stays hand
authored, because generation deliberately covers Plugin(actions=...) alone and
points mixed packages at the plugin.config.mjs build path.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class SmokeParams(ActionParams):
    name: ColumnRef[str]


class SmokeOutput(BaseModel):
    smoke_result: str


async def real_local_smoke_action(
    params: SmokeParams, row: Row
) -> RowResult[SmokeOutput]:
    name = str(params.name.read(row) or "")
    return RowResult(
        output=SmokeOutput(smoke_result=f"real-local-action:{name or 'ok'}")
    )


REAL_LOCAL_SMOKE = action(
    name="real_local_smoke",
    title="Real local smoke action",
    description="Writes a deterministic smoke marker for one text column.",
    category=ActionCategory.TEXT,
    run=map_rows(real_local_smoke_action),
)

plugin = Plugin(
    id="frisket.geosmoke",
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(REAL_LOCAL_SMOKE,),
)


@plugin.importer(
    "frisket_geosmoke_real_local_smoke_import",
    handler_key="real_local_smoke_importer",
    title="Real local smoke importer",
    columns=[
        {"name": "name", "type": "text"},
        {"name": "score", "type": "integer"},
    ],
)
async def real_local_smoke_importer(_ctx, _source, **handler_params: Any):
    city = str(handler_params.get("city") or "New York")
    yield {"name": city, "score": 9}
    yield {"name": "Boston", "score": 7}


@plugin.operator(
    "real_local_smoke_equals",
    handler_key="real_local_smoke_operator",
    title="Real local smoke equals",
)
async def real_local_smoke_operator(
    _ctx,
    rows: list[dict[str, Any]],
    *,
    value: Any,
    target: dict[str, Any],
    params: dict[str, Any],
) -> list[int]:
    del target, params
    expected = str(value or "").lower()
    return [
        int(row["rowId"])
        for row in rows
        if str(row.get("value") or "").lower() == expected
    ]


@plugin.projection(
    "map_points",
    handler_key="real_local_smoke_map_points",
    title="Real local smoke map points projection",
)
async def real_local_smoke_map_points_projection(
    ctx,
    _rows: list[dict[str, Any]],
    *,
    target: dict[str, Any],
    params: dict[str, Any],
    mode: str = "status",
) -> dict[str, Any]:
    del target, params
    projection_kind = ctx.projection_kind
    if mode == "status":
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "stale",
            "freshness": {
                "state": "stale",
                "generation": "real-local-smoke-gen-1",
                "transient": False,
            },
            "outputs": {
                "artifactRefs": [
                    {
                        "kind": "projection_artifact",
                        "projectionKind": projection_kind,
                        "artifactId": "projection://real-local-smoke/gen-1",
                    }
                ],
                "metrics": {"handler": "frisket.geosmoke:real_local_smoke_map_points"},
            },
            "warnings": [],
        }
    return {
        "schemaVersion": "frisket.runtime_projection_build_plan.v1",
        "status": "accepted",
        "build": {
            "operation": "refresh",
            "idempotencyKey": "real-local-smoke-map-points@gen-2",
        },
        "outputs": {
            "artifactRefs": [
                {
                    "kind": "projection_artifact",
                    "projectionKind": projection_kind,
                    "artifactId": "projection://real-local-smoke/gen-2",
                }
            ],
            "metrics": {"handler": "frisket.geosmoke:real_local_smoke_map_points"},
        },
        "warnings": [],
    }
