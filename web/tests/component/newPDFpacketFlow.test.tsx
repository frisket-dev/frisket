// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  PdfPacketMatchResult,
  PdfPacketPage,
  PdfPacketSplitSnapshot,
} from '../../src/api/pdfPacketSplits';
import { PdfPacketImportFlow } from '../../src/components/importWorkspace/pdfPacket/PdfPacketImportFlow';

const api = vi.hoisted(() => ({
  cancelPdfPacketOcr: vi.fn(),
  commitPdfPacketSplit: vi.fn(),
  createPdfPacketSplit: vi.fn(),
  deletePdfPacketSplit: vi.fn(),
  estimatePdfPacketOcr: vi.fn(),
  getPdfPacketPage: vi.fn(),
  getPdfPacketSplit: vi.fn(),
  matchPdfPacketCandidates: vi.fn(),
  setPdfPacketTextSource: vi.fn(),
  startPdfPacketOcr: vi.fn(),
}));

vi.mock('../../src/api/pdfPacketSplits', () => ({
  ...api,
  pdfPacketThumbnailUrl: (_projectId: string, _splitId: string, page: number) => `/thumb/${page}`,
}));

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: () => <div data-testid="mock-ocr-engine">rapidocr</div>,
}));

vi.mock('../../src/media/PdfViewer', () => ({
  PdfViewer: ({ url, currentPage }: { url: string; currentPage: number }) => (
    <div data-testid="mock-pdf-viewer" data-url={url} data-page={currentPage} />
  ),
}));

const baseSnapshot: PdfPacketSplitSnapshot = {
  schema_version: 'frisket.pdf_packet_split.v1',
  split_id: 'split-1',
  status: 'ready',
  packet: {
    blob_hash: 'sha256:packet',
    filename: 'Records.pdf',
    mime: 'application/pdf',
    size: 4_000,
    page_count: 4,
  },
  prepare: {
    job_id: 'prepare-1',
    progress: { status: 'done', done: 4, total: 4, error: null },
    pages_ready: [1, 2, 3, 4],
    native_text_pages: [1, 2, 3, 4],
    visual_pages_ready: 4,
  },
  text_source: 'unconfirmed',
  ocr_pages: [],
  ocr_engine: null,
  jobs: [],
  analysis_revision: 0,
  expires_at: '2026-10-10T00:00:00Z',
  commit_result: null,
};

const nativePage: PdfPacketPage = {
  schema_version: 'frisket.pdf_packet_split.v1',
  split_id: 'split-1',
  page: 1,
  thumbnail_ready: true,
  thumbnail_url: '/thumb/1',
  native_text: 'Native text from the packet',
  ocr_text: null,
  ocr_blocks: [],
  ocr_engine: null,
};

const matches: PdfPacketMatchResult = {
  schema_version: 'frisket.pdf_packet_split.v1',
  split_id: 'split-1',
  analysis_revision: 1,
  clusters: [{ id: 1, confirmed_pages: [1] }],
  pages: [],
  suggested_pages: [],
  unsure_pages: [],
  phrase_counts: {},
  accept_all_scope: 'packet',
};

function sampleDoneSnapshot(textSource: 'unconfirmed' | 'ocr' = 'unconfirmed'): PdfPacketSplitSnapshot {
  return {
    ...baseSnapshot,
    text_source: textSource,
    ocr_engine: textSource === 'ocr' ? 'rapidocr' : null,
    analysis_revision: 1,
    jobs: [{
      job_id: 'sample-1',
      kind: 'ocr_sample',
      progress: { status: 'done', done: 1, total: 1, error: null },
      engine: 'rapidocr',
      pages: [1],
      receipt_id: null,
      accounting: null,
    }],
  };
}

function renderFlow(projectId = 'project-1') {
  const props = {
    projectId,
    onContextChange: vi.fn(),
    registerCloseGuard: vi.fn(),
    onImported: vi.fn(),
    onError: vi.fn(),
  };
  return { ...render(<PdfPacketImportFlow {...props} />), props };
}

async function choosePacket() {
  fireEvent.change(screen.getByTestId('pdf-packet-input'), {
    target: { files: [new File(['pdf'], 'Records.pdf', { type: 'application/pdf' })] },
  });
  await screen.findByText('Native text from the packet');
}

async function reachImportStep() {
  renderFlow();
  await choosePacket();
  fireEvent.click(screen.getByRole('button', { name: 'Yes, use extracted text' }));
  await screen.findByText('Pages that look like your starts');
  fireEvent.click(screen.getByRole('button', { name: 'Continue with 1 marked start →' }));
  return screen.getByRole('button', { name: 'Import 1 document' });
}

beforeEach(() => {
  window.localStorage.clear();
  api.createPdfPacketSplit.mockResolvedValue(baseSnapshot);
  api.getPdfPacketPage.mockResolvedValue(nativePage);
  api.getPdfPacketSplit.mockResolvedValue(baseSnapshot);
  api.estimatePdfPacketOcr.mockResolvedValue({
    schema_version: 'frisket.pdf_packet_split.v1',
    engine: 'rapidocr',
    scope: 'sample',
    pages: [1],
    cached_pages: [],
    estimate: { cost: 0, rows: 1, billed_cost: 0, policy_id: 'local.v1', requires_confirmation: false },
  });
  api.startPdfPacketOcr.mockResolvedValue({
    schema_version: 'frisket.pdf_packet_split.v1',
    split_id: 'split-1',
    job_id: 'sample-1',
    kind: 'ocr_sample',
    total: 1,
    receipt_id: null,
  });
  api.setPdfPacketTextSource.mockImplementation(async (_projectId, _splitId, source) => ({
    ...baseSnapshot,
    text_source: source.kind,
    ocr_engine: source.kind === 'ocr' ? source.engine : null,
  }));
  api.matchPdfPacketCandidates.mockResolvedValue(matches);
  api.commitPdfPacketSplit.mockResolvedValue({
    schema_version: 'frisket.pdf_packet_split.v1',
    split_id: 'split-1',
    job_id: 'commit-1',
    kind: 'commit',
    total: 1,
    receipt_id: null,
  });
  api.cancelPdfPacketOcr.mockResolvedValue(baseSnapshot);
  api.deletePdfPacketSplit.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('PDF packet import flow boundaries', () => {
  it('accepts a dropped PDF and goes directly to Check text while its page loads', async () => {
    let resolvePage!: (page: PdfPacketPage) => void;
    api.getPdfPacketPage.mockReturnValue(new Promise((resolve) => {
      resolvePage = resolve;
    }));

    renderFlow();
    fireEvent.drop(screen.getByTestId('pdf-packet-chooser'), {
      dataTransfer: {
        files: [new File(['pdf'], 'Records.pdf', { type: 'application/pdf' })],
        types: ['Files'],
      },
    });

    expect(await screen.findByRole('status', { name: 'Reading page' })).toBeInTheDocument();
    expect(screen.queryByText('Browse the packet before checking its text.')).not.toBeInTheDocument();
    await act(async () => resolvePage(nativePage));
    expect(await screen.findByRole('heading', { name: 'Extracted text' })).toBeInTheDocument();
    expect(screen.getByText('Native text from the packet')).toBeInTheDocument();
  });

  it('can continue to visual matching when the sampled page has no text', async () => {
    api.getPdfPacketPage.mockResolvedValue({ ...nativePage, native_text: null });
    api.setPdfPacketTextSource.mockResolvedValue({ ...baseSnapshot, text_source: 'native' });

    renderFlow();
    fireEvent.change(screen.getByTestId('pdf-packet-input'), {
      target: { files: [new File(['pdf'], 'Records.pdf', { type: 'application/pdf' })] },
    });

    const continueButton = await screen.findByRole('button', { name: 'Continue without OCR' });
    fireEvent.click(continueButton);
    expect(await screen.findByText('Pages that look like your starts')).toBeInTheDocument();
    expect(screen.getByText(/Learning from 1 start you marked/)).toBeInTheDocument();
    expect(api.setPdfPacketTextSource).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      { kind: 'native' },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('refreshes six distinct sample pages and selects the first new sample', async () => {
    const laterTextSnapshot = {
      ...baseSnapshot,
      packet: { ...baseSnapshot.packet, page_count: 20 },
      prepare: {
        ...baseSnapshot.prepare,
        progress: { ...baseSnapshot.prepare.progress, done: 20, total: 20 },
        pages_ready: Array.from({ length: 20 }, (_, index) => index + 1),
        native_text_pages: Array.from({ length: 20 }, (_, index) => index + 1),
        visual_pages_ready: 20,
      },
    };
    api.createPdfPacketSplit.mockResolvedValue(laterTextSnapshot);
    api.getPdfPacketPage.mockImplementation(async (_projectId, _splitId, page: number) => ({
      ...nativePage,
      page,
      native_text: `Native text on page ${page}`,
    }));

    renderFlow();
    fireEvent.change(screen.getByTestId('pdf-packet-input'), {
      target: { files: [new File(['pdf'], 'Records.pdf', { type: 'application/pdf' })] },
    });

    await screen.findByText('Native text on page 1');
    const originalSamples = screen.getAllByRole('button', { name: /^p \d+$/ }).map((button) => button.textContent);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sample pages' }));

    const refreshedSamples = screen.getAllByRole('button', { name: /^p \d+$/ });
    expect(refreshedSamples).toHaveLength(6);
    expect(new Set(refreshedSamples.map((button) => button.textContent))).toHaveSize(6);
    const firstNewSample = refreshedSamples.find((button) => !originalSamples.includes(button.textContent));
    expect(firstNewSample).toHaveAttribute('aria-pressed', 'true');
  });

  it('can leave Check text to choose another file', async () => {
    renderFlow();
    await choosePacket();

    expect(screen.getByRole('button', { name: 'Refresh sample pages' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(await screen.findByTestId('pdf-packet-chooser')).toBeInTheDocument();
    expect(api.deletePdfPacketSplit).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('offers compact grid controls and one whole-packet review card', async () => {
    const reviewMatches: PdfPacketMatchResult = {
      ...matches,
      pages: [
        { page: 2, visual_score: 74, closest_confirmed_page: 1, kind_id: 1, matched_phrase_ids: [], suggested: false },
        { page: 3, visual_score: 92, closest_confirmed_page: 1, kind_id: 1, matched_phrase_ids: [], suggested: true },
        { page: 4, visual_score: 68, closest_confirmed_page: 1, kind_id: 1, matched_phrase_ids: [], suggested: false },
      ],
      suggested_pages: [3],
      unsure_pages: [2, 4],
    };
    api.matchPdfPacketCandidates.mockResolvedValue(reviewMatches);

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Yes, use extracted text' }));

    expect(await screen.findByRole('button', { name: 'Unsure 2' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Suggested 1' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'How to find document starts' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'All pages 4' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Starts only 1' })).toBeInTheDocument();
    expect(screen.getByRole('slider', { name: 'Page size' })).toHaveValue('1');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(screen.getByRole('slider', { name: 'Page size' })).toHaveValue('2');

    expect(screen.getByText('Does a document start here?')).toBeInTheDocument();
    expect(screen.getByText('1 of 2')).toBeInTheDocument();
    expect(screen.getByText('p 1 · before')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'View page 2 large' })).toHaveTextContent('Page 2 · 74% similar');
    fireEvent.click(screen.getByRole('button', { name: 'Skip to next unsure page' }));
    expect(screen.getByRole('button', { name: 'View page 4 large' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Previous unsure page' }));
    fireEvent.click(screen.getByRole('button', { name: /^No/ }));
    expect(screen.getByRole('button', { name: 'View page 4 large' })).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: 'Suggested 1' }));
    expect(screen.getByText('Likely a start')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Not a start' })).toBeInTheDocument();
    expect(screen.getByText('1 marked · 1 suggested · 1 unsure')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Accept all 1 suggested' }));
    expect(await screen.findByText('2 marked · 0 suggested · 1 unsure')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Starts only 2' }));
    expect(screen.queryByRole('button', { name: 'Page 2, unsure start' })).not.toBeInTheDocument();
  });

  it('uses the packet blob in a keyboard-operable large page viewer', async () => {
    api.matchPdfPacketCandidates.mockResolvedValue({
      ...matches,
      pages: [
        { page: 2, visual_score: 74, closest_confirmed_page: 1, kind_id: 1, matched_phrase_ids: [], suggested: false },
        { page: 3, visual_score: 92, closest_confirmed_page: 1, kind_id: 1, matched_phrase_ids: [], suggested: true },
      ],
      suggested_pages: [3],
      unsure_pages: [2],
    });

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Yes, use extracted text' }));
    fireEvent.click(await screen.findByRole('button', { name: 'View page 2 large' }));

    const dialog = await screen.findByRole('dialog', { name: 'Page 2' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByTestId('mock-pdf-viewer')).toHaveAttribute(
      'data-url',
      '/api/projects/project-1/blobs/sha256%3Apacket',
    );
    expect(within(dialog).getByText('Unsure · 74%')).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: 'ArrowRight' });
    expect(screen.getByRole('dialog', { name: 'Page 3' })).toBeInTheDocument();
    expect(screen.getByTestId('mock-pdf-viewer')).toHaveAttribute('data-page', '3');
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Page 3' }), { key: 'x' });
    expect(screen.getByText('Marked not a start')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Page 3' }), { key: 'Escape' });
    expect(screen.queryByTestId('pdf-packet-large-view')).not.toBeInTheDocument();
  });

  it('selects the completed sample engine before showing its OCR text', async () => {
    let ocrSelected = false;
    api.getPdfPacketSplit.mockResolvedValue(sampleDoneSnapshot());
    api.setPdfPacketTextSource.mockImplementation(async (_projectId, _splitId, source) => {
      if (source.kind === 'ocr') ocrSelected = true;
      return source.kind === 'ocr' ? sampleDoneSnapshot('ocr') : { ...baseSnapshot, text_source: 'native' };
    });
    api.getPdfPacketPage.mockImplementation(async () => ocrSelected ? {
      ...nativePage,
      ocr_text: 'OCR text from rapidocr',
      ocr_engine: 'rapidocr',
    } : nativePage);

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Looks wrong? Run OCR' }));
    fireEvent.click(screen.getByRole('button', { name: 'Extract text' }));

    await screen.findByText('OCR text from rapidocr');
    expect(api.setPdfPacketTextSource).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      { kind: 'ocr', engine: 'rapidocr' },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole('button', { name: 'Use this OCR and read all 4 pages' })).toBeEnabled();
  });

  it('uses a cached OCR sample without trying to start another job', async () => {
    let ocrSelected = false;
    api.estimatePdfPacketOcr.mockResolvedValue({
      schema_version: 'frisket.pdf_packet_split.v1',
      engine: 'rapidocr',
      scope: 'sample',
      pages: [1],
      cached_pages: [1],
      estimate: null,
    });
    api.setPdfPacketTextSource.mockImplementation(async () => {
      ocrSelected = true;
      return { ...baseSnapshot, text_source: 'ocr', ocr_engine: 'rapidocr' };
    });
    api.getPdfPacketPage.mockImplementation(async () => ocrSelected ? {
      ...nativePage,
      ocr_text: 'Cached OCR text',
      ocr_engine: 'rapidocr',
    } : nativePage);

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Looks wrong? Run OCR' }));
    fireEvent.click(screen.getByRole('button', { name: 'Extract text' }));

    await screen.findByText('Cached OCR text');
    expect(api.startPdfPacketOcr).not.toHaveBeenCalled();
    expect(api.setPdfPacketTextSource).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      { kind: 'ocr', engine: 'rapidocr' },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('keeps Extract text disabled and announces progress for the whole running OCR job', async () => {
    const sampleRunning: PdfPacketSplitSnapshot = {
      ...baseSnapshot,
      jobs: [{
        job_id: 'sample-1',
        kind: 'ocr_sample',
        progress: { status: 'running', done: 0, total: 1, error: null },
        engine: 'rapidocr',
        pages: [1],
        receipt_id: null,
        accounting: null,
      }],
    };
    api.getPdfPacketSplit.mockResolvedValue(sampleRunning);

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Looks wrong? Run OCR' }));
    const extract = screen.getByRole('button', { name: 'Extract text' });
    fireEvent.click(extract);

    expect(await screen.findByRole('status', { name: 'Extracting text' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Extract text' })).toBeDisabled();
    expect(screen.getByRole('heading', { name: 'Extracted text' })).toBeInTheDocument();
  });

  it('waits for deliberate cost approval and echoes only the quoted token', async () => {
    api.estimatePdfPacketOcr.mockResolvedValue({
      schema_version: 'frisket.pdf_packet_split.v1',
      engine: 'rapidocr',
      scope: 'sample',
      pages: [1],
      cached_pages: [],
      estimate: {
        cost: 0.25,
        rows: 1,
        billed_cost: 250_000,
        policy_id: 'hosted.v1',
        requires_confirmation: true,
        promise_set_hash: 'quote-hash-1',
      },
    });

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Looks wrong? Run OCR' }));
    fireEvent.click(screen.getByRole('button', { name: 'Extract text' }));

    await screen.findByTestId('cost-gate-modal');
    expect(api.startPdfPacketOcr).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('cost-gate-confirm'));
    await waitFor(() => expect(api.startPdfPacketOcr).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      expect.objectContaining({ confirmation: 'quote-hash-1' }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });

  it('uses the newest OCR job for progress, rematches on analysis revision, and imports native text safely', async () => {
    const running: PdfPacketSplitSnapshot = {
      ...baseSnapshot,
      text_source: 'native',
      analysis_revision: 1,
      jobs: [
        {
          job_id: 'old-full',
          kind: 'ocr_full',
          progress: { status: 'done', done: 4, total: 4, error: null },
          engine: 'rapidocr',
          pages: [1, 2, 3, 4],
          receipt_id: null,
          accounting: null,
        },
        {
          job_id: 'new-full',
          kind: 'ocr_full',
          progress: { status: 'running', done: 1, total: 3, error: null },
          engine: 'tesseract',
          pages: [2, 3, 4],
          receipt_id: null,
          accounting: null,
        },
      ],
    };
    api.setPdfPacketTextSource.mockResolvedValue(running);
    api.getPdfPacketSplit.mockResolvedValue({ ...running, analysis_revision: 2 });

    renderFlow();
    await choosePacket();
    fireEvent.click(screen.getByRole('button', { name: 'Yes, use extracted text' }));

    expect(await screen.findByText('Text: 4 of 4 read')).toBeInTheDocument();
    await waitFor(() => expect(api.matchPdfPacketCandidates.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 2_000 });
    fireEvent.click(screen.getByRole('button', { name: 'Continue with 1 marked start →' }));
    expect(screen.getByRole('textbox', { name: 'New sheet' })).toHaveValue('Records documents');
    expect(screen.getByRole('textbox', { name: 'File names' })).toHaveValue('{packet} · pp {start}–{end}');
    expect(screen.queryByRole('combobox', { name: 'Destination' })).not.toBeInTheDocument();
    expect(screen.queryByText(/OCR stays as ordinary Frisket text/)).not.toBeInTheDocument();
    const keepOcr = screen.getByRole('checkbox', { name: 'Keep OCR text and boxes with each PDF' });
    expect(keepOcr).toBeDisabled();
    expect(keepOcr).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Import 1 document' }));
    await waitFor(() => expect(api.commitPdfPacketSplit).toHaveBeenCalled());
    expect(api.commitPdfPacketSplit.mock.calls[0][2]).toEqual(expect.objectContaining({
      confirmed_starts: [1],
      keep_ocr_text: false,
    }));
  });

  it('shows a failed commit job once and retries an edited destination with a fresh key', async () => {
    const failedCommit: PdfPacketSplitSnapshot = {
      ...baseSnapshot,
      text_source: 'native',
      jobs: [{
        job_id: 'commit-failed-1',
        kind: 'commit',
        progress: {
          status: 'error',
          done: 0,
          total: 1,
          error: 'A sheet named Records documents already exists.',
        },
        engine: null,
        pages: [],
        receipt_id: null,
        accounting: null,
      }],
    };
    api.getPdfPacketSplit.mockResolvedValue(failedCommit);

    const importButton = await reachImportStep();
    fireEvent.click(importButton);

    expect(await screen.findByText('A sheet named Records documents already exists.')).toBeInTheDocument();
    const firstRequest = api.commitPdfPacketSplit.mock.calls[0][2];
    fireEvent.change(screen.getByRole('textbox', { name: 'New sheet' }), {
      target: { value: 'Records imported' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Import 1 document' }));

    await waitFor(() => expect(api.commitPdfPacketSplit).toHaveBeenCalledTimes(2));
    const retryRequest = api.commitPdfPacketSplit.mock.calls[1][2];
    expect(retryRequest.destination).toEqual({ kind: 'new_sheet', name: 'Records imported' });
    expect(retryRequest.idempotency_key).not.toBe(firstRequest.idempotency_key);
    await waitFor(() => {
      expect(screen.queryByText('A sheet named Records documents already exists.')).not.toBeInTheDocument();
    });
  });

  it('preserves the commit key after an ambiguous transport failure and stays busy from the server snapshot', async () => {
    const committing: PdfPacketSplitSnapshot = {
      ...baseSnapshot,
      status: 'committing',
      text_source: 'native',
      jobs: [{
        job_id: 'commit-running-1',
        kind: 'commit',
        progress: { status: 'running', done: 0, total: 1, error: null },
        engine: null,
        pages: [],
        receipt_id: null,
        accounting: null,
      }],
    };
    api.commitPdfPacketSplit
      .mockRejectedValueOnce(new Error('Connection lost after sending the request.'))
      .mockResolvedValueOnce({
        schema_version: 'frisket.pdf_packet_split.v1',
        split_id: 'split-1',
        job_id: 'commit-running-1',
        kind: 'commit',
        total: 1,
        receipt_id: null,
      });
    api.getPdfPacketSplit.mockResolvedValue(committing);

    const importButton = await reachImportStep();
    fireEvent.click(importButton);

    expect(await screen.findByText('Connection lost after sending the request.')).toBeInTheDocument();
    const firstRequest = api.commitPdfPacketSplit.mock.calls[0][2];
    fireEvent.click(screen.getByRole('button', { name: 'Import 1 document' }));

    await waitFor(() => expect(api.commitPdfPacketSplit).toHaveBeenCalledTimes(2));
    const retryRequest = api.commitPdfPacketSplit.mock.calls[1][2];
    expect(retryRequest.idempotency_key).toBe(firstRequest.idempotency_key);
    expect(await screen.findByRole('button', { name: 'Importing…' })).toBeDisabled();
    expect(screen.queryByText('Connection lost after sending the request.')).not.toBeInTheDocument();
  });

  it('drops a pending upload when the project changes', async () => {
    let resolveUpload!: (snapshot: PdfPacketSplitSnapshot) => void;
    api.createPdfPacketSplit.mockReturnValue(new Promise((resolve) => {
      resolveUpload = resolve;
    }));
    const first = renderFlow('project-1');
    fireEvent.change(screen.getByTestId('pdf-packet-input'), {
      target: { files: [new File(['pdf'], 'Records.pdf', { type: 'application/pdf' })] },
    });
    first.rerender(<PdfPacketImportFlow {...first.props} projectId="project-2" />);
    resolveUpload(baseSnapshot);

    await waitFor(() => expect(screen.getByTestId('pdf-packet-chooser')).toBeInTheDocument());
    expect(screen.queryByTestId('pdf-packet-flow')).not.toBeInTheDocument();
  });
});
