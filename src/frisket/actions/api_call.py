"""Templated per-row HTTP requests using the admitted host transport."""

from pydantic import BaseModel, Field, JsonValue
from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.http_types import HttpRequest, HttpRequester
from frisket.actions.types import ActionParams, Row, RowResult


class ApiCallParams(ActionParams):
    request: HttpRequest


class ApiCallOutput(BaseModel):
    api_result: dict[str, JsonValue] = Field(json_schema_extra={"default_hidden": True})


async def call_api(
    params: ApiCallParams, row: Row, requester: HttpRequester
) -> RowResult[ApiCallOutput]:
    return RowResult(
        output=ApiCallOutput(
            api_result=await requester.request_json(params.request, row)
        )
    )


API_CALL = action(
    name="api_call",
    title="Call an API per row",
    description="Send a templated HTTP request for each row and retain its JSON object response. Requests may have external side effects; interrupted requests can be repeated on retry.",
    category=ActionCategory.SOURCES,
    run=map_rows(call_api),
    examples=(
        ApiCallParams(
            request=HttpRequest(
                url="https://api.example.com/v1/items/{{id}}",
                headers=[("Authorization", "Bearer {{secret.API_TOKEN}}")],
            )
        ),
    ),
)
