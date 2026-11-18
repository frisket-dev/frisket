// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const promoteDocumentPriority = vi.hoisted(() => vi.fn());
vi.mock('../../src/api/documentPriority', () => ({ promoteDocumentPriority }));

import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import type { ImportPageReading } from '../../src/components/useImportProgress';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { useWorkspaceNavigationController } from '../../src/workspace/useWorkspaceNavigationController';
import { columnDef } from '../support/domainFixtures';
import type { ProjectInfo, SheetMeta } from '../../src/api/types';

const projectId = 'document-priority-correlation';
const project: ProjectInfo = { id: projectId, name: 'Priority' };
const sheet: SheetMeta = {
  id: '4', name: 'PDFs', rowCount: 2,
  columns: [columnDef({ id: '3', name: 'File', type: 'text' })],
  annotatedTextColumnIds: [],
  citedColumnIds: [],
  documentTextColumnIds: [],
};

function reading(importRef: string): ImportPageReading {
  return { importRef, sheetId: sheet.id, readingRowIds: [], waitingRowIds: ['7'] };
}

function renderController(initial: ImportPageReading | null) {
  const stores = createWorkspaceStores(projectId, createProjectApi(projectId));
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>
  );
  const view = renderHook(({ importPageReading }: { importPageReading: ImportPageReading | null }) => (
    useWorkspaceNavigationController({
      project,
      sheet,
      sheets: [sheet],
      externalRoute: {},
      chromeState: {
        openEvidenceViewer: vi.fn(), setOpenSplit: vi.fn(), setDocumentView: vi.fn(), documentView: null,
        openSplit: null, hideActionPanel: vi.fn(), recordCommandAction: vi.fn(), actionPanelOpen: false,
        commandPaletteOpen: false,
      },
      visibility: { isContributionHidden: () => false, hideContribution: vi.fn(), revealContribution: vi.fn() },
      grid: {
        activeChildFilter: null, activeGridFilter: null, activeGridSort: null, scopeRowIds: null,
        visibleHeaderMenu: null,
      },
      mapContributionId: null,
      actActionTemplates: [],
      importPageReading,
      showError: vi.fn(),
    })
  ), { wrapper, initialProps: { importPageReading: initial } });
  return { ...view, dispose: () => stores.dispose() };
}

afterEach(() => {
  cleanup();
  promoteDocumentPriority.mockReset();
});

describe('document priority correlation', () => {
  it('shows the opened document first only while its import still reads or waits', async () => {
    promoteDocumentPriority.mockResolvedValue({ promoted: true, importRef: 'import-a' });
    const view = renderController(reading('import-a'));

    act(() => view.result.current.mainView.prioritizeDocumentRow('7', '3'));
    await waitFor(() => expect(view.result.current.mainView.documentPriority).toMatchObject({
      sheetId: '4', rowId: '7', columnId: '3', importRef: 'import-a',
    }));
    expect(promoteDocumentPriority).toHaveBeenCalledWith(projectId, { sheetId: 4, rowId: 7, columnId: 3 });

    view.rerender({ importPageReading: reading('import-b') });
    expect(view.result.current.mainView.documentPriority).toBeNull();
    view.rerender({ importPageReading: null });
    expect(view.result.current.mainView.documentPriority).toBeNull();
    view.dispose();
  });

  it('ignores a promotion that names no import', async () => {
    promoteDocumentPriority.mockResolvedValue({ promoted: true, importRef: null });
    const view = renderController(reading('import-a'));

    act(() => view.result.current.mainView.prioritizeDocumentRow('7', '3'));
    await waitFor(() => expect(promoteDocumentPriority).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    expect(view.result.current.mainView.documentPriority).toBeNull();
    view.dispose();
  });
});
