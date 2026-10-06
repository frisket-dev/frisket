// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { CSSProperties, ReactNode } from 'react';
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
vi.mock('../../src/workbench/useDocumentView', async () => {
  const { useState } = await import('react');
  return { useDocumentView: () => {
    const [optionsOpen, setOptionsOpen] = useState(false);
    return { ...mocks.browse, optionsOpen, setOptionsOpen };
  } };
});
vi.mock('../../src/workbench/DocumentReader', () => ({ DocumentReader: (props: {
  optionsOpen: boolean; onToggleOptions(): void;
  optionsPopover(args: { popoverRef(el: HTMLDivElement | null): void; style: CSSProperties }): ReactNode;
}) => {
  mocks.reader(props);
  return <><button onClick={props.onToggleOptions}>View options</button>
    {props.optionsOpen && props.optionsPopover({ popoverRef: () => {}, style: {} })}</>;
} }));
vi.mock('../../src/api/documentExtraction', () => ({ documentExtractionApi: { document: mocks.document, templates: mocks.templates } }));
import { ExtractView } from '../../src/workbench/extract/ExtractView';

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it('renders octet-stream .pdf sources with the same server geometry used for annotation', async () => {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  mocks.templates.mockResolvedValue({ templates: [] });
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', filename: 'cropped.pdf', mime: 'application/octet-stream',
    document: { source_fingerprint: 'native:example', pages: [{ page: 1, width: 800, height: 1000, tokens: [] }] } });
  render(<ExtractView projectId="p" sheet={{ id: '1', columns: [mocks.browse.sourceColumn] } as SheetMeta}
    state={{ sheetId: '1', sourceColumnId: '1', activeRowId: '1' } as DocumentViewState} onChangeState={vi.fn()}
    onDocumentFocus={vi.fn()} queryDocuments={vi.fn()} hydrateRow={vi.fn()} orderKey="" />);
  await waitFor(() => expect(mocks.reader.mock.lastCall?.[0].pageImages).toEqual([
    { page: 1, width: 800, height: 1000, url: '/api/projects/p/blobs/example/pages/1/image' },
  ]));
  const props = mocks.reader.mock.lastCall![0];
  expect(props.mediaKind).toBe('pdf');
  expect(props.renderPageOverlay(1)).not.toBeNull();
});

it('keeps tools in the workbench host and moves secondary settings into the reader options', async () => {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  mocks.templates.mockResolvedValue({ templates: [] });
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', filename: 'cropped.pdf', mime: 'application/pdf',
    document: { source_fingerprint: 'native:example', pages: [{ page: 1, width: 800, height: 1000, tokens: [] }] } });
  render(<><div id="tools-host" data-testid="tools-host" /><ExtractView projectId="p" sheet={{ id: '1', columns: [mocks.browse.sourceColumn] } as SheetMeta}
    state={{ sheetId: '1', sourceColumnId: '1', activeRowId: '1' } as DocumentViewState} onChangeState={vi.fn()}
    onDocumentFocus={vi.fn()} queryDocuments={vi.fn()} hydrateRow={vi.fn()} orderKey="" toolbarTargetId="tools-host" /></>);
  const host = screen.getByTestId('tools-host');
  await waitFor(() => expect(within(host).getByRole('button', { name: 'Key / value' })).toBeEnabled());
  expect(within(host).getByRole('group', { name: 'Annotation tools' })).toHaveClass('segmented-toolbar');
  expect(within(host).getByLabelText('Result sheet name')).toHaveValue('Extracted data');
  expect(within(host).getByRole('button', { name: 'Preview on 12 documents' })).toHaveTextContent('Preview');
  expect(screen.queryByLabelText('Expand value areas')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'View options' }));
  expect(screen.getByLabelText('Look on every page')).toBeChecked();
  expect(screen.getByLabelText('Continue across pages')).not.toBeChecked();
  expect(screen.getByLabelText('Result rows')).toHaveValue('');
  fireEvent.click(screen.getByLabelText('Expand value areas'));
  fireEvent.click(screen.getByLabelText('Continue across pages'));
  fireEvent.click(screen.getByRole('button', { name: 'View options' }));
  fireEvent.click(screen.getByRole('button', { name: 'View options' }));
  expect(screen.getByLabelText('Expand value areas')).toBeChecked();
  expect(screen.getByLabelText('Continue across pages')).toBeChecked();
  expect(screen.getByText(/Expanded areas follow matched field boundaries/)).toBeInTheDocument();
});
