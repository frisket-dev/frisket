import { useState } from 'react';

export interface McpToolsSelectorServer {
  id: string;
  name: string;
  enabled: boolean;
  lastDiscoveredToolCount?: number;
}

export interface McpToolsSelectorProps {
  servers: readonly McpToolsSelectorServer[];
  /** Stable server references saved on Tool-assisted Extract, never tool IDs. */
  mcpServerIds: readonly string[];
  onMcpServerIdsChange: (mcpServerIds: string[]) => void;
}

/**
 * Server-level MCP affordance for Tool-assisted Extract. A selected server
 * contributes its complete current tools/call inventory when a run starts;
 * displayed discovery data is strictly informational.
 */
export function McpToolsSelector({
  servers,
  mcpServerIds,
  onMcpServerIdsChange,
}: McpToolsSelectorProps) {
  const [open, setOpen] = useState(false);
  const selected = new Set(mcpServerIds);
  const label = `${mcpServerIds.length} MCP server${mcpServerIds.length === 1 ? '' : 's'}`;
  const knownIds = new Set(servers.map((server) => server.id));
  const visibleServers = [
    ...servers.map((server) => ({ ...server, unavailable: false })),
    ...mcpServerIds
      .filter((id) => !knownIds.has(id))
      .map((id) => ({ id, name: id, enabled: false, unavailable: true, lastDiscoveredToolCount: undefined })),
  ];

  const setSelected = (serverId: string, nextSelected: boolean) => {
    const next = new Set(mcpServerIds);
    if (nextSelected) next.add(serverId);
    else next.delete(serverId);
    onMcpServerIdsChange(visibleServers.map((server) => server.id).filter((id) => next.has(id)));
  };

  return (
    <div className="action-mcp-tools-selector" data-testid="mcp-tools-selector">
      <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        Tools · {label}
      </button>
      {open && (
        <div role="dialog" aria-label="MCP tools">
          <p>All current tools from each selected server are available when this run starts.</p>
          {visibleServers.map((server) => (
            <label key={server.id}>
              <input
                type="checkbox"
                aria-label={server.name}
                checked={selected.has(server.id)}
                disabled={!server.enabled && !selected.has(server.id)}
                onChange={(event) => setSelected(server.id, event.currentTarget.checked)}
              />
              <span>{server.name}</span>
              {server.unavailable
                ? <span>Unavailable saved server</span>
                : <span>All tools · {server.lastDiscoveredToolCount ?? 0} last discovered</span>}
              {!server.enabled && !server.unavailable && <span>Disabled</span>}
            </label>
          ))}
          <p>Last discovered tool counts are advisory and may change before a run.</p>
        </div>
      )}
    </div>
  );
}
