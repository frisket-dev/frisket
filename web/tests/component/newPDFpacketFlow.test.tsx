// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  question_pages: [],
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
    fireEvent.click(screen.getByRole('button', { name: 'Run rapidocr on this sample' }));

    await screen.findByText('OCR text from rapidocr');
    expect(api.setPdfPacketTextSource).toHaveBeenCalledWith(
      'project-1',
      'split-1',
      { kind: 'ocr', engine: 'rapidocr' },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole('button', { name: 'Use this OCR and read all 4 pages' })).toBeEnabled();
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
    fireEvent.click(screen.getByRole('button', { name: 'Run rapidocr on this sample' }));

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

    expect(await screen.findByText('Text: 2 of 4 read')).toBeInTheDocument();
    await waitFor(() => expect(api.matchPdfPacketCandidates.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 2_000 });
    fireEvent.click(screen.getByRole('button', { name: 'Continue to import →' }));
    const keepOcr = screen.getByRole('checkbox', { name: 'Keep OCR text and boxes with each PDF' });
    expect(keepOcr).toBeDisabled();
    expect(keepOcr).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Import 1 documents' }));
    await waitFor(() => expect(api.commitPdfPacketSplit).toHaveBeenCalled());
    expect(api.commitPdfPacketSplit.mock.calls[0][2]).toEqual(expect.objectContaining({
      confirmed_starts: [1],
      keep_ocr_text: false,
    }));
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
