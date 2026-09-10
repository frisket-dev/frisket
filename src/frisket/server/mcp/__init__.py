"""frisket MCP server.

frisket exposed as MCP tools — list_projects, read_sheet, search,
run_action, backfill_run, get_run_status — over two bindings:

- Local (stdio, keyless): `frisket mcp <workspace-dir>` binds the tools
  DIRECTLY to the store/runner via the same Workspace object the HTTP API
  uses; no separate server process needed.
- Hosted (PAT): `frisket mcp --hosted` is a thin client over the hosted
  /api/* surface, authenticated with `Authorization: Bearer frisket_pat_*`
  from FRISKET_PAT / FRISKET_BASE_URL.
"""

from frisket.server.mcp.backends import HostedBackend, LocalBackend
from frisket.server.mcp.server import create_mcp_server, main

__all__ = ["HostedBackend", "LocalBackend", "create_mcp_server", "main"]
