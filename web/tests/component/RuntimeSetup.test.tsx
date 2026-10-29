// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ActionParamResolution, ModelPullDto } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { createProjectApi } from '../../src/api/real';
import { ModelSetupRequestError } from '../../src/api/engineSetup';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { RuntimeSetupNotice } from '../../src/components/RuntimeSetupNotice';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { RuntimeSetupGate } from '../../src/components/action-panel/RuntimeSetupGate';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

const api = vi.hoisted(() => ({ start: vi.fn(), read: vi.fn(), cancel: vi.fn() }));
vi.mock('../../src/api/engineSetup', async (original) => ({ ...await original<typeof import('../../src/api/engineSetup')>(), startEngineSetup: api.start }));
vi.mock('../../src/api/modelPullOperations', () => ({ readModelPull: api.read, cancelScopedModelPull: api.cancel }));
const setup: NonNullable<ActionParamResolution['runtime_setup']> = {
  kind: 'engine_setup', scope: 'workspace', setup_ref: 'engine-setup:chromium.local@1',
  can_start: true, can_mutate: true, blocked_by_operation: null,
};
function pull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return { schemaVersion: 'frisket.model_pull.v4', id: 7, operation_kind: 'engine_setup',
    display_name: 'Browser tools (Chromium)', model: setup.setup_ref, status: 'running',
    capabilities: { cancel: true, retry: false, remove: false },
    endpoint_id: null, endpoint_origin: null, cancel_requested: false, error: null,
    created_at: '2026-10-29T00:00:00Z', started_at: null, finished_at: null,
    initiated_by: null, phase: 'provisioning', completed_bytes: 0, total_bytes: null,
    resolved_digest: null, resolved_size: null, artifact: null, ...overrides };
}
let stores: WorkspaceStores | null = null;
afterEach(() => { cleanup(); stores?.dispose(); stores = null; vi.clearAllMocks(); });
function workspace() {
  stores = createWorkspaceStores('browser-project', createProjectApi('browser-project'));
  return stores;
}
describe('browser runtime setup', () => {
  it('preserves a screenshot draft, blocks launch, and revalidates completion without running', async () => {
    const state = workspace();
    const catalog = servedActionCatalog();
    const entry = catalog.actions.find((item) => item.kind === 'web.capture_screenshot')!;
    const template = actionTemplatesFromCatalog(catalog).find((item) => item.kind === entry.kind)!;
    const onExecute = vi.fn();
    let ready = false;
    const resolveParams = vi.fn(async () => ({ diagnostics: {},
      logical_outputs: [{ key: 'screenshot', column_type: 'file' }], runtime_setup: ready ? null : setup }));
    api.start.mockResolvedValue({ pull: pull(), deduplicated: false });
    render(<WorkspaceStoresContext.Provider value={state}>
      <GeneratedActionForm catalogEntry={entry as import('../../src/api/types').GeneratedActionCatalogEntry}
        actionTemplate={template} sheet={sheetMeta([columnDef({ id: '11', name: 'source', type: 'link' })], { id: '7' })}
        initialDraft={{ action_id: entry.kind, scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: { source: 'source', viewport_width: 1440 }, output_names: { screenshot: 'Saved image' } }}
        running={false} resolveParams={resolveParams} onExecute={onExecute} onClose={() => {}} />
    </WorkspaceStoresContext.Provider>);
    const start = await screen.findByRole('button', { name: 'Set up browser tools' });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(api.start).not.toHaveBeenCalled();
    fireEvent.click(start);
    await waitFor(() => expect(state.runtimeSetup.store.get().pull?.id).toBe(7));
    expect(api.start).toHaveBeenCalledWith('workspace', setup.setup_ref);
    ready = true;
    act(() => state.runtimeSetup.update(pull({ status: 'done' })));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('field-viewport_width')).toHaveValue('1440');
    expect(screen.getByTestId('field-output-screenshot')).toHaveValue('Saved image');
    expect(onExecute).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledOnce();
    expect(onExecute.mock.calls[0][0]).toMatchObject({ params: { source: 'source', viewport_width: 1440 },
      output_names: { screenshot: 'Saved image' } });
  });

  it('recovers a busy operation and keeps polling in the notice after the action closes', async () => {
    const state = workspace();
    api.start.mockRejectedValue(new ModelSetupRequestError(409, 7));
    api.read.mockResolvedValueOnce(pull()).mockResolvedValue(pull({ status: 'done' }));
    const view = (open: boolean) => <WorkspaceStoresContext.Provider value={state}>
      {open && <RuntimeSetupGate setup={setup} onChanged={() => {}} />}
      <RuntimeSetupNotice />
    </WorkspaceStoresContext.Provider>;
    const { rerender } = render(view(true));
    fireEvent.click(screen.getByRole('button', { name: 'Set up browser tools' }));
    await screen.findByTestId('model-pull-progress');
    expect(screen.getByText('provisioning')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
    expect(document.querySelector('.model-pull-progress-size')).not.toBeInTheDocument();
    rerender(view(false));
    await waitFor(() => expect(screen.getByText('Setup complete')).toBeInTheDocument(), { timeout: 2500 });
    expect(state.runtimeSetup.store.get().completionVersion).toBe(1);
    expect(api.start).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss activity notification' }));
    expect(screen.queryByTestId('model-pull-progress')).not.toBeInTheDocument();
    expect(state.runtimeSetup.store.get().completionVersion).toBe(1);
  });
  it('shows dependency instructions without offering a browser download', () => {
    render(<RuntimeSetupGate setup={{ kind: 'instructions', title: 'Install browser support',
      steps: ['Install the browser extra and restart Frisket.'], url: null }} onChanged={() => {}} />);
    expect(screen.getByText('Install the browser extra and restart Frisket.')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Set up browser tools' })).not.toBeInTheDocument();
    expect(api.start).not.toHaveBeenCalled();
  });

  it('cancels through the shared operation while retaining its terminal receipt', async () => {
    const state = workspace();
    state.runtimeSetup.observe(pull(), 'workspace', true);
    api.cancel.mockResolvedValue(pull({ status: 'cancelled', cancel_requested: true }));
    render(<WorkspaceStoresContext.Provider value={state}><RuntimeSetupNotice /></WorkspaceStoresContext.Provider>);
    fireEvent.click(screen.getByTestId('model-pull-cancel'));
    await screen.findByText('Setup cancelled');
    expect(api.cancel).toHaveBeenCalledWith('workspace', 7);
    expect(state.runtimeSetup.store.get().pull?.status).toBe('cancelled');
    expect(state.runtimeSetup.store.get().completionVersion).toBe(1);
  });

});
