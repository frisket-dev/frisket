// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as apiModule from '../../src/api/open';
import type { GeneratedActionDraft } from '../../src/api/types';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { API_CALL_CATALOG, API_CALL_SHEET, API_CALL_TEMPLATES,
  resolveApiCallParams } from '../support/apiCallActionFixture';

let stores: WorkspaceStores | null = null;
beforeEach(() => {
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay', live_calls_possible: true, cache_mode_editable: false,
    email_from_address: null, email_from_name: null, recipe_fence_posture: 'enforced',
  });
  stores = createWorkspaceStores('api-call-canonical');
  stores.actionCatalog.store.set(() => ({ status: 'ready', error: null,
    catalog: API_CALL_CATALOG, resolvedTemplates: API_CALL_TEMPLATES, version: 1 }));
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(resolveApiCallParams);
});
afterEach(() => { cleanup(); stores?.dispose(); stores = null; vi.restoreAllMocks(); });

describe('typed API Call discovery and saved intent', () => {
  it('launches by canonical ID without a dedicated lifecycle drawer', async () => {
    const onExecute = vi.fn();
    render(<WorkspaceStoresContext.Provider value={stores!}>
      <ActionPanel sheet={API_CALL_SHEET} running={false} onRun={vi.fn()}
        onExecuteRegisteredAction={onExecute} routeActionKind="map.api_call" routeActionLaunchId={1} />
    </WorkspaceStoresContext.Provider>);
    fireEvent.change(await screen.findByTestId('api-call-url'),
      { target: { value: 'https://example.test/static' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      action_id: 'map.api_call', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { request: { url: 'https://example.test/static' } },
      output_names: { api_result: 'api_result' },
    });
  });

  it.each([
    { url: 'https://example.test/{{customer_id}}' },
    { url: 'https://example.test', headers: [['Authorization', 'Bearer {{secret.TOKEN}}']] },
    { url: 'https://example.test', method: 'PATCH', body_mode: 'raw',
      body: ' {{customer_id}} ', timeout: 12.5, max_requests_per_second: 2.5, follow_redirects: false,
      headers: [['X:Mode', '  exact header  ']], query_params: [[' filter:kind ', '  exact query  ']],
      cookies: [['session', '{{customer_id}}']], form_body: [['inactive', 'kept']], content_type: 'text/plain' },
  ])('reopens and edits canonical saved request without manufacturing consent: %j', async (request) => {
    const draft: GeneratedActionDraft = { action_id: 'map.api_call',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] },
      params: { request }, output_names: { api_result: 'Stored response' } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(API_CALL_CATALOG, draft))).toEqual(draft);
    const onExecute = vi.fn();
    render(<WorkspaceStoresContext.Provider value={stores!}>
      <ActionPanel sheet={API_CALL_SHEET} running={false} onRun={vi.fn()}
        onExecuteRegisteredAction={onExecute}
        inspectProposal={{ seq: 1, title: 'Saved API request', spec: draft }} />
    </WorkspaceStoresContext.Provider>);
    expect(await screen.findByTestId('api-call-url')).toHaveValue(request.url);
    expect(screen.getByTestId('api-call-timeout')).toHaveValue(request.timeout ?? 30);
    fireEvent.change(screen.getByTestId('api-call-url'),
      { target: { value: 'https://example.test/edited' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    const submitted = onExecute.mock.calls[0][0];
    expect(submitted).toMatchObject({ ...draft,
      params: { request: { ...request, url: 'https://example.test/edited' } } });
    expect(submitted).not.toHaveProperty('confirmation');
    expect(submitted).not.toHaveProperty('replace_existing');
    expect(submitted.params).not.toHaveProperty('confirmed');
    expect(submitted.idempotency_key).toEqual(expect.any(String));
  });
});
