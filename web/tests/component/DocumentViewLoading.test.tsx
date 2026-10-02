// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => {}, TextLayer: class {} },
}));
vi.mock('../../src/components/RowDrawer', () => ({
  FieldValue: ({ value }: { value: unknown }) => <>{String(value ?? '')}</>,
  fieldValueDependencyColumnIds: () => [],
}));

import type { SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';
import { DocumentView } from '../../src/workbench/DocumentView';

vi.mock('../../src/workspace/useWorkspaceChromeState', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/workspace/useWorkspaceChromeState')>(),
  useDocumentAlongsidePreference: () => ({ preference: { open: true, columnId: 'file' }, setPreference: vi.fn() }),
}));

afterEach(cleanup);

const sheet: SheetMeta = {
  id: 'sheet-1',
  name: 'Filings',
  rowCount: 1,
  columns: [
    { id: 'file', name: 'file', type: 'file', ai_generated: false },
  ],
  citedColumnIds: [],
  annotatedTextColumnIds: [],
};

const state: DocumentViewState = {
  sheetId: sheet.id,
  sourceColumnId: null,
  titleColumnId: null,
  layout: 'continuous',
  fit: 'width',
  videoFit: 'full',
  textLayer: true,
  sync: false,
  activeRowId: null,
};

describe('DocumentView loading state', () => {
  it('does not render an empty reader while the initial row page is pending', async () => {
    let resolveRows!: (page: { items: []; nextCursor: null; previousCursor: null }) => void;
    const queryDocuments = () => new Promise<{ items: []; nextCursor: null; previousCursor: null }>((resolve) => {
      resolveRows = resolve;
    });

    render(
      <DocumentView
        projectId="project-1"
        sheet={sheet}
        state={state}
        onChangeState={() => undefined}
        onDocumentFocus={() => undefined}
        onOpenDetail={() => undefined}
        queryDocuments={queryDocuments}
        hydrateRow={async () => null}
        onListSort={() => undefined}
        orderKey="none"
        listSortDir={null}
        annotatedTextColumnIds={[]}
        disabledToggleKeys={[]}
        onSetDisabledToggleKeys={() => undefined}
      />,
    );

    expect(screen.getByTestId('document-view-loading')).toHaveTextContent('Loading documents…');
    expect(screen.queryByTestId('document-empty')).toBeNull();
    expect(screen.queryByTestId('document-alongside-pane')).toBeNull();

    await act(async () => {
      resolveRows({ items: [], nextCursor: null, previousCursor: null });
    });
    await waitFor(() => expect(screen.queryByTestId('document-view-loading')).toBeNull());
    expect(screen.getByTestId('document-empty')).toBeVisible();
    expect(screen.getByTestId('document-alongside-pane')).toHaveTextContent('Choose a document to view its value.');
  });

  it('searches on the server and hydrates only the active columns', async () => {
    const queryDocuments = vi.fn(async ({ query }: { query: string }) => ({
      items: query === 'needle' ? [{
        rowId: '7', ordinal: 3, title: 'Needle result', titleTruncated: false,
        sourceKind: 'pdf' as const, sourceLabel: 'brief.pdf', sourceLabelTruncated: false,
        sourcePresent: true,
        characterCount: null,
      }] : [],
      nextCursor: null,
      previousCursor: null,
    }));
    const hydrateRow = vi.fn(async () => ({ id: '7', index: 2, cells: { file: null }, provenance: {} }));

    render(
      <DocumentView
        projectId="project-1" sheet={sheet} state={state}
        onChangeState={() => undefined} onDocumentFocus={() => undefined} onOpenDetail={() => undefined}
        queryDocuments={queryDocuments} hydrateRow={hydrateRow}
        onListSort={() => undefined} orderKey="none" listSortDir={null}
        annotatedTextColumnIds={[]} disabledToggleKeys={[]} onSetDisabledToggleKeys={() => undefined}
      />,
    );

    await waitFor(() => expect(screen.getByTestId('document-list-empty')).toBeVisible());
    const input = screen.getByTestId('document-list-search');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'needle' } });
      await new Promise((resolve) => window.setTimeout(resolve, 220));
    });
    await waitFor(() => expect(queryDocuments).toHaveBeenLastCalledWith(expect.objectContaining({ query: 'needle' })));
    await waitFor(() => expect(screen.getAllByText('Needle result')).toHaveLength(2));
    expect(screen.getByText('1 loaded')).toBeVisible();
    await waitFor(() => expect(hydrateRow).toHaveBeenCalledWith('7', ['file']));
  });

  it('labels absent media and empty annotated text without claiming content', async () => {
    render(
      <DocumentView
        projectId="project-1" sheet={sheet} state={state}
        onChangeState={() => undefined} onDocumentFocus={() => undefined} onOpenDetail={() => undefined}
        queryDocuments={async () => ({
          items: [
            { rowId: '1', ordinal: 1, title: 'Missing media', titleTruncated: false,
              sourceKind: 'pdf', sourcePresent: false, sourceLabel: null,
              sourceLabelTruncated: false, characterCount: null },
            { rowId: '2', ordinal: 2, title: 'Empty text', titleTruncated: false,
              sourceKind: 'annotated_text', sourcePresent: false, sourceLabel: null,
              sourceLabelTruncated: false, characterCount: null },
            { rowId: '3', ordinal: 3, title: 'Remote image', titleTruncated: false,
              sourceKind: 'image', sourcePresent: true,
              sourceLabel: 'https://picsum.photos/seed/example/240/160',
              sourceLabelTruncated: false, characterCount: null },
          ],
          nextCursor: null, previousCursor: null,
        })}
        hydrateRow={async () => null} onListSort={() => undefined} orderKey="none"
        listSortDir={null} annotatedTextColumnIds={[]} disabledToggleKeys={[]}
        onSetDisabledToggleKeys={() => undefined}
      />,
    );
    await waitFor(() => expect(screen.getAllByText('No document').length).toBeGreaterThanOrEqual(2));
    expect(screen.getByText('Empty')).toBeVisible();
    expect(screen.getByText('IMAGE')).toBeVisible();
    expect(screen.queryByText('160')).toBeNull();
    expect(screen.getAllByTestId('document-list-item').slice(0, 2)
      .every((node) => node.dataset.hasMedia === 'false')).toBe(true);
  });

  it('shows active media hydration progress instead of an empty-document flash', async () => {
    let resolveHydration!: (row: Row | null) => void;
    const hydration = new Promise<Row | null>((resolve) => { resolveHydration = resolve; });
    render(
      <DocumentView
        projectId="project-1" sheet={sheet} state={state}
        onChangeState={() => undefined} onDocumentFocus={() => undefined} onOpenDetail={() => undefined}
        queryDocuments={async () => ({ items: [{ rowId: '4', ordinal: 4, title: 'Loading media',
          titleTruncated: false, sourceKind: 'audio', sourcePresent: true, sourceLabel: 'clip.mp3',
          sourceLabelTruncated: false, characterCount: null }], nextCursor: null, previousCursor: null })}
        hydrateRow={() => hydration} onListSort={() => undefined} orderKey="none"
        listSortDir={null} annotatedTextColumnIds={[]} disabledToggleKeys={[]}
        onSetDisabledToggleKeys={() => undefined}
      />,
    );
    await screen.findByText('Loading document…');
    expect(screen.queryByText('No document')).toBeNull();
    await act(async () => { resolveHydration(null); });
  });

  it('surfaces active media hydration failures', async () => {
    render(
      <DocumentView
        projectId="project-1" sheet={sheet} state={state}
        onChangeState={() => undefined} onDocumentFocus={() => undefined} onOpenDetail={() => undefined}
        queryDocuments={async () => ({ items: [{ rowId: '5', ordinal: 5, title: 'Broken media',
          titleTruncated: false, sourceKind: 'video', sourcePresent: true, sourceLabel: 'clip.mp4',
          sourceLabelTruncated: false, characterCount: null }], nextCursor: null, previousCursor: null })}
        hydrateRow={async () => { throw new Error('Could not hydrate media.'); }}
        onListSort={() => undefined} orderKey="none" listSortDir={null}
        annotatedTextColumnIds={[]} disabledToggleKeys={[]} onSetDisabledToggleKeys={() => undefined}
      />,
    );
    expect((await screen.findAllByRole('alert'))[0]).toHaveTextContent('Could not hydrate media.');
    expect(screen.queryByText('No document')).toBeNull();
  });
});
