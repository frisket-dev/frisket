// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';

const mocks = vi.hoisted(() => ({
  reader: vi.fn(),
  setState: vi.fn(),
}));

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
    setSearch: vi.fn(),
    optionsOpen: false,
    setOptionsOpen: vi.fn(),
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
    recordPageCount: vi.fn(),
    selectDocument: vi.fn(),
    onListKeyDown: vi.fn(),
    onListScroll: vi.fn(),
    startIndex: 0,
    windowRows: [],
    loadMore: vi.fn(),
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
    setPreference: vi.fn(),
  }),
}));

import { DocumentView } from '../../src/workbench/DocumentView';

const sheet = {
  id: 'sheet-1',
  name: 'Pages',
  rowCount: 1,
  columns: [sourceColumn],
  citedColumnIds: [],
  annotatedTextColumnIds: [],
} as SheetMeta;

const state = {
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('DocumentView PDF page rows', () => {
  it('opens the original PDF at the page selected by the file value', () => {
    render(
      <DocumentView
        projectId="p"
        sheet={sheet}
        state={state}
        onChangeState={vi.fn()}
        onDocumentFocus={vi.fn()}
        onOpenDetail={vi.fn()}
        queryDocuments={vi.fn()}
        hydrateRow={vi.fn()}
        onListSort={vi.fn()}
        orderKey="none"
        listSortDir={null}
        annotatedTextColumnIds={[]}
        disabledToggleKeys={[]}
        onSetDisabledToggleKeys={vi.fn()}
      />,
    );

    expect(mocks.reader.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      initialPage: 7,
      mediaKind: 'pdf',
    }));
  });
});
