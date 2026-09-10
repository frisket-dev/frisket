// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { isGeneratedActionCatalogEntry, type GeneratedActionDraft } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';

const catalog = servedActionCatalog();
const templates = actionTemplatesFromCatalog(catalog);
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'url', type: 'link' }),
  columnDef({ id: '2', name: 'video', type: 'video' }),
  columnDef({ id: '3', name: 'image', type: 'image' }),
], { id: '7', rowCount: 6 });
let stores: WorkspaceStores | undefined;
afterEach(() => { cleanup(); stores?.dispose(); stores = undefined; vi.restoreAllMocks(); });

describe('canonical media discovery and saved intent', () => {
  it.each([
    ['media.fetch_url', 'url', 'media'],
    ['web.capture_screenshot', 'url', 'screenshot'],
    ['media.video_frames', 'video', 'frames'],
    ['media.extract_faces', 'image', 'faces'],
  ])('opens and edits %s with authoritative Params and saved exact scope', async (actionId, source, output) => {
    const entry = catalog.actions.find((entry) => entry.kind === actionId)!;
    expect(isGeneratedActionCatalogEntry(entry)).toBe(true);
    expect(templates.filter((template) => template.kind === actionId)).toHaveLength(1);
    const draft: GeneratedActionDraft = { action_id: actionId,
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 13] },
      params: { source }, output_names: { [output]: 'Stored output' } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    stores = createWorkspaceStores('typed-media');
    stores.actionCatalog.store.set(() => ({ status: 'ready', error: null,
      catalog, resolvedTemplates: templates, version: 1 }));
    vi.spyOn(stores.projectApi, 'resolveActionParams').mockResolvedValue({ diagnostics: {},
      logical_outputs: entry.ui_hints.logical_outputs });
    const onExecute = vi.fn();
    const onLegacyRun = vi.fn();
    render(<WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={sheet} running={false} selectedRowIds={['999']} activeRowId="888"
        onRun={onLegacyRun} onExecuteRegisteredAction={onExecute}
        inspectProposal={{ seq: 1, title: 'Saved media action', spec: draft }} />
    </WorkspaceStoresContext.Provider>);
    expect(await screen.findByTestId('field-source')).toHaveValue(source);
    fireEvent.change(screen.getByTestId('field-output-' + output), { target: { value: 'Renamed output' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toEqual({ ...draft, output_names: { [output]: 'Renamed output' },
      idempotency_key: expect.any(String) });
    expect(onLegacyRun).not.toHaveBeenCalled();
  });
});
