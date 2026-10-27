// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PdfPacketReview } from '../../src/components/importWorkspace/pdfPacket/PdfPacketReview';
import type { PdfPacketSplitSnapshot } from '../../src/api/pdfPacketSplits';

Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); } });

const matcher = vi.hoisted(() => vi.fn(async () => ({ analysis_revision: 1, pages: [], suggested_pages: [], unsure_pages: [], phrase_counts: {} })));
vi.mock('../../src/components/importWorkspace/pdfPacket/PacketLargePageView', () => ({ PacketLargePageView: () => null }));
vi.mock('../../src/api/pdfPacketSplits', () => ({
  matchPdfPacketCandidates: matcher,
  getPdfPacketSplit: vi.fn(), setPdfPacketTextSource: vi.fn(),
  pdfPacketThumbnailUrl: (_project: string, _split: string, page: number) => `/page/${page}`,
}));

const snapshot = {
  split_id: 's', status: 'ready', packet: { blob_hash: 'hash', filename: 'a.pdf', page_count: 5 },
  sources: [
    { key: 'a', filename: 'a.pdf', page_start: 1, page_end: 2, display_start: 1, blob_hash: 'a' },
    { key: 'b', filename: 'b.pdf', page_start: 4, page_end: 6, display_start: 3, blob_hash: 'b' },
  ], fixed_starts: [1, 3], example_pages: [], text_source: 'native', analysis_revision: 1, ocr_pages: [],
  prepare: { native_text_pages: [1, 2, 3] }, jobs: [],
} as unknown as PdfPacketSplitSnapshot;

afterEach(() => { cleanup(); matcher.mockReset(); matcher.mockResolvedValue({ analysis_revision: 1, pages: [], suggested_pages: [], unsure_pages: [], phrase_counts: {} }); });
describe('combined PDF review', () => {
  it('submits only confirmed interior starts with original-local fixed starts', async () => {
    const onReviewed = vi.fn();
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={onReviewed} onBack={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /Page 5/ }));
    fireEvent.click(screen.getByRole('button', { name: /Continue to import/ }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ a: [1], b: [4, 5] }));
  });
  it('shows source first pages as examples without making extra document boundaries', async () => {
    const onReviewed = vi.fn();
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={onReviewed} onBack={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /b.pdf Page 4, file start/ }));
    fireEvent.click(screen.getByRole('button', { name: /Continue to import/ }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ a: [1], b: [4] }));
  });
  it('asks before discarding marked starts but not before marking', () => {
    const onBack = vi.fn();
    const first = render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={() => {}} onBack={onBack} />);
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(onBack).toHaveBeenCalledTimes(1);
    first.unmount();
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={() => {}} onBack={onBack} />);
    fireEvent.click(screen.getByRole('button', { name: /Page 5/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByRole('dialog', { name: 'Discard this split?' })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Keep working' }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
  it('registers the same discard guard for the enclosing modal', () => {
    const onClose = vi.fn();
    let requestClose = () => {};
    const register = (handler: () => void) => { requestClose = handler; return () => {}; };
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={() => {}} onBack={() => {}} onClose={onClose} registerCloseRequest={register} />);
    fireEvent.click(screen.getByRole('button', { name: /Page 5/ }));
    act(() => requestClose());
    expect(screen.getByRole('dialog', { name: 'Discard this split?' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    act(() => requestClose());
    expect(screen.queryByRole('dialog', { name: 'Discard this split?' })).toBeNull();
  });
  it('does not commit an unaccepted suggestion', async () => {
    matcher.mockResolvedValue({ analysis_revision: 1, pages: [], suggested_pages: [4], unsure_pages: [], phrase_counts: {} });
    const onReviewed = vi.fn();
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={onReviewed} onBack={() => {}} />);
    await screen.findByRole('button', { name: 'Accept all 1 suggested' });
    fireEvent.click(screen.getByRole('button', { name: /Continue to import/ }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ a: [1], b: [4] }));
  });
  it('applies suggested boundaries only after accepting them', async () => {
    matcher.mockResolvedValue({ analysis_revision: 1, pages: [], suggested_pages: [4], unsure_pages: [], phrase_counts: {} });
    const onReviewed = vi.fn();
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={onReviewed} onBack={() => {}} />);
    const accept = await screen.findByRole('button', { name: 'Accept all 1 suggested' });
    fireEvent.click(accept);
    fireEvent.click(screen.getByRole('button', { name: /Continue to import/ }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ a: [1], b: [4, 5] }));
  });
  it('marks file boundaries without adding filename labels above the grid', () => {
    const { container } = render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={() => {}} onBack={() => {}} />);
    const fileStart = container.querySelector('[data-packet-page="3"]');
    expect(fileStart?.hasAttribute('data-fixed')).toBe(true);
    expect(fileStart?.querySelector('.pdf-packet-doc-label')?.textContent).not.toContain('b.pdf');
  });
  it('explains that visual suggestions have not started without examples', () => {
    render(<PdfPacketReview projectId="p" snapshot={snapshot} onReviewed={() => {}} onBack={() => {}} />);
    expect(screen.getByText('No suggestions yet.')).toBeTruthy();
  });
  it('counts read pages including completed empty text', () => {
    const withEmpty = { ...snapshot, prepare: { ...snapshot.prepare, text_pages_read: [1, 2, 3, 4, 5] } } as unknown as PdfPacketSplitSnapshot;
    render(<PdfPacketReview projectId="p" snapshot={withEmpty} onReviewed={() => {}} onBack={() => {}} />);
    expect(screen.getByText('Text: 5 of 5 read')).toBeTruthy();
  });
  it('does not request thumbnails before preparation is ready', () => {
    const preparing = { ...snapshot, status: 'preparing', prepare: { native_text_pages: [], progress: { status: 'running', done: 1, total: 5 } } } as unknown as PdfPacketSplitSnapshot;
    const { container } = render(<PdfPacketReview projectId="p" snapshot={preparing} onReviewed={() => {}} onBack={() => {}} />);
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(screen.getByText('Preparing pages')).toBeTruthy();
  });
  it('mounts a bounded page window for a large packet', () => {
    const large = { ...snapshot, packet: { ...snapshot.packet, page_count: 2000 }, sources: [{ key: 'large', filename: 'large.pdf', page_start: 1, page_end: 2000, display_start: 1, blob_hash: 'a' }], fixed_starts: [1] } as unknown as PdfPacketSplitSnapshot;
    const { container } = render(<PdfPacketReview projectId="p" snapshot={large} onReviewed={() => {}} onBack={() => {}} />);
    expect(container.querySelectorAll('[data-packet-page]').length).toBeLessThanOrEqual(96);
    const grid = container.querySelector('.pdf-packet-page-grid')!;
    fireEvent.scroll(grid, { target: { scrollTop: 20000 } });
    fireEvent.click(screen.getByRole('button', { name: /Starts only/ }));
    expect(container.querySelector('[data-packet-page="1"]')).not.toBeNull();
  });

});
