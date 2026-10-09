import { describe, expect, it } from 'vitest';

import type { PdfPacketMatchResult, PdfPacketSplitSnapshot } from '../../src/api/pdfPacketSplits';
import {
  confirmedDocuments,
  formatPdfPacketDocumentName,
  initialPdfPacketFlowState,
  pdfPacketNamePatternError,
  pdfPacketFlowReducer,
  samplePages,
} from '../../src/components/importWorkspace/pdfPacket/model';

const snapshot: PdfPacketSplitSnapshot = {
  schema_version: 'frisket.pdf_packet_split.v1',
  split_id: 'split-1',
  status: 'ready',
  packet: {
    blob_hash: 'sha256:packet',
    filename: 'FOIA packet.pdf',
    mime: 'application/pdf',
    size: 4_000,
    page_count: 20,
  },
  prepare: {
    job_id: 'prepare-1',
    progress: { status: 'done', done: 20, total: 20, error: null },
    pages_ready: Array.from({ length: 20 }, (_, index) => index + 1),
    native_text_pages: [1, 2],
    visual_pages_ready: 20,
  },
  text_source: 'unconfirmed',
  ocr_engine: null,
  jobs: [],
  analysis_revision: 0,
  expires_at: '2026-10-10T00:00:00Z',
  commit_result: null,
};

const matches: PdfPacketMatchResult = {
  schema_version: 'frisket.pdf_packet_split.v1',
  split_id: 'split-1',
  analysis_revision: 2,
  clusters: [{ id: 1, confirmed_pages: [1, 9] }],
  pages: [],
  suggested_pages: [5, 14, 19],
  question_pages: [11],
  phrase_counts: {},
  accept_all_scope: 'packet',
};

describe('PDF packet split state', () => {
  it('starts page one immutably and restores only remembered import options', () => {
    const prepared = pdfPacketFlowReducer(initialPdfPacketFlowState, {
      type: 'prepared',
      snapshot,
      remembered: { namePattern: '{packet}-{start}', keepOcrText: false },
    });

    expect(prepared.confirmedStarts).toEqual([1]);
    expect(prepared.destinationName).toBe('FOIA packet documents');
    expect(prepared.namePattern).toBe('{packet}-{start}');
    expect(prepared.keepOcrText).toBe(false);
    expect(prepared.rememberOptions).toBe(true);
    expect(pdfPacketFlowReducer(prepared, { type: 'toggleStart', page: 1 })).toBe(prepared);
  });

  it('accepts every server suggestion across the packet without importing unaccepted suggestions', () => {
    const withMatches = pdfPacketFlowReducer(
      { ...initialPdfPacketFlowState, confirmedStarts: [1, 9], rejected: [14] },
      { type: 'matches', value: matches },
    );

    expect(confirmedDocuments(withMatches.confirmedStarts, 20)).toEqual([
      { index: 1, start: 1, end: 8, pages: 8 },
      { index: 2, start: 9, end: 20, pages: 12 },
    ]);

    const accepted = pdfPacketFlowReducer(withMatches, { type: 'acceptAll' });
    expect(accepted.confirmedStarts).toEqual([1, 5, 9, 14, 19]);
    expect(accepted.rejected).toEqual([]);
  });

  it('chooses six samples spread from the first through last page', () => {
    expect(samplePages(284)).toEqual([1, 58, 114, 171, 227, 284]);
    expect(samplePages(3)).toEqual([1, 2, 3]);
  });

  it('previews every backend-supported filename field and adds the PDF suffix', () => {
    const document = { index: 3, start: 17, end: 21, pages: 5 };
    expect(formatPdfPacketDocumentName(
      '{packet}-{index}-{page}-{start}-{end}',
      'FOIA packet.pdf',
      document,
    )).toBe('FOIA packet-3-17-17-21.pdf');
    expect(formatPdfPacketDocumentName('{packet}.pdf', 'FOIA packet.pdf', document)).toBe('FOIA packet.pdf');
    expect(pdfPacketNamePatternError('{packet}-{unknown}')).toBe('Unknown field {unknown}.');
  });
});
