import { describe, expect, it } from 'vitest';

import type { PdfPacketMatchResult, PdfPacketSplitSnapshot } from '../../src/api/pdfPacketSplits';
import {
  initialPdfPacketFlowState,
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
  ocr_pages: [],
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
  unsure_pages: [11],
  phrase_counts: {},
  accept_all_scope: 'packet',
};

describe('PDF packet split state', () => {
  it('starts page one immutably', () => {
    const prepared = pdfPacketFlowReducer(initialPdfPacketFlowState, {
      type: 'prepared',
      snapshot,
    });

    expect(prepared.confirmedStarts).toEqual([1]);
    expect(pdfPacketFlowReducer(prepared, { type: 'toggleStart', page: 1 })).toBe(prepared);
  });

  it('builds sorted whole-packet review queues with suggestions included in unsure', async () => {
    const { packetFindQueues } = await import('../../src/components/importWorkspace/pdfPacket/packetFindQueues');
    const duplicatedAndUnordered = {
      ...matches,
      suggested_pages: [19, 5, 19, 14],
      unsure_pages: [18, 11, 5, 11],
    };

    expect(packetFindQueues(duplicatedAndUnordered, [1, 5], [14, 18])).toEqual({
      unsure: [19, 11],
      suggested: [19],
    });
  });

  it('chooses six samples spread from the first through last page', () => {
    expect(samplePages(284)).toEqual([1, 58, 114, 171, 227, 284]);
    expect(samplePages(3)).toEqual([1, 2, 3]);
  });

  it('refreshes to six distinct pages that are new when the packet has room', async () => {
    const { resamplePages } = await import('../../src/components/importWorkspace/pdfPacket/model');
    const previous = samplePages(20);
    const refreshed = resamplePages(20, previous);

    expect(refreshed).toHaveLength(6);
    expect(new Set(refreshed).size).toBe(6);
    expect(refreshed.every((page) => !previous.includes(page))).toBe(true);
  });

});
