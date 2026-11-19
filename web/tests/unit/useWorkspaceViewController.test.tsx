// @vitest-environment jsdom

import type { ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { useWorkspaceViewController } from '../../src/workspace/useWorkspaceViewController';
import { projectApiWith } from '../support/workspaceTestHarness';
import { sheetMeta } from '../support/actionFormFixtures';

const stores = createWorkspaceStores('view-switch-project', projectApiWith({}, 'view-switch-project'));

afterEach(() => {
  stores.selection.clearRowSelection('sheet-1');
});

const sheet = sheetMeta(
  [
    { id: 'title', name: 'Title', type: 'text' },
    { id: 'answer', name: 'Answer', type: 'text' },
  ],
  { citedColumnIds: ['answer'] },
);

function renderController() {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>
  );
  return renderHook(
    () =>
      useWorkspaceViewController({
        sheet,
        chromeState: {
          openSplit: null,
          documentView: null,
          promotedViews: [],
          setOpenSplit: vi.fn(),
          setDocumentView: vi.fn(),
          setPromotedViews: vi.fn(),
        },
        visibility: {
          hiddenContributionIdSet: new Set<string>(),
          isContributionHidden: () => false,
        },
        mapViewDescriptor: null,
        selectSheet: vi.fn(),
        openMapPanel: vi.fn(),
        openGraphPanel: vi.fn(),
      }),
    { wrapper },
  );
}

describe('work view switch', () => {
  it('keeps the row selection when switching to the Answers view and back', () => {
    const selected = { sheetId: 'sheet-1', rowIds: ['r2', 'r5'], rowIndexes: [1, 4] };
    stores.selection.setSelectedRows(selected);
    const { result } = renderController();

    act(() => result.current.setWorkView('answers'));
    expect(stores.selection.store.get().selectedRows).toEqual(selected);

    act(() => result.current.setWorkView('grid'));
    expect(stores.selection.store.get().selectedRows).toEqual(selected);
  });
});
