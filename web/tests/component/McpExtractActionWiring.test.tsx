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
import { lastExecutedRequest, mockLocalProviders, mountTypedForm, pressRun } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  vi.restoreAllMocks();
});

beforeEach(() => {
  mockLocalProviders();
});

const sheet = () => sheetMeta([columnDef({ id: '1', name: 'Company', type: 'text' })], { id: '7' });

const servers = [
  { id: 'local-crm', name: 'Local CRM', enabled: true, last_discovered_tool_count: 8 },
  { id: 'disabled-browser', name: 'Browser', enabled: false, last_discovered_tool_count: 2 },
];

describe('Tool-assisted Extract MCP wiring', () => {
  // PRODUCT BUG (left red on purpose): a FRESH map.mcp_extract form crashes on
  // mount with `TypeError: mcpServerIds.filter is not a function`.
  // GeneratedActionForm.initialParams
  // (web/src/components/action-panel/GeneratedActionForm.tsx:216-218) seeds
  // a param whose schema is a bare `$ref` (McpServers) as `''` because it
  // reads `schema.type` off the unresolved property, and
  // McpExtractParamsBody (ExtractParamsBody.tsx:94) forwards that string as
  // `mcpServerIds`. Reachable from the launcher: DeriveActionForm passes a
  // fresh launch (no initialDraft) straight through. Smallest repair: add
  // `mcp_server_ids: []` to the 'map.mcp_extract' customization's
  // initialParams (generatedActionCustomizations.tsx:292).
  it('loads enabled servers, saves stable mcp_server_ids into the run, and never exposes tool ids', async () => {
    const user = userEvent.setup();
    const mounted = mountTypedForm({ kind: 'map.mcp_extract', sheet: sheet(), listMcpServers: servers });
    dispose = mounted.dispose;

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

    expect(screen.queryByTestId('mcp-tools-selector')).not.toBeInTheDocument();
    await pressRun();
    expect(lastExecutedRequest(mounted.onExecute).params).not.toHaveProperty('mcp_server_ids');
  });
});
