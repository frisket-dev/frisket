// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { entityTableDraft } from '../../src/actions/entityTable';
import type { ActionLaunchPrefill } from '../../src/actions/actionFormInitial';
import { type ActionCatalogPayload, type GeneratedActionDraft,
  type RegisteredActionRequest, type V1Receipt } from '../../src/api/types';
import { ApiError } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { CompletedClusterResult } from '../../src/components/CompletedClusterResult';
import { ReceiptInspector } from '../../src/components/ReceiptInspector';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { sheetMeta } from '../support/actionFormFixtures';
import { hasServedActionCatalogPython, servedActionCatalog } from '../support/servedActionCatalog';

const SHEET = sheetMeta([], { id: '7', name: 'People' });
function receipt(receiptId = 'receipt:clusters', overrides: Partial<V1Receipt> = {}): V1Receipt {
  return { schemaVersion: 'frisket.receipt.v1', receiptId, projectId: 'p',
    actionId: 'action:cluster', actionKind: 'cluster.values', runId: null, opIds: [1],
    idempotencyKey: null, paramsHash: null, status: 'completed', inputs: [], outputs: [],
    providerUse: [], evidence: [], errors: [], ...overrides };
}

describe.skipIf(!hasServedActionCatalogPython() && !process.env.CI)('entity table workflow', () => {
  let catalog: ActionCatalogPayload;
  let stores: WorkspaceStores;
  const showError = vi.fn();
  const navigate = vi.fn();
  beforeAll(() => { catalog = servedActionCatalog(); }, 30_000);
  beforeEach(() => {
    showError.mockReset();
    navigate.mockReset();
    stores = createWorkspaceStores('p', createProjectApi('p'));
    vi.spyOn(stores.projectApi, 'getReceipt').mockImplementation(async (id) => receipt(id));
    vi.spyOn(stores.projectApi, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    vi.spyOn(stores.projectApi, 'resolveActionParams').mockResolvedValue({
      diagnostics: {}, logical_outputs: catalog.actions.find((entry) =>
        entry.kind === 'resolve.entities')!.ui_hints.logical_outputs!,
    });
    stores.actionCatalog.store.set(() => ({
      status: 'ready', error: null, catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog), version: 1,
    }));
    stores.job.start({ invalidateProjectData: vi.fn(), refreshHistory: vi.fn(),
      refreshReviewCount: vi.fn(), refreshSheets: vi.fn(), showError,
      onMaterializedSheetCreated: navigate });
  });
  afterEach(() => { cleanup(); stores.dispose(); vi.restoreAllMocks(); });

  function Workflow({ history = false, draft, saved = false }: {
    history?: boolean; draft?: GeneratedActionDraft; saved?: boolean;
  }) {
    const [initial, setInitial] = useState<ActionLaunchPrefill | undefined>(
      draft && !saved ? { actionDraft: draft } : undefined,
    );
    const create = (id: string) => setInitial({ actionDraft: entityTableDraft(id) });
    return <WorkspaceStoresContext.Provider value={stores}>
      {!initial && !saved && (history
        ? <ReceiptInspector receiptId="receipt:clusters" onCreateEntityTable={create} />
        : <CompletedClusterResult onCreate={create} />)}
      {(initial || saved) && <ActionPanel sheet={SHEET} running={false} onRun={vi.fn()}
        routeActionKind={saved ? undefined : 'resolve.entities'} routeActionLaunchId={1}
        routeActionInitial={initial}
        inspectProposal={saved && draft ? { seq: 1, title: 'Saved entities', spec: draft } : undefined}
        onExecuteRegisteredAction={(request, intent) => {
          expect(intent).toBe('run');
          stores.job.startRun(request, SHEET);
        }} />}
    </WorkspaceStoresContext.Provider>;
  }

  function clusterRequest(): RegisteredActionRequest {
    return { action_id: 'cluster.values', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'name', review: { source_hash: `sha256:${'a'.repeat(64)}` } },
      output_names: { canonical: 'name_canonical' }, idempotency_key: 'cluster-workflow-test' };
  }

  it('keeps receipt-backed custom authoring available without a current sheet', async () => {
    const execute = vi.fn();
    const draft = entityTableDraft('receipt:clusters');
    render(<WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={null} running={false} onRun={vi.fn()}
        routeActionKind="resolve.entities" routeActionLaunchId={1}
        routeActionInitial={{ actionDraft: draft }} onExecuteRegisteredAction={execute} />
    </WorkspaceStoresContext.Provider>);
    expect(await screen.findByTestId('entity-table-source')).toHaveTextContent('receipt:clusters');
    await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    expect(execute).toHaveBeenCalledWith({ ...draft, idempotency_key: expect.any(String) }, 'preview');
  });

  it.each([undefined, false])('displays PDF defaults without adding them to saved Params (%s)', async (renderPages) => {
    const entry = catalog.actions.find((item) => item.kind === 'import.pdf')!;
    vi.mocked(stores.projectApi.resolveActionParams).mockResolvedValue({
      diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs!,
    });
    const params = { source: { kind: 'file', path: '/illustrative.pdf' },
      ...(renderPages === undefined ? {} : { render_pages: renderPages }) };
    const execute = vi.fn();
    render(<WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={SHEET} running={false} onRun={vi.fn()}
        routeActionKind="import.pdf" routeActionLaunchId={1}
        routeActionInitial={{ actionDraft: { action_id: 'import.pdf', scope: { kind: 'project' },
          params, output_names: {}, sheet_name: 'PDF pages' } }}
        onExecuteRegisteredAction={execute} />
    </WorkspaceStoresContext.Provider>);
    expect(await screen.findByTestId('field-dpi')).toHaveValue('150');
    if (renderPages === false) expect(screen.getByTestId('field-render_pages')).not.toBeChecked();
    else expect(screen.getByTestId('field-render_pages')).toBeChecked();
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    expect(execute).toHaveBeenCalledWith(expect.objectContaining({ action_id: 'import.pdf' }), 'run');
    expect(execute.mock.calls[0][0].params).toEqual(params);
  });

  it.each(['completion', 'history'])('%s CTA names a table and sends only the committed receipt source', async (origin) => {
    const run = vi.spyOn(stores.projectApi, 'runAction').mockResolvedValue({
      runId: null, status: 'completed', receiptId: 'receipt:entities', outputSheetId: '42',
    });
    const clusterPreview = vi.spyOn(stores.projectApi, 'clusterPreview');
    render(<Workflow history={origin === 'history'} />);
    if (origin === 'completion') {
      expect(screen.queryByRole('button', { name: 'Create entity table' })).not.toBeInTheDocument();
      run.mockResolvedValueOnce({ runId: null, status: 'completed', receiptId: 'receipt:clusters' });
      await act(async () => { stores.job.startRun(clusterRequest(), SHEET); });
    }
    fireEvent.click(await screen.findByRole('button', { name: 'Create entity table' }));
    expect(await screen.findByTestId('entity-table-source')).toHaveTextContent('receipt:clusters');
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Entities');
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(screen.getByTestId('generated-action-preview')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: ' Reviewed people ' } });
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('42'));
    const request = run.mock.calls.at(-1)![0];
    expect(request).toEqual({ ...entityTableDraft('receipt:clusters'), sheet_name: 'Reviewed people',
      idempotency_key: expect.stringMatching(/^web-resolve.entities:/) });
    expect(clusterPreview).not.toHaveBeenCalled();
    expect(stores.job.store.get().completedClusterReceiptId).toBeNull();
  });

  it('preserves richer saved options and output names without offering another group editor', async () => {
    const draft: GeneratedActionDraft = { ...entityTableDraft('receipt:clusters'),
      params: { source: { kind: 'cluster_values', receipt_id: 'receipt:clusters' },
        cluster_keys: ['jon'], canonical_overrides: { jon: 'Jonathan Smith' } },
      output_names: { entity: 'Person' }, sheet_name: 'Saved people' };
    const run = vi.spyOn(stores.projectApi, 'runAction').mockResolvedValue({ runId: null, status: 'completed' });
    render(<Workflow draft={draft} saved />);
    expect(await screen.findByText('Saved entity options (preserved)')).toBeInTheDocument();
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0][0]).toEqual({ ...draft, idempotency_key: expect.any(String) });
  });

  it.each(['receipt:clusters', 'receipt:newer'])('history launch dismisses only its matching banner (%s)', async (bannerReceiptId) => {
    vi.spyOn(stores.projectApi, 'runAction').mockResolvedValue({
      runId: null, status: 'completed', receiptId: bannerReceiptId,
    });
    render(<WorkspaceStoresContext.Provider value={stores}>
      <CompletedClusterResult onCreate={vi.fn()} />
      <Workflow history />
    </WorkspaceStoresContext.Provider>);
    await act(async () => { stores.job.startRun(clusterRequest(), SHEET); });
    expect(screen.getByTestId('completed-cluster-result')).toBeInTheDocument();
    const history = within(screen.getByTestId('receipt-inspector'));
    fireEvent.click(await history.findByRole('button', { name: 'Create entity table' }));
    expect(await screen.findByTestId('entity-table-source')).toHaveTextContent('receipt:clusters');
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Entities');
    if (bannerReceiptId === 'receipt:clusters') {
      expect(stores.job.store.get().completedClusterReceiptId).toBeNull();
      expect(screen.queryByTestId('completed-cluster-result')).not.toBeInTheDocument();
    } else {
      expect(stores.job.store.get().completedClusterReceiptId).toBe(bannerReceiptId);
      expect(screen.getByTestId('completed-cluster-result')).toBeInTheDocument();
    }
  });

  it('reports a stale committed source unchanged and permits explicit retry without reclustering', async () => {
    const refusal = new ApiError(409, 'The committed cluster source changed.', 'stale_replay');
    const run = vi.spyOn(stores.projectApi, 'runAction')
      .mockRejectedValueOnce(refusal)
      .mockResolvedValueOnce({ runId: null, status: 'completed', outputSheetId: '42' });
    const preview = vi.spyOn(stores.projectApi, 'clusterPreview');
    render(<Workflow draft={entityTableDraft('receipt:clusters')} />);
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    await waitFor(() => expect(showError).toHaveBeenCalledWith(refusal));
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('run-button'));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('42'));
    const first = run.mock.calls[0][0] as RegisteredActionRequest;
    const second = run.mock.calls[1][0] as RegisteredActionRequest;
    expect(second.params).toEqual(first.params);
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
    expect(preview).not.toHaveBeenCalled();
  });

  it.each(['failed', 'cancelled', 'receipt-free'])('does not offer entity creation for a %s cluster dispatch', async (outcome) => {
    vi.spyOn(stores.projectApi, 'runAction').mockResolvedValue({ runId: null,
      status: outcome === 'receipt-free' ? 'completed' : outcome,
      receiptId: outcome === 'receipt-free' ? null : 'receipt:failed' });
    render(<Workflow />);
    await act(async () => { stores.job.startRun(clusterRequest(), SHEET); });
    expect(screen.queryByRole('button', { name: 'Create entity table' })).not.toBeInTheDocument();
  });

  it('refuses a source-free form and exposes receipt-history recovery', async () => {
    vi.mocked(stores.projectApi.resolveActionParams).mockResolvedValue({
      diagnostics: { source: { ok: false, message: 'Select a completed clustering result.' } },
      logical_outputs: [],
    });
    render(<Workflow draft={{ ...entityTableDraft(''), params: {} }} />);
    expect(await screen.findByTestId('entity-table-source')).toHaveTextContent('A preview alone is not a result');
    expect(screen.getByTestId('run-button')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Open receipt history' }));
    expect(stores.chrome.store.get().provenanceOpen).toBe(true);
  });

  it.each([false, 0, '', { matched: 2 }])('shows a callable domain result %j in receipt history', async (value) => {
    vi.mocked(stores.projectApi.getReceipt).mockResolvedValue(receipt('receipt:query', {
      actionKind: 'query.preview', value,
    }));
    render(<Workflow history />);
    const result = await screen.findByTestId('receipt-value');
    expect(JSON.parse(result.textContent ?? '')).toEqual(value);
  });

  it('does not reuse a previous receipt CTA while the new history receipt loads or fails', async () => {
    const create = vi.fn();
    const view = render(<WorkspaceStoresContext.Provider value={stores}>
      <ReceiptInspector receiptId="receipt:clusters" onCreateEntityTable={create} />
    </WorkspaceStoresContext.Provider>);
    await screen.findByRole('button', { name: 'Create entity table' });
    vi.mocked(stores.projectApi.getReceipt).mockRejectedValueOnce(new Error('Receipt not found.'));
    view.rerender(<WorkspaceStoresContext.Provider value={stores}>
      <ReceiptInspector receiptId="receipt:missing" onCreateEntityTable={create} />
    </WorkspaceStoresContext.Provider>);
    expect(screen.queryByRole('button', { name: 'Create entity table' })).not.toBeInTheDocument();
    expect(await screen.findByTestId('receipt-error')).toHaveTextContent('Receipt not found');
    expect(create).not.toHaveBeenCalled();
  });

  it.each([
    { status: 'failed', actionKind: 'cluster.values' },
    { status: 'completed', actionKind: 'map.template' },
  ])('does not offer history creation for $status $actionKind', async (overrides) => {
    vi.mocked(stores.projectApi.getReceipt).mockResolvedValue(receipt('receipt:other', overrides));
    render(<Workflow history />);
    await screen.findByText(overrides.actionKind);
    expect(screen.queryByRole('button', { name: 'Create entity table' })).not.toBeInTheDocument();
  });
});
