// @vitest-environment jsdom
//
// Tool-assisted Extract selects trusted MCP *servers*, not a frozen subset of
// their current tools. The discovered list is intentionally advisory: each
// run uses every currently usable tools/call tool from its selected servers.

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { McpToolsSelector } from '../../src/components/action-panel/McpToolsSelector';
import { installPopoverPolyfill } from '../support/domPolyfills';

beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(cleanup);

const SERVERS = [
  { id: 'local-crm', name: 'Local CRM', enabled: true, lastDiscoveredToolCount: 8 },
  { id: 'filesystem', name: 'Filesystem', enabled: true, lastDiscoveredToolCount: 5 },
  { id: 'browser', name: 'Browser', enabled: false, lastDiscoveredToolCount: 12 },
];

describe('McpToolsSelector', () => {
  it('opens as a compact Tools control and selects enabled servers at the all-tools level', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <McpToolsSelector
        servers={SERVERS}
        mcpServerIds={[]}
        onMcpServerIdsChange={onChange}
      />,
    );

    expect(screen.getByRole('button', { name: /^tools/i })).toHaveTextContent('0 MCP servers');
    await user.click(screen.getByRole('button', { name: /^tools/i }));

    expect(screen.getByRole('checkbox', { name: /local crm/i })).not.toBeChecked();
    expect(screen.getByText('All tools · 8 last discovered')).toBeInTheDocument();
    expect(screen.getByText('All tools · 5 last discovered')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /browser/i })).toBeDisabled();

    await user.click(screen.getByRole('checkbox', { name: /local crm/i }));
    expect(onChange).toHaveBeenLastCalledWith(['local-crm']);
  });

  it('serializes multiple selections as stable mcp_server_ids, never individual tool ids', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <McpToolsSelector
        servers={SERVERS}
        mcpServerIds={['local-crm']}
        onMcpServerIdsChange={onChange}
      />,
    );

    await user.click(screen.getByRole('button', { name: /^tools/i }));
    await user.click(screen.getByRole('checkbox', { name: /filesystem/i }));

    expect(onChange).toHaveBeenLastCalledWith(['local-crm', 'filesystem']);
    expect(screen.queryByRole('checkbox', { name: /search customers/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /read file/i })).not.toBeInTheDocument();
  });

  it('keeps tool inventory advisory instead of pinning a stale tool contract', async () => {
    const user = userEvent.setup();
    render(
      <McpToolsSelector
        servers={SERVERS}
        mcpServerIds={['local-crm', 'filesystem']}
        onMcpServerIdsChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: /^tools/i })).toHaveTextContent('2 MCP servers');
    await user.click(screen.getByRole('button', { name: /^tools/i }));
    expect(screen.getByText(/all current tools from each selected server/i)).toBeInTheDocument();
    expect(screen.getAllByText(/last discovered/i)).not.toHaveLength(0);
  });

  it('keeps saved unavailable servers visible and lets the user remove them', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <McpToolsSelector
        servers={SERVERS}
        mcpServerIds={['removed-server']}
        onMcpServerIdsChange={onChange}
      />,
    );

    await user.click(screen.getByRole('button', { name: /^tools/i }));
    const missing = screen.getByRole('checkbox', { name: 'removed-server' });
    expect(missing).toBeChecked();
    expect(missing).not.toBeDisabled();
    expect(screen.getByText(/unavailable saved server/i)).toBeInTheDocument();
    await user.click(missing);
    expect(onChange).toHaveBeenCalledWith([]);
  });
});
