import { describe, expect, it } from 'vitest';
import type { EvidenceViewerPayload } from '../../src/api/types';
import { evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';
import { reviewCitationSources, sourceLocation, sourcesForField } from '../../src/components/review/reviewCitationSources';

function payload(role: string, artifact: ReturnType<typeof evidenceArtifact>): EvidenceViewerPayload {
  return {
    schema_version: 'frisket.evidence_viewer.v1', warnings: [], artifacts: [artifact],
    link: {
      id: 1, stable_id: `link:${role}:${artifact.stable_id}`, export_ref: 'link', subject_kind: 'cell', subject_ref: null,
      sheet_id: 1, row_id: 1, column_id: 1, run_id: 4, op_id: null, receipt_id: null, role, status: 'active',
      confidence: null, pinned: false, producer: {}, stale_reason: null, stale_at: null,
      created_at: '2026-09-01T00:00:00Z', text_layer_hash_mismatch: false,
    },
  };
}

describe('review citation source tabs', () => {
  it('groups a shared cited artifact across fields and omits provenance-only artifacts', () => {
    const shared = evidenceArtifact({
      stable_id: 'source:markdown', title: 'filing.md', media_type: 'text/markdown',
      spans: [evidenceSpan({ stable_id: 'span:one', required: true })],
      text_context: { text: 'A cited passage.', offset_unit: 'utf16_code_unit', ranges: [{ span_id: 'span:one', start: 2, end: 7 }] },
    });
    const sameSourceSecondField = { ...shared, spans: [evidenceSpan({ stable_id: 'span:two', required: true })],
      text_context: { ...shared.text_context!, ranges: [{ span_id: 'span:two', start: 9, end: 16 }] } };
    const provenance = evidenceArtifact({ stable_id: 'source:original-pdf', media_type: 'application/pdf', spans: [evidenceSpan()] });

    const sources = reviewCitationSources([
      { fieldId: 'defendant', payload: payload('citation', shared) },
      { fieldId: 'plaintiff', payload: payload('citation', sameSourceSecondField) },
      { fieldId: 'defendant', payload: payload('source_provenance', provenance) },
    ]);

    expect(sources).toHaveLength(1);
    expect(sources[0].id).toBe('source:markdown');
    expect(sources[0].fieldIds).toEqual(['defendant', 'plaintiff']);
    expect(sources[0].artifact.spans.map((span) => span.stable_id)).toEqual(['span:one', 'span:two']);
    expect(sourcesForField(sources, 'plaintiff')).toEqual(sources);
  });

  it('keeps other active occurrences when one citation span is required', () => {
    const artifact = evidenceArtifact({ spans: [
      evidenceSpan({ stable_id: 'required', required: true }),
      evidenceSpan({ stable_id: 'other-match', required: false }),
    ] });
    const sources = reviewCitationSources([{ fieldId: 'name', payload: payload('citation', artifact) }]);
    expect(sources[0].artifact.spans.map((span) => span.stable_id)).toEqual(['required', 'other-match']);
  });

  it('uses persisted page and temporal selectors as tab locations', () => {
    const pdf = evidenceArtifact({ stable_id: 'source:pdf', media_type: 'application/pdf', spans: [evidenceSpan({ selector: { page_start: 2 } })] });
    const audio = evidenceArtifact({ stable_id: 'source:audio', media_type: 'audio/mpeg', spans: [evidenceSpan({ selector: { start_ms: 849000 } })] });
    const sources = reviewCitationSources([
      { fieldId: 'relief', payload: payload('citation', pdf) },
      { fieldId: 'relief', payload: payload('citation', audio) },
    ]);

    expect(sourceLocation(sources[0], 'relief')).toBe('p. 2');
    expect(sourceLocation(sources[1], 'relief')).toBe('14:09');
  });

  it('unions span references for one temporal run cited by separate fields', () => {
    const firstField = evidenceArtifact({
      stable_id: 'source:recording', media_type: 'audio/mpeg',
      spans: [evidenceSpan({ stable_id: 'span:intro', selector: { start_ms: 1_000, end_ms: 3_000 } })],
      runs: [{ index: 4, start_ms: 1_000, end_ms: 5_000, span_ids: ['span:intro'], clip_url: 'https://example.test/clip' }],
    });
    const secondField = {
      ...firstField,
      spans: [evidenceSpan({ stable_id: 'span:detail', selector: { start_ms: 3_000, end_ms: 5_000 } })],
      runs: [{ index: 4, start_ms: 1_000, end_ms: 5_000, span_ids: ['span:detail'], clip_url: 'https://example.test/clip' }],
    };

    const source = reviewCitationSources([
      { fieldId: 'summary', payload: payload('citation', firstField) },
      { fieldId: 'finding', payload: payload('citation', secondField) },
    ])[0];

    expect(source.artifact.runs).toEqual([
      { index: 4, start_ms: 1_000, end_ms: 5_000, span_ids: ['span:intro', 'span:detail'], clip_url: 'https://example.test/clip' },
    ]);
  });

  it('labels plain-text sources as TXT', () => {
    const plainText = evidenceArtifact({ stable_id: 'source:notes', media_type: 'text/plain', spans: [evidenceSpan()] });

    expect(reviewCitationSources([{ fieldId: 'notes', payload: payload('citation', plainText) }])[0].kind).toBe('TXT');
  });
});
