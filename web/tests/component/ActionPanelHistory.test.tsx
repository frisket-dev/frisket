// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { GeneratedActionDraft } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';

const catalog = servedActionCatalog();
const sheet = sheetMeta([columnDef({ id: '1', name: 'url', type: 'link' }),
  columnDef({ id: '2', name: 'story', type: 'text' })], { id: '7', name: 'Sources', rowCount: 3 });
let stores: WorkspaceStores;
afterEach(() => { cleanup(); stores?.dispose(); vi.restoreAllMocks(); });
function setup() {
  stores = createWorkspaceStores('history-panel');
  stores.actionCatalog.store.set(() => ({ status: 'ready', error: null, catalog,
    resolvedTemplates: actionTemplatesFromCatalog(catalog), version: 1 }));
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockResolvedValue({ diagnostics: {}, logical_outputs: [] });
}

describe('ordinary action history', () => {
  it.each(['map.extract', 'media.fetch_url'])('shows the clock in a fresh %s drawer', async actionId => {
    setup();
    const list = vi.spyOn(stores.projectApi, 'getActionHistory').mockResolvedValue({ items: [], total: 0, offset: 0, limit: 20 });
    const execute = vi.fn();
    render(<WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={sheet} running={false} routeActionKind={actionId} onNavigateToAction={vi.fn()}
        onRun={vi.fn()} onExecuteRegisteredAction={execute} />
    </WorkspaceStoresContext.Provider>);
    fireEvent.click(await screen.findByTestId('action-history-button'));
    expect(await screen.findByText('No earlier runs.')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(actionId, 0, 20);
    expect(execute).not.toHaveBeenCalled();
  });

  it('waits for the saved source sheet, then keeps empty exact scope from widening', async () => {
    setup();
    const actionId = 'media.fetch_url';
    const draft: GeneratedActionDraft = { action_id: actionId,
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [] },
      params: { source: 'url' }, output_names: { media: 'downloaded' } };
    const execute = vi.fn();
    const panel = (sourceSheet: typeof sheet) => <WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={sourceSheet} running={false} routeActionKind={actionId}
        routeActionInitial={{ actionDraft: draft }} routeActionLaunchId={1}
        selectedRowIds={['99']} onNavigateToAction={vi.fn()} onRun={vi.fn()} onExecuteRegisteredAction={execute} />
    </WorkspaceStoresContext.Provider>;
    const view = render(panel({ ...sheet, id: '8' }));
    expect(await screen.findByText('Loading source sheet…')).toBeInTheDocument();
    expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
    view.rerender(panel(sheet));
    expect(await screen.findByTestId('field-source')).toHaveValue('url');
    expect(screen.getByTestId('field-output-media')).toHaveValue('downloaded');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    expect(execute).not.toHaveBeenCalled();
  });
});
