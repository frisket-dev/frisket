// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';
import type { SavedExtractionTemplate } from '../../src/api/documentExtraction';
import type { HttpExtractionTemplateSave } from '../../src/generated/openHttpContracts';
import type { PageRegion } from '../../src/workbench/extract/types';

const mocks = vi.hoisted(() => {
  const sourceColumn = { id: '1', name: 'Document', type: 'file' };
  return { reader: vi.fn(), document: vi.fn(), templates: vi.fn(), save: vi.fn(), select: vi.fn(), counts: vi.fn(), preview: vi.fn(),
    browse: { sourceColumn, activeRowId: '1', activeMedia: { url: '/blob/example', label: 'cropped.pdf', filename: 'cropped.pdf', mime: 'application/octet-stream' },
      activeItem: { title: 'cropped.pdf' }, sources: [{ kind: 'media', column: sourceColumn }], search: '', setSearch: vi.fn(), list: { pages: [], loading: false, error: null },
      items: [], listBodyRef: { current: null }, onListScroll: vi.fn(), onListKeyDown: vi.fn(), windowRows: [], startIndex: 0,
      loadMore: vi.fn(), selectDocument: vi.fn(), recordPageCount: vi.fn() } };
});
vi.mock('../../src/workbench/useDocumentView', () => ({ useDocumentView: () => mocks.browse }));
vi.mock('../../src/workbench/DocumentReader', () => ({ DocumentReader: (props: { onToggleOptions(): void }) => {
  mocks.reader(props); return <button onClick={props.onToggleOptions}>View options</button>;
} }));
vi.mock('../../src/api/documentExtraction', () => ({ documentExtractionApi: {
  document: mocks.document, templates: mocks.templates, save: mocks.save, select: mocks.select, counts: mocks.counts, preview: mocks.preview,
} }));
import { ExtractView, type ExtractViewProps } from '../../src/workbench/extract/ExtractView';

const region: PageRegion = { page: 1, box: { x0: .1, x1: .2, y0: .1, y1: .15 } };
const emptyDraft = { reference_blob_id: '', reference_page: null, reference_fingerprint: '', fields: [], sections: [], ignore_bands: [], pending: null,
  expand_values: false, look_every_page: true, continue_across_pages: false };
const newLayout = (id: number, draft: SavedExtractionTemplate['draft'] = emptyDraft): SavedExtractionTemplate => ({
  id, name: `Layout ${id}`, sheet_id: 1, source: 'Document', source_column_id: 1, reference_row_id: null,
  repeat_group_id: null, has_applied: false, draft,
});
let saved: SavedExtractionTemplate[];
let selectedId: number;
let projectSequence = 0;
let projectId: string;
function props(overrides: Partial<ExtractViewProps> = {}): ExtractViewProps {
  return { projectId, sheet: { id: '1', columns: [mocks.browse.sourceColumn] } as SheetMeta,
    state: { sheetId: '1', sourceColumnId: '1', activeRowId: '1' } as DocumentViewState,
    onChangeState: vi.fn(), onDocumentFocus: vi.fn(), queryDocuments: vi.fn(), hydrateRow: vi.fn(), orderKey: '', ...overrides };
}
beforeEach(() => {
  projectId = `extraction-test-${++projectSequence}`; saved = [newLayout(1)]; selectedId = 1;
  mocks.templates.mockImplementation(async () => ({ templates: structuredClone(saved), selected_layout_id: selectedId }));
  mocks.save.mockImplementation(async (pid: string, value: HttpExtractionTemplateSave) => {
    const existing = saved.find((item) => item.id === value.id);
    const result = { ...(existing ?? newLayout(saved.length + 1)), ...value, id: existing?.id ?? saved.length + 1 };
    saved = existing ? saved.map((item) => item.id === result.id ? result : item) : [...saved, result];
    return structuredClone(result);
  });
  mocks.select.mockImplementation(async (pid: string, value: { layout_id: number }) => { selectedId = value.layout_id; return saved.find((item) => item.id === selectedId); });
  mocks.counts.mockResolvedValue({ all: 3, filter: 2, layout: 0, this: 1 });
  mocks.preview.mockResolvedValue({ documents: [], truncated: false });
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', filename: 'cropped.pdf', mime: 'application/octet-stream',
    document: { source_fingerprint: 'native:example', pages: [{ page: 1, width: 800, height: 1000, tokens: [{ text: 'NAME', box: region.box, granularity: 'word' }] }] } });
});
afterEach(async () => { cleanup(); await act(async () => {}); vi.clearAllMocks(); vi.unstubAllGlobals(); });
async function ready() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Key / value' })).toBeEnabled());
  await waitFor(() => expect(screen.getByRole('option', { name: 'All (3 documents)' })).toBeInTheDocument());
}

it('renders octet-stream PDFs with server geometry used for annotation', async () => {
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', reference_page: 2, filename: 'cropped.pdf', mime: 'application/octet-stream',
    document: { source_fingerprint: 'native:example:page:2', pages: [{ page: 2, width: 800, height: 1000, tokens: [] }] } });
  render(<ExtractView {...props()} />); await ready();
  const reader = mocks.reader.mock.lastCall![0];
  expect(reader.pageImages).toEqual([{ page: 2, width: 800, height: 1000, url: `/api/projects/${projectId}/blobs/example/pages/2/image` }]);
  expect(reader.mediaKind).toBe('pdf'); expect(reader.initialPage).toBe(2);
  expect(reader.renderPageOverlay(2)).not.toBeNull();
  expect(reader.renderPageOverlay(1)).toBeNull();
  const pageTwoRegion = { ...region, page: 2 };
  act(() => reader.renderPageOverlay(2).props.onDraw(pageTwoRegion));
  await waitFor(() => expect(saved[0].draft.reference_page).toBe(2));
  expect(saved[0].draft.pending).toEqual({ tool: 'key', region: pageTwoRegion });
});

it.each([
  ['another physical page', { reference_page: 1, fingerprint: 'native:example:page:2' }],
  ['newer positioned content', { reference_page: 2, fingerprint: 'native:example:page:2:new' }],
])('keeps a saved layout read-only when its reference row resolves to %s', async (_label, resolved) => {
  const pageTwoRegion = { ...region, page: 2 };
  saved = [{ ...newLayout(1, { ...emptyDraft, reference_blob_id: 'example', reference_page: 2,
    reference_fingerprint: 'native:example:page:2', fields: [{ id: 'name', name: 'Name', key: pageTwoRegion,
      value: { ...pageTwoRegion, box: { ...pageTwoRegion.box, x0: .3, x1: .6 } }, section_id: null }] }), reference_row_id: 1 }];
  mocks.document.mockResolvedValue({ row_id: 1, blob_id: 'example', reference_page: resolved.reference_page,
    filename: 'cropped.pdf', mime: 'application/octet-stream', document: { source_fingerprint: resolved.fingerprint,
      pages: [{ page: resolved.reference_page, width: 800, height: 1000, tokens: [] }] } });
  render(<ExtractView {...props()} />);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('saved reference document has changed or is unavailable'));
  await waitFor(() => expect(mocks.reader).toHaveBeenCalled());
  const reader = mocks.reader.mock.lastCall![0];
  expect(reader.renderPageOverlay(resolved.reference_page).props.muted).toBe(true);
  expect(screen.getByTestId('extract-options-button')).toBeDisabled();
  expect(screen.getByLabelText('Column name for Name')).toBeDisabled();
  expect(screen.getByTestId('extract-preview-button')).toBeDisabled();
  expect(screen.getByTestId('extract-new-sheet')).toBeDisabled();
  expect(screen.getByTestId('extract-layout-selector')).toBeEnabled();
  expect(screen.queryByRole('button', { name: 'Back to example' })).not.toBeInTheDocument();
  expect(saved[0].draft.reference_page).toBe(2);
  expect(saved[0].draft.reference_fingerprint).toBe('native:example:page:2');
  expect(mocks.save).not.toHaveBeenCalled();
});

it('keeps a layout selectable but read-only when its saved reference is unavailable', async () => {
  const pageTwoRegion = { ...region, page: 2 };
  saved = [{ ...newLayout(1, { ...emptyDraft, reference_blob_id: 'missing', reference_page: 2,
    reference_fingerprint: 'native:missing:page:2', fields: [{ id: 'name', name: 'Name', key: pageTwoRegion,
      value: pageTwoRegion, section_id: null }] }), reference_row_id: 1 }];
  mocks.document.mockRejectedValue(new Error('Not found'));
  render(<ExtractView {...props()} />);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('saved reference document is unavailable'));
  expect(screen.getByTestId('extract-options-button')).toBeDisabled();
  expect(screen.getByLabelText('Column name for Name')).toBeDisabled();
  expect(screen.getByTestId('extract-layout-selector')).toBeEnabled();
  expect(mocks.save).not.toHaveBeenCalled();
});

it('keeps tools and options inside Extract and leaves reader View options independent', async () => {
  render(<><div data-testid="old-toolbar-host" /><ExtractView {...props()} /></>); await ready();
  const toolbar = within(screen.getByTestId('extract-view')).getByTestId('extract-toolbar');
  expect(within(screen.getByTestId('old-toolbar-host')).queryByRole('button')).not.toBeInTheDocument();
  expect(within(toolbar).getByRole('group', { name: 'Annotation tools' })).toHaveClass('segmented-toolbar');
  expect(within(toolbar).getByLabelText('Result sheet name')).toHaveValue('Layout 1 results');
  expect(within(toolbar).getByRole('button', { name: 'Preview' })).toHaveAttribute('title', 'Preview on up to 12 documents in this scope');
  fireEvent.click(screen.getByRole('button', { name: 'View options' }));
  expect(screen.queryByLabelText('Expand value areas')).not.toBeInTheDocument();
  fireEvent.click(screen.getByTestId('extract-options-button'));
  expect(screen.getByLabelText('Look on every page')).toBeChecked();
  expect(screen.getByLabelText('Continue across pages')).not.toBeChecked();
  expect(screen.getByLabelText('Result rows')).toHaveValue('');
  fireEvent.click(screen.getByLabelText('Expand value areas')); fireEvent.click(screen.getByLabelText('Continue across pages'));
  fireEvent.click(screen.getByTestId('extract-options-button'));
  expect(screen.getByText(/Expanded areas follow matched field boundaries/)).toBeInTheDocument();
  fireEvent.click(screen.getByTestId('extract-options-button'));
  expect(screen.getByLabelText('Expand value areas')).toBeChecked(); expect(screen.getByLabelText('Continue across pages')).toBeChecked();
});

it('autosaves a half-drawn pair before switching layouts and preserves it through filter changes', async () => {
  const initialProps = props(); const { rerender } = render(<ExtractView {...initialProps} />); await ready();
  act(() => mocks.reader.mock.lastCall![0].renderPageOverlay(1).props.onDraw(region));
  expect(screen.getByRole('button', { name: 'Cancel drawing' })).toBeInTheDocument();
  rerender(<ExtractView {...initialProps} orderKey="new-filter-and-sort" filterScope={{ filter: { Category: { eq: 'A' } } }} />);
  expect(screen.getByRole('button', { name: 'Cancel drawing' })).toBeInTheDocument();
  fireEvent.change(screen.getByTestId('extract-layout-selector'), { target: { value: 'new' } });
  await waitFor(() => expect(screen.getByTestId('extract-layout-selector')).toHaveValue('2'));
  expect(saved[0].draft.fields).toEqual([]); expect(saved[0].draft.pending).toEqual({ tool: 'key', region });
  expect(saved[1].draft.reference_page).toBeNull();
  expect(screen.getByTestId('extract-scope-selector')).toHaveValue('all');
  fireEvent.change(screen.getByTestId('extract-layout-selector'), { target: { value: '1' } });
  await waitFor(() => expect(screen.getByTestId('extract-layout-selector')).toHaveValue('1'));
  expect(screen.getByRole('button', { name: 'Cancel drawing' })).toBeInTheDocument(); expect(screen.getByTestId('extract-new-sheet')).toBeDisabled();
  expect(mocks.select).toHaveBeenLastCalledWith(projectId, { sheet_id: 1, source: 'Document', layout_id: 1 });
});

it('restores an applied layout with an empty cohort without falling back to all documents', async () => {
  saved = [newLayout(1), { ...newLayout(2), has_applied: true }]; selectedId = 2;
  render(<ExtractView {...props()} />); await ready();
  expect(screen.getByTestId('extract-layout-selector')).toHaveValue('2'); expect(screen.getByTestId('extract-scope-selector')).toHaveValue('layout');
  expect(screen.getByRole('option', { name: 'Documents using this layout (0 documents)' })).toBeInTheDocument();
  expect(screen.getByTestId('extract-new-sheet')).toBeDisabled();
});

it('shows autosave failures, keeps incomplete edits and lets the user retry', async () => {
  render(<ExtractView {...props()} />); await ready(); mocks.save.mockRejectedValueOnce(new Error('Storage unavailable'));
  fireEvent.click(screen.getByTestId('extract-options-button')); fireEvent.click(screen.getByLabelText('Continue across pages'));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Storage unavailable'));
  expect(screen.getByLabelText('Continue across pages')).toBeChecked(); fireEvent.click(screen.getByRole('button', { name: 'Retry saving' }));
  await waitFor(() => expect(screen.getByTestId('extract-save-status')).toHaveTextContent('Layout saved'));
  expect(saved[0].draft.continue_across_pages).toBe(true);
});

it('saves the latest draft before submitting one layout and a server-resolved filter scope', async () => {
  saved = [newLayout(1, { ...emptyDraft, reference_blob_id: 'example', reference_fingerprint: 'native:example',
    fields: [{ id: 'name', name: 'Name', key: region, value: { ...region, box: { ...region.box, x0: .3, x1: .6 } }, section_id: null }] })];
  const onExtract = vi.fn(); const filter = { Category: { eq: 'A' } };
  render(<ExtractView {...props({ onExtract, filterScope: { filter, parent_row_id: 4, scope_row_ids: [1, 2] } })} />); await ready();
  fireEvent.change(screen.getByLabelText('Column name for Name'), { target: { value: 'Person' } });
  fireEvent.change(screen.getByTestId('extract-scope-selector'), { target: { value: 'filter' } }); fireEvent.click(screen.getByTestId('extract-new-sheet'));
  await waitFor(() => expect(onExtract).toHaveBeenCalledTimes(1)); const request = onExtract.mock.lastCall![0];
  expect(request).toMatchObject({ source: 'Document', layout_id: 1, sheet_name: 'Layout 1 results',
    extraction_scope: { kind: 'filter', filter, parent_row_id: 4, scope_row_ids: [1, 2] }, template: { fields: [{ name: 'Person' }] } });
  expect(request).not.toHaveProperty('row_ids'); expect(request.template).not.toHaveProperty('pending'); expect(saved[0].draft.fields?.[0].name).toBe('Person');
});

it('blocks repeat submission while the ordinary job controller is extracting', async () => {
  saved = [newLayout(1, { ...emptyDraft, reference_blob_id: 'example', reference_fingerprint: 'native:example',
    fields: [{ id: 'name', name: 'Name', key: region, value: { ...region, box: { ...region.box, x0: .3, x1: .6 } }, section_id: null }] })];
  const onExtract = vi.fn();
  const initialProps = props({ onExtract, extractionRunning: true });
  const { rerender } = render(<ExtractView {...initialProps} />); await ready();
  expect(screen.getByTestId('extract-new-sheet')).toBeDisabled();
  expect(screen.getByTestId('extract-new-sheet')).toHaveTextContent('Extracting…');
  fireEvent.click(screen.getByTestId('extract-new-sheet'));
  expect(onExtract).not.toHaveBeenCalled();
  rerender(<ExtractView {...initialProps} extractionRunning={false} />);
  expect(screen.getByTestId('extract-new-sheet')).toBeEnabled();
});

it('refreshes cohort counts after ordinary sheet inventory changes without resetting a draft', async () => {
  const initialProps = props({ refreshKey: 1 }); const { rerender } = render(<ExtractView {...initialProps} />); await ready();
  fireEvent.click(screen.getByTestId('extract-options-button')); fireEvent.click(screen.getByLabelText('Continue across pages'));
  mocks.counts.mockResolvedValue({ all: 3, filter: 2, layout: 2, this: 1 }); saved[0].has_applied = true;
  rerender(<ExtractView {...initialProps} refreshKey={2} />);
  await waitFor(() => expect(screen.getByRole('option', { name: 'Documents using this layout (2 documents)' })).toBeInTheDocument());
  expect(screen.getByLabelText('Continue across pages')).toBeChecked(); expect(screen.getByTestId('extract-scope-selector')).toHaveValue('all');
});

it('reports a layout load failure as a load error and retries listing instead of saving', async () => {
  mocks.templates.mockRejectedValueOnce(new Error('Temporary connection failure'));
  render(<ExtractView {...props()} />);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not load layouts: Temporary connection failure'));
  expect(screen.getByTestId('extract-save-status')).toHaveTextContent('No layout available');
  expect(screen.queryByRole('button', { name: 'Retry saving' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry loading layouts' }));
  await ready();
  expect(screen.getByTestId('extract-save-status')).toHaveTextContent('Layout saved');
  expect(mocks.save).not.toHaveBeenCalled();
});

it('does not describe a metadata refresh or selection failure as a failed save', async () => {
  saved.push(newLayout(2));
  const initialProps = props({ refreshKey: 1 });
  const { rerender } = render(<ExtractView {...initialProps} />); await ready();
  mocks.templates.mockRejectedValueOnce(new Error('Refresh unavailable'));
  rerender(<ExtractView {...initialProps} refreshKey={2} />);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not refresh layout documents: Refresh unavailable'));
  expect(screen.getByTestId('extract-save-status')).toHaveTextContent('Layout saved');
  expect(screen.queryByRole('button', { name: 'Retry saving' })).not.toBeInTheDocument();
  mocks.select.mockRejectedValueOnce(new Error('Selection unavailable'));
  fireEvent.change(screen.getByTestId('extract-layout-selector'), { target: { value: '2' } });
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not select layout: Selection unavailable'));
  expect(screen.getByTestId('extract-save-status')).toHaveTextContent('Layout saved');
  expect(screen.queryByRole('button', { name: 'Retry saving' })).not.toBeInTheDocument();
  expect(mocks.save).not.toHaveBeenCalled();
});
