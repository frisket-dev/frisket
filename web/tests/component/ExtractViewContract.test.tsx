// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';

const mocks = vi.hoisted(() => {
  const sourceColumn = { id: '1', name: 'Document', type: 'file' };
  return {
    reader: vi.fn(), document: vi.fn(), templates: vi.fn(),
    browse: { sourceColumn, activeRowId: '1', activeMedia: { url: '/blob/example', label: 'cropped.pdf', filename: 'cropped.pdf', mime: 'application/octet-stream' },
      activeItem: { title: 'cropped.pdf' }, sources: [{ kind: 'media', column: sourceColumn }], search: '', setSearch: vi.fn(), list: { pages: [], loading: false, error: null },
      items: [], listBodyRef: { current: null }, onListScroll: vi.fn(), onListKeyDown: vi.fn(), windowRows: [], startIndex: 0,
      loadMore: vi.fn(), selectDocument: vi.fn(), recordPageCount: vi.fn() },
  };
});
vi.mock('../../src/workbench/useDocumentView', () => ({ useDocumentView: () => mocks.browse }));
vi.mock('../../src/workbench/DocumentReader', () => ({ DocumentReader: (props: unknown) => { mocks.reader(props); return null; } }));
vi.mock('../../src/api/documentExtraction', () => ({ documentExtractionApi: { document: mocks.document, templates: mocks.templates } }));
import { ExtractView } from '../../src/workbench/extract/ExtractView';

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it('renders octet-stream .pdf sources with the same server geometry used for annotation', async () => {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  mocks.templates.mockResolvedValue({ templates: [] });
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', reference_page: 2, filename: 'cropped.pdf', mime: 'application/octet-stream',
    document: { source_fingerprint: 'native:example:page:2', pages: [{ page: 2, width: 800, height: 1000, tokens: [] }] } });
  render(<ExtractView projectId="p" sheet={{ id: '1', columns: [mocks.browse.sourceColumn] } as SheetMeta}
    state={{ sheetId: '1', sourceColumnId: '1', activeRowId: '1' } as DocumentViewState} onChangeState={vi.fn()}
    onDocumentFocus={vi.fn()} queryDocuments={vi.fn()} hydrateRow={vi.fn()} orderKey="" />);
  await waitFor(() => expect(mocks.reader.mock.lastCall?.[0].pageImages).toEqual([
    { page: 2, width: 800, height: 1000, url: '/api/projects/p/blobs/example/pages/2/image' },
  ]));
  const props = mocks.reader.mock.lastCall![0];
  expect(props.mediaKind).toBe('pdf');
  expect(props.initialPage).toBe(2);
  expect(props.renderPageOverlay(2)).not.toBeNull();
});
