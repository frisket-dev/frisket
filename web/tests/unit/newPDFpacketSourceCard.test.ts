import { describe, expect, it } from 'vitest';

import type { EvidenceArtifact } from '../../src/api/open';
import { pdfPacketSourceCardData } from '../../src/components/evidence/pdfPacketSourceCardModel';

const artifact = {
  filename: 'FOIA packet.pdf',
  page_count: 196,
  external_ref: { kind: 'pdf_packet_split_source', sibling_count: 29 },
  artifact_ref: {
    blob: { hash: 'hash', url: '/api/blobs/hash', filename: 'FOIA packet.pdf' },
  },
  source_cell: { sheet_id: 17, row_id: 2, column_id: 4 },
  spans: [{ selector: { kind: 'page_range', page_start: 17, page_end: 21 } }],
} as EvidenceArtifact;

describe('PDF packet source card projection', () => {
  it('builds the deep link, range strip values, and sibling destination from evidence', () => {
    expect(pdfPacketSourceCardData(artifact)).toEqual({
      filename: 'FOIA packet.pdf',
      pageCount: 196,
      pageStart: 17,
      pageEnd: 21,
      otherDocumentCount: 28,
      blobUrl: '/api/blobs/hash',
      sourceSheetId: 17,
    });
  });

  it('does not mislabel ordinary provenance as a packet source', () => {
    expect(pdfPacketSourceCardData({
      ...artifact,
      external_ref: { kind: 'uploaded_file' },
    })).toBeNull();
  });
});
