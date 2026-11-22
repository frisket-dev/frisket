// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';

const mocks = vi.hoisted(() => ({
  reader: vi.fn(),
  setState: vi.fn(),
}));

const noop = () => {};
const sourceColumn = { id: 'file', name: 'Source', type: 'file' };

vi.mock('../../src/workbench/useDocumentView', () => ({
  useDocumentView: () => ({
    sources: [{ kind: 'media', column: sourceColumn }],
    source: { kind: 'media', column: sourceColumn },
    sourceColumn,
    defaultTitleColumn: sourceColumn,
    titleColumn: sourceColumn,
    list: { pages: [], loading: false, error: null },
    pageCounts: {},
    search: '',
    setSearch: noop,
    optionsOpen: false,
    setOptionsOpen: noop,
    listBodyRef: { current: null },
    items: [],
    activeRowId: 'row-1',
    activeItem: { title: 'Page seven' },
    activeRow: {
      id: 'row-1',
      index: 0,
      cells: {
        file: JSON.stringify({
          blob: 'pdf-hash',
          filename: 'filing.pdf',
          mime: 'application/pdf',
          page: 7,
        }),
      },
      provenance: {},
    },
    activeMedia: { url: '/api/projects/p/blobs/pdf-hash', label: 'filing.pdf' },
    activeMediaKind: 'pdf',
    hydrationLoading: false,
    hydrationError: null,
    recordPageCount: noop,
    selectDocument: noop,
    onListKeyDown: noop,
    onListScroll: noop,
    startIndex: 0,
    windowRows: [],
    loadMore: noop,
    setState: mocks.setState,
  }),
}));

vi.mock('../../src/workbench/DocumentReader', () => ({
  DocumentReader: (props: unknown) => {
    mocks.reader(props);
    return <div data-testid="document-reader" />;
  },
}));

vi.mock('../../src/workspace/useWorkspaceChromeState', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/workspace/useWorkspaceChromeState')>(),
  useDocumentAlongsidePreference: () => ({
    preference: { open: false, columnId: null },
    setPreference: noop,
  }),
}));

import { DocumentView } from '../../src/workbench/DocumentView';
import { sheetMeta } from '../support/actionFormFixtures';

const sheet = sheetMeta([sourceColumn], {
  id: 'sheet-1',
  name: 'Pages',
  rowCount: 1,
});

const baseState = {
  sheetId: sheet.id,
  sourceColumnId: sourceColumn.id,
  titleColumnId: null,
  layout: 'continuous',
  fit: 'width',
  videoFit: 'full',
  textLayer: true,
  sync: false,
  activeRowId: 'row-1',
} as DocumentViewState;

function renderDocumentView(stateOverrides: Partial<DocumentViewState> = {}) {
  return render(
    <DocumentView
      projectId="p"
      sheet={sheet}
      state={{ ...baseState, ...stateOverrides }}
      onChangeState={noop}
      onDocumentFocus={noop}
      onOpenDetail={noop}
      queryDocuments={async () => ({ items: [], nextCursor: null, previousCursor: null })}
      hydrateRow={async () => null}
      onListSort={noop}
      orderKey="none"
      listSortDir={null}
      annotatedTextColumnIds={[]}
      disabledToggleKeys={[]}
      onSetDisabledToggleKeys={noop}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('DocumentView PDF page rows', () => {
  it('opens the original PDF at the file-value page and forwards media metadata', () => {
    renderDocumentView();

    expect(mocks.reader).toHaveBeenCalledTimes(1);
    expect(mocks.reader.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      initialPage: 7,
      mediaKind: 'pdf',
      title: 'Page seven',
      media: { url: '/api/projects/p/blobs/pdf-hash', label: 'filing.pdf' },
    }));
  });

  it('prefers an explicit readerPage for the active row and ignores another row readerPage', () => {
    const { unmount } = renderDocumentView({ readerPage: { rowId: 'row-1', page: 12 } });
    expect(mocks.reader.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      initialPage: 12,
      mediaKind: 'pdf',
    }));
    unmount();

    mocks.reader.mockClear();
    renderDocumentView({ readerPage: { rowId: 'row-other', page: 99 } });
    expect(mocks.reader.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      initialPage: 7,
      mediaKind: 'pdf',
    }));
  });
});
