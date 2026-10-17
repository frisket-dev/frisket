import { describe, expect, it } from 'vitest';
import type { EvidenceViewerPayload } from '../../src/api/types';
import { mergeCitedQuote } from '../../src/workbench/citationQuote';
import { evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

function payload(truncated: boolean): EvidenceViewerPayload {
  return { artifacts: [evidenceArtifact({ spans: [evidenceSpan({
    quote: 'A quoted passage', quote_truncated: truncated, required: true,
  })] })] } as EvidenceViewerPayload;
}

describe('bounded citation quotes', () => {
  it('marks truncated viewer quotes as excerpts in the citation summary', () => {
    expect(mergeCitedQuote(payload(true))).toBe('A quoted passage… [excerpt]');
  });
  it('preserves ordinary exact citation quotes', () => {
    expect(mergeCitedQuote(payload(false))).toBe('A quoted passage');
  });
});
