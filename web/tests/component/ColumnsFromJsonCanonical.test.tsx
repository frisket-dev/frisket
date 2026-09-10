// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import * as apiModule from '../../src/api/open';
import type { RegisteredActionRequest } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { columnDef } from '../support/domainFixtures';

const ENTRY = syntheticActionCatalogEntry('map.columns_from_json');
const CATALOG = completeCatalogPayload([ENTRY]);
const TEMPLATES = actionTemplatesFromCatalog(CATALOG);
const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'customer_id', type: 'text' }),
  columnDef({
    id: '2', name: 'api_result', type: 'json',
    ai: { actionName: 'Call an API', prompt: '', model: '', costSoFar: 0, versions: [] },
  }),
  columnDef({ id: '3', name: 'payload_backup', type: 'json' }),
], { id: '7', name: 'Customers', rowCount: 3 });

let stores: WorkspaceStores | null = null;

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => 'columns-json-request' });
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay', live_calls_possible: true, cache_mode_editable: false,
    email_from_address: null, email_from_name: null, recipe_fence_posture: 'enforced',
  });
  stores = createWorkspaceStores('columns-from-json-canonical');
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(async ({ params }) => {
    const routes = Array.isArray(params.routes) ? params.routes : [];
    const incomplete = routes.some((route) => (
      !route || typeof route !== 'object'
      || typeof (route as { name?: unknown }).name !== 'string'
      || !(route as { name: string }).name.trim()
      || typeof (route as { path?: unknown }).path !== 'string'
      || !(route as { path: string }).path.trim()
    ));
    return {
      diagnostics: incomplete ? {
        routes: { ok: false, message: 'Every route needs a name and JSON path.' },
      } : {},
      logical_outputs: incomplete ? [] : routes.flatMap((route) => (
        route && typeof route === 'object' && typeof (route as { name?: unknown }).name === 'string'
          ? [{ key: (route as { name: string }).name, column_type: 'json' }]
          : []
      )),
    };
  });
  stores.actionCatalog.store.set(() => ({
    status: 'ready', error: null, catalog: CATALOG, resolvedTemplates: TEMPLATES, version: 1,
  }));
});

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderPanel(props: {
  onExecute: (request: RegisteredActionRequest, intent: 'preview' | 'run') => void;
  selectedRowIds?: string[];
  inspectProposal?: { seq: number; title: string; spec: Record<string, unknown> };
  routeActionKind?: string;
}) {
  const { onExecute, ...panelProps } = props;
  render(
    <WorkspaceStoresContext.Provider value={stores!}>
      <ActionPanel
        sheet={SHEET}
        running={false}
        onRun={vi.fn()}
        onExecuteRegisteredAction={onExecute}
        {...panelProps}
      />
    </WorkspaceStoresContext.Provider>,
  );
}

describe('map.columns_from_json generated request drawer', () => {
  it('emits a fresh multi-route registered request directly', async () => {
    const user = userEvent.setup();
    const onExecute = vi.fn();
    renderPanel({ routeActionKind: 'map.columns_from_json', onExecute });

    await user.type(await screen.findByTestId('columns-from-json-path-0'), '$.address.city');
    await user.type(screen.getByTestId('columns-from-json-name-0'), 'city');
    await user.click(screen.getByTestId('columns-from-json-add-route'));
    fireEvent.change(screen.getByTestId('columns-from-json-path-1'), {
      target: { value: '$.tags[0]' },
    });
    await user.type(screen.getByTestId('columns-from-json-name-1'), 'first_tag');
    await waitFor(() => expect(screen.getByTestId('field-output-city')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledWith({
      action_id: 'map.columns_from_json',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source_column: 'payload_backup',
        routes: [
          { name: 'city', path: '$.address.city' },
          { name: 'first_tag', path: '$.tags[0]' },
        ],
      },
      output_names: { city: 'city', first_tag: 'first_tag' },
      idempotency_key: 'web-map.columns_from_json:columns-json-request',
    }, 'run');
  });

  it('blocks a wholly blank added route instead of submitting it', async () => {
    const user = userEvent.setup();
    const onExecute = vi.fn();
    renderPanel({ routeActionKind: 'map.columns_from_json', onExecute });

    await user.type(await screen.findByTestId('columns-from-json-path-0'), '$.city');
    await user.type(screen.getByTestId('columns-from-json-name-0'), 'city');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('columns-from-json-add-route'));

    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toHaveAttribute(
      'title', 'Every route needs a name and JSON path.',
    ));
    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('reopens a registered draft and preserves its exact selected scope', async () => {
    const onExecute = vi.fn();
    renderPanel({
      selectedRowIds: ['101', '102'],
      inspectProposal: {
        seq: 12,
        title: 'Saved JSON routes',
        spec: {
          action_id: 'map.columns_from_json',
          scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [102] },
          params: {
            source_column: 'payload_backup',
            routes: [{ name: 'city', path: '$.address.city' }],
          },
          output_names: { city: 'location' },
        },
      },
      onExecute,
    });

    expect(await screen.findByTestId('field-source_column')).toHaveValue('payload_backup');
    expect(screen.getByTestId('columns-from-json-path-0')).toHaveValue('$.address.city');
    expect(await screen.findByTestId('field-output-city')).toHaveValue('location');
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [102] },
      params: {
        source_column: 'payload_backup',
        routes: [{ name: 'city', path: '$.address.city' }],
      },
      output_names: { city: 'location' },
    }), 'preview');
  });
});
