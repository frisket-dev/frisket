"""The frisket MCP server: six typed tools over a swappable backend.

Built on the installed ``mcp`` python SDK's FastMCP (verified against
mcp 1.27.2): ``@server.tool()`` derives the input schema from the type
annotations and the description from the docstring; dict returns become
structured content. The same tool definitions serve both bindings —
:class:`~frisket.mcp.backends.LocalBackend` (direct store/runner, stdio,
keyless) and :class:`~frisket.mcp.backends.HostedBackend` (HTTP + PAT).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from frisket.actions.types import ActionRequest, ProjectScope, SheetRows
from frisket.contracts.action import SheetRowScope
from frisket.server.mcp.backends import HostedBackend, LocalBackend

_INSTRUCTIONS = """\
frisket: AI-augmented sheets for reporters. Projects hold sheets of rows;
actions (map.classify/map.extract/etc.) transform rows into new columns.
Typical flow: list_projects -> read_sheet (paginate!) -> run_action ->
poll get_run_status. If a run half-fails, call backfill_run: it reconstructs
the producing action as a fresh explicitly scoped generation. Existing cell
heads outside that scope remain untouched, and only selected rows are bought.
Never repair a partial column with an unrelated second run_action. A run_action
or backfill_run answer of
status=needs_confirmation is the
cost gate, not an error: surface the estimate (null estimate = the price is
UNKNOWN), details, and promise_set_hash to the human. Re-call with
confirmed=true AND consented_promise_set_hash set to that exact returned hash
only if they agree, without changing the action. A bare confirmed=true is never
approval for a changed scope."""


class ActionToolSpec(BaseModel):
    """Canonical action envelope carried by the MCP wire.

    ``schema_version`` deliberately has no Python default: omitting the wire
    version must fail MCP input validation rather than silently selecting a
    compatibility dialect.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["frisket.action.v2"]
    kind: str = Field(min_length=1)
    params: dict[str, Any]
    row_scope: SheetRowScope | None = None
    input_refs: list[dict[str, Any]] = Field(default_factory=list)
    output_intent: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class RegisteredActionToolSpec(BaseModel):
    """Typed action draft accepted by MCP before host authorization is added."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    scope: ProjectScope | SheetRows = Field(discriminator="kind")
    params: dict[str, Any]
    output_names: dict[str, str] = Field(default_factory=dict)
    replace_existing: bool = Field(default=False, strict=True)
    sheet_name: Annotated[
        str | None, AfterValidator(ActionRequest._valid_sheet_name)
    ] = None
    idempotency_key: str | None = Field(default=None, min_length=1)


async def _call(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke a backend method that may be sync (local) or async (hosted)."""
    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


def create_mcp_server(backend: LocalBackend | HostedBackend) -> FastMCP:
    server = FastMCP("frisket", instructions=_INSTRUCTIONS)

    @server.tool()
    async def list_projects() -> dict:
        """List the frisket projects in this workspace (id + name). Project
        ids are the strings every other tool takes as project_id."""
        return {"projects": await _call(backend.list_projects)}

    @server.tool()
    async def list_sheets(project_id: str) -> dict:
        """List visible sheets in a project, including id, name and row count.
        Use the returned id as sheet_id when reading or acting on a sheet."""
        return {"sheets": await _call(backend.list_sheets, project_id)}

    @server.tool()
    async def read_sheet(
        project_id: str, sheet_id: int, offset: int = 0, limit: int = 100
    ) -> dict:
        """Read a page of rows from a sheet. Returns columns (id, name, type,
        ai_generated) and rows whose cells are keyed by column NAME, plus
        total/offset/limit for paging. Sheets can be 100k+ rows: limit is
        capped server-side at 1000 — page with offset, never assume one call
        returned everything (compare len(rows)+offset to total)."""
        return await _call(backend.read_sheet, project_id, sheet_id, offset, limit)

    @server.tool()
    async def search(project_id: str, query: str, limit: int = 20) -> dict:
        """Full-text search across every sheet of a project. Returns ranked
        hits with sheet/row/column coordinates and a snippet; feed a hit's
        sheet_id into read_sheet to see the full row in context."""
        return {
            "query": query,
            "results": await _call(backend.search, project_id, query, limit),
        }

    @server.tool()
    async def run_action(
        project_id: str,
        action: ActionToolSpec | RegisteredActionToolSpec,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        """Run a frisket action. Registered typed actions carry action_id,
        scope, params, output_names, an optional replace_existing flag, and
        sheet_name when creating a sheet.
        Other catalog actions carry
        schema_version="frisket.action.v2", for example
        {"schema_version": "frisket.action.v2", "kind": "map.classify",
        "capabilities": ["project:write", "model:complete"], "params":
        {"sheet_id": 1, "model": "anthropic/claude-haiku-4-5",
        "input_columns": ["text"], "fields": [{"name": "...", "type":
        "score", "description": "..."}]}}. map.ner requires a top-level
        row_scope with sheet_id and either an all_rows selector or an
        exact_membership selector; its params omit sheet_id and row_ids. Most
        other map actions still carry sheet_id and optional row_ids in params.
        If a run half-fails, call backfill_run instead of re-running the whole
        action: it creates a fresh successor generation for only the selected
        rows, while completed cell heads remain unchanged. Runless queued action-job kinds
        are refused with queued_action_job_not_supported because this tool has
        no receipt-polling contract. Run-backed actions return
        {status: "started", run_id, done}; poll get_run_status with the
        run_id. Synchronous direct actions return {status: "completed",
        done: true, receipt_id, outputs, value} and need no polling. A failed,
        cancelled, or partial direct action with a receipt also returns its terminal
        status, errors, and any completed outputs; do not assume failure undid them.
        If the estimated cost trips the
        gate you get {status: "needs_confirmation", estimate, estimate_known,
        estimate_details, details, promise_set_hash, message} instead:
        estimate is USD, estimate_details is the complete quote, and
        estimate=null means the price is UNKNOWN (unpriced model, or this
        deployment's pricing policy declined to price the run), which needs
        the same human sign-off. Re-call with confirmed=true AND
        consented_promise_set_hash set to the exact returned promise_set_hash
        ONLY after the human approves, and do not change the action between the
        challenge and approval. A changed action gets a new 402 and hash."""
        return await _call(
            backend.run_action,
            project_id,
            action.model_dump(mode="json", exclude_unset=True),
            confirmed,
            consented_promise_set_hash,
        )

    # Consenting to changed terms needs no tool of its own: a fresh scoped
    # backfill re-challenges through its own needs_confirmation flow, which
    # renders the new claims and binds them on the exact echo.
    @server.tool()
    async def backfill_run(
        project_id: str,
        sheet_id: int,
        column: str,
        row_ids: list[int] | None = None,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        """Create a fresh explicitly scoped generation from an AI-generated
        column's exact source generation. Existing cell heads remain unchanged;
        only selected rows run and only they are billed. Omit row_ids
        for the automatic sweep (fills every row without a terminal result;
        terminally-failed rows are left alone — automation never claims a
        retry will help); pass row_ids to deliberately re-run exactly those
        rows regardless of their current outcome. The selected rows must resolve
        to one source generation. Returns {status, run_id, requested_row_ids,
        filled_row_ids, filled, receipt_id, message}; every cell outside the
        requested scope keeps its published generation and remains free.
        status=needs_confirmation is the
        same cost gate as run_action: surface the estimate and
        promise_set_hash to the human, then re-call with confirmed=true AND
        consented_promise_set_hash set to that exact hash only if they
        approve. status=failed carries the named refusal, such as
        mixed_origin_column_unsupported or backfill_source_unavailable."""
        return await _call(
            backend.backfill_run,
            project_id,
            sheet_id,
            column,
            row_ids,
            confirmed,
            consented_promise_set_hash,
        )

    @server.tool()
    async def get_run_status(project_id: str, run_id: int) -> dict:
        """Progress of a run: status (running/completed/failed/cancelled),
        total/completed/failed row counts, cost in USD so far, and live
        (whether this server still holds the in-flight handle)."""
        return await _call(backend.get_run_status, project_id, run_id)

    return server


def main(argv: list[str] | None = None) -> int:
    """`frisket mcp [workspace-dir] [--hosted]` — serve the tools over stdio.

    Local (default): bind directly to the workspace directory (keyless, no
    HTTP server needed). Hosted (--hosted): thin client over the hosted API
    using FRISKET_BASE_URL + FRISKET_PAT from the environment.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    backend: LocalBackend | HostedBackend
    if "--hosted" in args:
        args.remove("--hosted")
        backend = HostedBackend()
        where = "hosted API (PAT auth)"
    else:
        root = Path(args[0]) if args else Path.cwd() / "frisket-projects"
        backend = LocalBackend(root)
        where = f"workspace {root}"
    # stdout belongs to the stdio transport; human chatter goes to stderr
    print(f"frisket mcp: serving MCP tools over stdio against {where}", file=sys.stderr)
    create_mcp_server(backend).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
