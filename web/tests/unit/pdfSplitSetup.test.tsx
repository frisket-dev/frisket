// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PdfSplitSetup } from '../../src/components/pdfSplit/PdfSplitSetup';

const api = vi.hoisted(() => ({ create: vi.fn(), remove: vi.fn(), get: vi.fn(), token: vi.fn(), quote: vi.fn(), sampleQuote: vi.fn(), sampleStart: vi.fn(), page: vi.fn().mockResolvedValue({ page: 1, preview_text: '' }) }));
vi.mock('../../src/api/pdfPacketSplits', () => ({ createPdfPacketSplitFromSheet: api.create, getPdfPacketSheetScopeToken: api.token, deletePdfPacketSplit: api.remove, getPdfPacketSplit: api.get, getPdfPacketPage: api.page, estimatePdfPacketBatchOcr: api.quote, estimatePdfPacketOcr: api.sampleQuote, startPdfPacketOcr: api.sampleStart, pdfPacketThumbnailUrl: () => '' }));
vi.mock('../../src/components/importWorkspace/pdfPacket/PdfPacketReview', () => ({ PdfPacketReview: ({ snapshot, onSnapshotChange, onReviewed }: { snapshot: Record<string, unknown>; onSnapshotChange(value: unknown): void; onReviewed(value: Record<string, number[]>): void }) => <div><button onClick={() => onSnapshotChange({ ...snapshot, analysis_revision: 10 })}>New revision</button><button onClick={() => onSnapshotChange({ ...snapshot, analysis_revision: 9 })}>Old revision</button><button onClick={() => onReviewed({ r: [1] })}>Apply review</button></div> }));
vi.mock('../../src/components/CostGateModal', () => ({ CostGateModal: ({ onConfirm }: { onConfirm(): void }) => <button onClick={onConfirm}>Approve quoted OCR</button> }));
vi.mock('../../src/engine-selector/SelectorField', () => ({ SelectorField: () => null }));
const base = { split_id: 's', status: 'ready', sources: [{ key: 'r', filename: 'a.pdf', page_start: 1, page_end: 3, display_start: 1, text_category: 'text' }], sheet_scope: { order_hash: 'view-hash', excluded: [] } };
Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); } });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('freezes view membership before resolving a partly cached selection', async () => {
  api.token.mockResolvedValueOnce({ order_hash: 'view-hash' });
  api.create.mockResolvedValueOnce(base);
  api.remove.mockResolvedValue(undefined);
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: ['8'], rowIndexes: [2, 3], cachedAnchors: [{ index: 2, rowId: '8' }] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{ filter_: '{"test":1}', sort: null }} onClose={() => {}} onApply={async () => {}} />);
  await screen.findByRole('heading', { name: '3 rows' });
  expect(screen.queryByLabelText('Split stages')).toBeNull();
  expect(api.create).toHaveBeenNthCalledWith(1, 'p', expect.objectContaining({ scope: 'selected', expected_order_hash: 'view-hash', selected_index_ranges: [{ start: 2, end: 3 }], cached_anchors: [{ index: 2, row_id: 8 }] }), expect.anything());
  expect(api.token).toHaveBeenCalledWith('p', expect.objectContaining({ sheet_id: 1, scope: 'selected', filter_: '{"test":1}' }), expect.anything());
});
it('routes missing text through Check text and applies the explicit none policy', async () => {
  api.create.mockResolvedValueOnce({ ...base, sources: [{ ...base.sources[0], text_category: 'missing' }] });
  api.remove.mockResolvedValue(undefined);
  const apply = vi.fn().mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={apply} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
  expect(screen.getByLabelText('Split stages')).toBeTruthy();
  fireEvent.click(screen.getByRole('radio', { name: /^None/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Create 3 rows' }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(expect.objectContaining({ ocrPolicy: 'none', ocrConfirmation: null, starts: {} })));
});

it('passes the exact approved quote hash with OCR policy and engine', async () => {
  api.create.mockResolvedValueOnce({ ...base, analysis_revision: 7, sources: [{ ...base.sources[0], text_category: 'missing' }] });
  api.remove.mockResolvedValue(undefined);
  api.quote.mockResolvedValue({ pages: 3, estimate: { requires_confirmation: true, promise_set_hash: 'quoted-plan' } });
  const apply = vi.fn().mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={apply} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
  fireEvent.click(screen.getByRole('radio', { name: /Missing text/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Create 3 rows' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Approve quoted OCR' }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(expect.objectContaining({ ocrPolicy: 'missing', ocrSelection: { kind: 'engine', engine: 'rapidocr' }, ocrConfirmation: 'quoted-plan' })));
});

it('binds a cached sample OCR page without a provider job', async () => {
  api.create.mockResolvedValueOnce({ ...base, sources: [{ ...base.sources[0], text_category: 'missing' }] });
  api.remove.mockResolvedValue(undefined);
  api.sampleQuote.mockResolvedValue({ cached_pages: [1], estimate: null });
  api.sampleStart.mockResolvedValue({ status: 'cached', job_id: null, kind: 'ocr_sample', total: 0 });
  api.get.mockResolvedValue(base);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
  fireEvent.click(screen.getByRole('button', { name: 'OCR preview' }));
  await waitFor(() => expect(api.sampleQuote).toHaveBeenCalled());
  await waitFor(() => expect(api.sampleStart).toHaveBeenCalledWith('p', 's', expect.objectContaining({ scope: 'sample', force_rescan: false }), expect.anything()));
});
it('keeps the sheet dialog mounted while reviewing boundaries', async () => {
  api.create.mockResolvedValue(base);
  api.remove.mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  await screen.findByRole('heading', { name: '3 rows' });
  fireEvent.click(screen.getByRole('radio', { name: /Split into documents/ }));
  fireEvent.click(await screen.findByRole('button', { name: 'Find documents →' }));
  expect(screen.getByRole('dialog', { name: 'Split PDFs' })).toBeTruthy();
});
it('bounds the visible exclusion list', async () => {
  api.create.mockResolvedValueOnce({ ...base, sheet_scope: { order_hash: 'h', excluded: Array.from({ length: 30 }, (_, index) => ({ row_id: index + 10, reason: 'not_pdf' })) } });
  api.remove.mockResolvedValue(undefined);
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 40, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  await screen.findByRole('heading', { name: '3 rows' });
  expect(document.body.querySelectorAll('.pdf-split-excluded')).toHaveLength(9);
  expect(screen.getByText('+ 21 more excluded rows')).toBeTruthy();
});
it('finishes a sample even if OCR changes its source text category', async () => {
  api.create.mockResolvedValueOnce({ ...base, analysis_revision: 1, jobs: [], sources: [{ ...base.sources[0], text_category: 'missing' }] });
  api.remove.mockResolvedValue(undefined);
  api.sampleQuote.mockResolvedValue({ cached_pages: [], estimate: { requires_confirmation: false } });
  api.sampleStart.mockResolvedValue({ job_id: 'job-1' });
  api.get.mockResolvedValue({ ...base, analysis_revision: 2, jobs: [{ job_id: 'job-1', progress: { status: 'done', error: null } }] });
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 10, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
  fireEvent.click(screen.getByRole('button', { name: 'OCR preview' }));
  await waitFor(() => expect(api.sampleStart).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create 3 rows' })).not.toHaveProperty('disabled', true), { timeout: 3000 });
  expect(screen.getByRole('dialog', { name: 'Check text' })).toBeTruthy();
});
it('admits the PDF column chosen in the document view', async () => {
  api.create.mockResolvedValueOnce(base);
  api.remove.mockResolvedValue(undefined);
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'other', type: 'file' }, { id: '3', name: 'active PDF', type: 'file' }] }} fileColumnId="3" selection={{ sheetId: '1', rowIds: ['8'], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  await screen.findByRole('heading', { name: '3 rows' });
  expect(api.create).toHaveBeenCalledWith('p', expect.objectContaining({ file_column_id: 3, selected_row_ids: [8] }), expect.anything());
});
it('keeps the admitted session when sheet metadata refreshes with the same file column', async () => {
  api.create.mockResolvedValue(base);
  api.remove.mockResolvedValue(undefined);
  const selection = { sheetId: '1', rowIds: [], rowIndexes: [] };
  const view = {};
  const getRows = vi.fn().mockResolvedValue({ rows: [] });
  const sheet = { id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' as const }] };
  const renderSetup = (value: typeof sheet) => <PdfSplitSetup projectId="p" sheet={value} selection={{ ...selection, rowIds: [...selection.rowIds], rowIndexes: [...selection.rowIndexes] }} getSheetRows={getRows} view={{ ...view }} onClose={() => {}} onApply={async () => {}} />;
  const mounted = render(renderSetup(sheet));
  await screen.findByRole('heading', { name: '3 rows' });
  mounted.rerender(renderSetup({ ...sheet, columns: sheet.columns.map((column) => ({ ...column })) }));
  expect(api.create).toHaveBeenCalledTimes(1);
});
it('uses the shared modal cancel behavior for Escape', async () => {
  api.create.mockResolvedValueOnce(base);
  api.remove.mockResolvedValue(undefined);
  const onClose = vi.fn();
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={onClose} onApply={async () => {}} />);
  fireEvent(screen.getByRole('dialog', { name: 'Split PDFs' }), new Event('cancel', { bubbles: true, cancelable: true }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
it('does not schedule OCR when pages have usable text and Check text is skipped', async () => {
  api.create.mockResolvedValueOnce(base);
  api.remove.mockResolvedValue(undefined);
  const apply = vi.fn().mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={apply} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Create 3 rows' }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(expect.objectContaining({ ocrPolicy: 'none', ocrConfirmation: null, starts: {} })));
  expect(api.quote).not.toHaveBeenCalled();
});

it('keeps a newer review revision when an older snapshot arrives', async () => {
  api.create.mockResolvedValue({ ...base, analysis_revision: 1 });
  api.remove.mockResolvedValue(undefined);
  const apply = vi.fn().mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={apply} />);
  await screen.findByRole('heading', { name: '3 rows' });
  fireEvent.click(screen.getByRole('radio', { name: /Split into documents/ }));
  fireEvent.click(await screen.findByRole('button', { name: 'Find documents →' }));
  fireEvent.click(screen.getByRole('button', { name: 'New revision' }));
  fireEvent.click(screen.getByRole('button', { name: 'Old revision' }));
  fireEvent.click(screen.getByRole('button', { name: 'Apply review' }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(expect.objectContaining({ snapshot: expect.objectContaining({ analysis_revision: 10 }) })));
});
it('keeps the shared modal mounted across setup and text check', async () => {
  api.create.mockResolvedValueOnce({ ...base, sources: [{ ...base.sources[0], text_category: 'missing' }] });
  api.remove.mockResolvedValue(undefined);
  const show = vi.spyOn(HTMLDialogElement.prototype, 'showModal');
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(show).toHaveBeenCalledTimes(1);
  show.mockRestore();
});

it('shows bounded document text provenance and friendly exclusions', async () => {
  api.create.mockResolvedValue({ ...base, sheet_scope: { order_hash: 'h', excluded: [{ row_id: 99, reason: 'invalid_page' }] } });
  api.page.mockResolvedValue({ page: 1, preview_text: 'Prepared excerpt', preview_text_kind: 'ocr' });
  api.remove.mockResolvedValue(undefined);
  const { fireEvent } = await import('@testing-library/react');
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 2, columns: [{ id: '2', name: 'media', type: 'file' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  await screen.findByRole('heading', { name: '3 rows' });
  fireEvent.click(screen.getByRole('radio', { name: /Split into documents/ }));
  expect(await screen.findByText('Prepared excerpt')).toBeTruthy();
  expect(screen.getByText('OCR')).toBeTruthy();
  expect(screen.getByText('Unavailable page · stays as it is')).toBeTruthy();
  expect(screen.queryByText(/invalid_page|Row 99/)).toBeNull();
});
it('uses singular counts for one PDF and one page', async () => {
  api.create.mockResolvedValueOnce({ ...base, sources: [{ ...base.sources[0], page_end: 1 }] });
  api.remove.mockResolvedValue(undefined);
  render(<PdfSplitSetup projectId="p" sheet={{ id: '1', name: 'Sheet', rowCount: 1, columns: [{ id: '2', name: 'media', type: 'file' }, { id: '3', name: 'case', type: 'text' }] }} selection={{ sheetId: '1', rowIds: [], rowIndexes: [] }} getSheetRows={vi.fn().mockResolvedValue({ rows: [] })} view={{}} onClose={() => {}} onApply={async () => {}} />);
  expect(await screen.findByRole('heading', { name: '1 row' })).toBeTruthy();
  expect(screen.getByText('from 1 PDF')).toBeTruthy();
  expect(screen.getByText('1 row · can be undone')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Create 1 row' })).toBeTruthy();
  expect(screen.getByRole('checkbox', { name: 'case' })).toHaveProperty('checked', false);
});
