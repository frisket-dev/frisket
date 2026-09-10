// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, within, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { ActionCatalogPayload, GeneratedActionDraft, GeneratedActionRequest, ClusterValuesMethod } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createJobStore } from '../../src/state/jobStore';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { createProjectApi } from '../../src/api/real';

const catalog = servedActionCatalog();
const resolvedTemplates = actionTemplatesFromCatalog(catalog);
const SHEET = sheetMeta([columnDef({ id: '11', name: 'name', type: 'text' }),
  columnDef({ id: '12', name: 'notes', type: 'text' })], { id: '7', name: 'People', rowCount: 3 });
const HASH = `sha256:${'a'.repeat(64)}`;
const OLD_HASH = `sha256:${'b'.repeat(64)}`;
const CONFIRMATION = 'c'.repeat(64);
const COMPLETED = { schema_version: 'frisket.action_result.v1', status: 'completed',
  run_id: null, job_id: null, receipt_id: 'receipt-cluster', outputs: [], errors: [] };
const modes = [{ method: 'fingerprint' as const, knob: {} },
  { method: 'ngram_fingerprint' as const, knob: { ngram_size: 3 } },
  { method: 'semantic' as const, knob: { threshold: 0.91 } }];
let api = createProjectApi('cluster-action-panel');
let stores: WorkspaceStores[] = [];
afterEach(() => { cleanup(); stores.forEach((store) => store.dispose()); stores = [];
  vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
function preview(method: ClusterValuesMethod) {
  return { clusters: [{ key: 'jon smith', canonical: 'Jon Smith', size: 3,
    values: [{ value: 'Jon Smith', count: 2 }, { value: 'Smith, Jon', count: 1 }], rowIds: ['1', '2', '3'] }],
  count: 1, valueHash: HASH, method, semantic: method === 'semantic' };
}
function setup(posts: Record<string, unknown>[], gated = false) {
  api = createProjectApi('cluster-action-panel');
  vi.spyOn(api, 'resolveActionParams').mockResolvedValue({ diagnostics: {},
    logical_outputs: [{ key: 'canonical', column_type: 'text' }], creates_sheet: false });
  vi.spyOn(api, 'estimateAction').mockResolvedValue({ rows: 3, cost: 0 } as never);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST' && url === '/api/projects/cluster-action-panel/actions/v1/run') {
      posts.push(JSON.parse(String(init.body)));
      if (gated && posts.length === 1) return response({ ...COMPLETED, status: 'needs_confirmation',
        receipt_id: null, errors: [{ code: 'model_cost_requires_confirmation', message: 'Confirm embedding cost.',
          details: { reason: 'model_cost', estimate: { cost: 0.02, rows: 3 }, promise_set_hash: CONFIRMATION } }] }, 402);
      return response(COMPLETED);
    }
    throw new Error(`Unexpected fetch: ${init?.method ?? 'GET'} ${url}`);
  }));
}
function panel(onRun: (request: GeneratedActionRequest) => void,
  draft?: GeneratedActionDraft, payload: ActionCatalogPayload = catalog,
  routeActionKind = 'cluster.values') {
  const workspace = createWorkspaceStores('cluster-action-panel', api); stores.push(workspace);
  workspace.actionCatalog.store.set(() => ({ status: 'ready', error: null, catalog: payload,
    resolvedTemplates, version: 1 }));
  return render(<WorkspaceStoresContext.Provider value={workspace}>
    <ActionPanel sheet={SHEET} running={false} selectedRowIds={['2']}
      routeActionKind={draft ? undefined : routeActionKind} routeActionLaunchId={1}
      routeActionInitial={{ sourceColumn: 'name' }}
      inspectProposal={draft ? { seq: 41, title: 'Saved clustering', spec: draft } : undefined}
      onRun={vi.fn()} onExecuteRegisteredAction={onRun} />
  </WorkspaceStoresContext.Provider>);
}
async function commit(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByTestId('cluster-commit-button')).toBeEnabled());
  await user.click(screen.getByTestId('cluster-commit-button'));
}

describe('cluster ActionPanel public typed requests', () => {
  it('refuses a retired short route alias even when its canonical action is served', async () => {
    setup([]);
    panel(vi.fn(), undefined, catalog, 'ask');
    expect(await screen.findByText('Action unavailable: ask is not in this catalog.'))
      .toBeInTheDocument();
  });

  it.each(modes)('sends fresh $method review edits through the public API over the whole column', async ({ method }) => {
    const posts: Record<string, unknown>[] = []; setup(posts);
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(preview(method));
    let pending: Promise<unknown> | undefined;
    panel((request) => { pending = api.runAction(request); });
    const user = userEvent.setup();
    await screen.findByTestId('cluster-method-select');
    if (method !== 'fingerprint') await user.selectOptions(screen.getByTestId('cluster-method-select'), method);
    await user.click(screen.getByTestId('cluster-preview-button'));
    await screen.findByTestId('cluster-card');
    await user.clear(screen.getByTestId('cluster-canonical-input'));
    await user.type(screen.getByTestId('cluster-canonical-input'), 'Jonathan Smith');
    await user.click(screen.getAllByTestId('cluster-member-checkbox')[1]);
    await commit(user); await waitFor(() => expect(posts).toHaveLength(1)); await pending;
    expect(posts[0]).toMatchObject({ action_id: 'cluster.values', scope: { kind: 'sheet_rows', sheet_id: 7 },
      output_names: { canonical: 'name_canonical' }, params: { source: 'name', method, min_size: 2,
        review: { source_hash: HASH, canonical_overrides: { 'jon smith': 'Jonathan Smith' },
          excluded_members: { 'jon smith': ['Smith, Jon'] } } } });
    expect(posts[0].scope).not.toHaveProperty('row_ids');
    expect(posts[0]).not.toHaveProperty('capabilities');
    expect(posts[0]).not.toHaveProperty('confirmation');
    expect(posts[0].params).not.toHaveProperty('output_name');
    expect(posts[0].params).not.toHaveProperty('sheet_id');
    expect((posts[0].params as Record<string, unknown>)[method === 'semantic' ? 'ngram_size' : 'threshold'] ?? null).toBeNull();
    expect(posts[0].idempotency_key).toEqual(expect.any(String));
  });

  it.each(modes)('reopens $method, re-previews saved edits and replaces only their source hash', async ({ method, knob }) => {
    const posts: Record<string, unknown>[] = []; setup(posts);
    const previewSpy = vi.spyOn(api, 'clusterPreview').mockResolvedValue(preview(method));
    panel((request) => { void api.runAction(request); }, { action_id: 'cluster.values',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: { canonical: 'notes_clustered' },
      params: { source: 'notes', method, min_size: 3, key_template: '{{value|lower}}', ...knob,
        review: { source_hash: OLD_HASH, canonical_overrides: { 'jon smith': 'Saved Jonathan' },
          excluded_members: { 'jon smith': ['Smith, Jon'] } } } });
    expect(await screen.findByTestId('cluster-column-select')).toHaveValue('notes');
    expect(screen.getByTestId('cluster-method-select')).toHaveValue(method);
    expect(screen.getByTestId('cluster-min-size')).toHaveValue(3);
    expect(screen.getByTestId('cluster-key-template')).toHaveValue('{{value|lower}}');
    expect(await screen.findByTestId('field-output-canonical')).toHaveValue('notes_clustered');
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
    if (method === 'semantic') expect(within(screen.getByTestId('cluster-threshold-field')).getByRole('spinbutton')).toHaveValue(0.91);
    if (method === 'ngram_fingerprint') expect(within(screen.getByTestId('cluster-ngram-size-field')).getByRole('spinbutton')).toHaveValue(3);
    const user = userEvent.setup(); await user.click(screen.getByTestId('cluster-preview-button'));
    await screen.findByTestId('cluster-card');
    expect(previewSpy).toHaveBeenCalledWith(expect.objectContaining({ inputColumn: 'notes', method, minSize: 3,
      keyTemplate: '{{value|lower}}' }));
    expect(screen.getByTestId('cluster-canonical-input')).toHaveValue('Saved Jonathan');
    expect(screen.getAllByTestId('cluster-member-checkbox')[1]).not.toBeChecked();
    await commit(user); await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ action_id: 'cluster.values', output_names: { canonical: 'notes_clustered' },
      params: { source: 'notes', method, min_size: 3, ...knob, review: { source_hash: HASH,
        canonical_overrides: { 'jon smith': 'Saved Jonathan' }, excluded_members: { 'jon smith': ['Smith, Jon'] } } } });
    expect(JSON.stringify(posts[0])).not.toContain(OLD_HASH);
  });

  it.each(['missing', 'duplicated'])('fails closed when the served entry is %s', async (condition) => {
    const raw = catalog.actions.find((item) => item.kind === 'cluster.values')!;
    const actions = condition === 'missing' ? catalog.actions.filter((item) => item !== raw) : [...catalog.actions, raw];
    panel(vi.fn(), undefined, { ...catalog, actions });
    expect(await screen.findByRole('alert')).toHaveTextContent(/cluster.values/i);
    expect(screen.queryByTestId('cluster-preview-button')).toBeNull();
  });

  it('retries 402 with the same key and unchanged review, putting exact consent only on the envelope', async () => {
    const posts: Record<string, unknown>[] = []; setup(posts, true);
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(preview('semantic'));
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const jobs = createJobStore('cluster-action-panel', api);
    const deps = { invalidateProjectData: vi.fn(), refreshHistory: vi.fn(), refreshReviewCount: vi.fn(),
      refreshSheets: vi.fn(), showError: vi.fn() }; jobs.start(deps);
    panel((request) => jobs.startRun(request, SHEET));
    const user = userEvent.setup(); await screen.findByTestId('cluster-method-select');
    await user.selectOptions(screen.getByTestId('cluster-method-select'), 'semantic');
    await user.click(screen.getByTestId('cluster-preview-button')); await screen.findByTestId('cluster-card');
    await commit(user); await waitFor(() => expect(jobs.store.get().costGate).not.toBeNull());
    expect(posts).toHaveLength(1); expect(posts[0]).not.toHaveProperty('confirmation');
    jobs.confirmCostGate(); await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({ ...posts[0], confirmation: CONFIRMATION });
    expect(posts[1].params).not.toHaveProperty('confirmed');
    expect(jobs.store.get().costGate).toBeNull(); expect(deps.showError).not.toHaveBeenCalled(); jobs.dispose();
  });
});
