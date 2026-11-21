// @vitest-environment jsdom
//
// The typed form owns the run request. MCP selection is a map.mcp_extract
// Params concern (`mcp_server_ids`, stable server references — never tool
// ids); map.extract must remain unchanged.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { lastExecutedRequest, mockLocalProviders, mountTypedForm, pressRun } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  vi.restoreAllMocks();
});

beforeEach(() => {
  installPopoverPolyfill();
  mockLocalProviders();
});

const sheet = () => sheetMeta([columnDef({ id: '1', name: 'Company', type: 'text' })], { id: '7' });

const servers = [
  { id: 'local-crm', name: 'Local CRM', enabled: true, last_discovered_tool_count: 8 },
  { id: 'disabled-browser', name: 'Browser', enabled: false, last_discovered_tool_count: 2 },
];

describe('Tool-assisted Extract MCP wiring', () => {
  it('loads enabled servers, saves stable mcp_server_ids into the run, and never exposes tool ids', async () => {
    const user = userEvent.setup();
    const mounted = mountTypedForm({ kind: 'map.mcp_extract', sheet: sheet(), listMcpServers: servers });
    dispose = mounted.dispose;
    await userEvent.click(screen.getByTestId('text-source-columns'));
    await userEvent.click(screen.getByRole('option', { name: /^Company/, hidden: true }));
    await userEvent.keyboard('{Escape}');

    await user.click(await screen.findByRole('button', { name: /^tools/i }));
    await user.click(await screen.findByRole('checkbox', { name: 'Local CRM' }));
    expect(screen.getByRole('checkbox', { name: 'Browser' })).toBeDisabled();
    expect(screen.queryByText(/search customers/i)).not.toBeInTheDocument();

    await pressRun();
    const request = lastExecutedRequest(mounted.onExecute);
    expect(request).toMatchObject({
      action_id: 'map.mcp_extract',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['Company'], mcp_server_ids: ['local-crm'] },
    });
    expect(request).not.toHaveProperty('canonicalAction');
    expect(request).not.toHaveProperty('actionKind');
  });

  // The legacy test only checked nothing auto-ran; this one runs on purpose
  // to prove the typed map.extract request carries no `mcp_server_ids`.
  it('does not render or serialize MCP selection for ordinary map.extract', async () => {
    const mounted = mountTypedForm({ kind: 'map.extract', sheet: sheet(), listMcpServers: servers });
    dispose = mounted.dispose;
    await userEvent.click(screen.getByTestId('text-source-columns'));
    await userEvent.click(screen.getByRole('option', { name: /^Company/, hidden: true }));
    await userEvent.keyboard('{Escape}');

    expect(screen.queryByTestId('mcp-tools-selector')).not.toBeInTheDocument();
    await pressRun();
    expect(lastExecutedRequest(mounted.onExecute).params).not.toHaveProperty('mcp_server_ids');
  });
});
